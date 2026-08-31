"""语料入库管线：MinIO 原件 → PDF 解析 → 章节面包屑 → 中文切块 → embedding → per-company 索引。

设计源自研究仓 argus-lg 的验证结论（ADR-005，产品化重写；v0.2 返工同步两项）：
- pypdf 直读页粒度（退役 PyPDFLoader：community 日落，它本就是 extract_text 薄壳）
- annotate_page_sections：跨页运行章节头（免重嵌：只进 metadata 与证据渲染，
  不改块文本——embedding/BM25 词面零影响，旧索引零迁移）
- RecursiveCharacterTextSplitter：中文分隔符优先级，500/50，keep_separator=end
- chunk 字段：source_id / company / page / seq / chunk_id="{source_id}:{seq}" /
  section / text
- embedding：text-embedding-v4 经 dashscope OpenAI 兼容端点；
  check_embedding_ctx_length=False 必设；单请求批 10 条为端点上限
- 索引:chunks 表(pgvector + tsvector,P3.4 起)
- corpus_profile：研究图时间锚先验的唯一来源，从 ready 文档文件名数据驱动生成
  （研究仓 v0.2 工程问题账「硬编码年份」条：产品语料任意上传，先验不可预设）

本模块全部同步函数：worker 以 asyncio.to_thread 调用，单测直接调用。
"""

import re
from pathlib import Path

import jieba
from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding, Embeddings
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from app.breakers import emb_breaker
from app.config import get_settings
from app.llm import DASHSCOPE_COMPAT_BASE, EMBED_MODEL

EMBED_BATCH = 10
EMBED_DIM = 1024  # text-embedding-v4 输出维度（fake 向量按此对齐）

# 中文语料分隔符优先级：段落 > 换行 > 句读 > 空格 > 硬切
SEPARATORS = ["\n\n", "\n", "。", "；", "，", " ", ""]
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


class BreakerEmbeddings(OpenAIEmbeddings):
    """embedding 端点熔断包装：open 态秒败，检索层接住降级为纯词法（批2）。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return emb_breaker.call(super().embed_documents, texts)

    def embed_query(self, text: str) -> list[float]:
        return emb_breaker.call(super().embed_query, text)


def make_embeddings() -> Embeddings:
    settings = get_settings()
    if settings.fake_llm:
        # 压测线 B：确定性向量零 API 调用；查询与索引同分布，相似度检索行为正常
        return DeterministicFakeEmbedding(size=EMBED_DIM)
    return BreakerEmbeddings(
        model=EMBED_MODEL,
        base_url=DASHSCOPE_COMPAT_BASE,
        api_key=settings.dashscope_api_key,
        check_embedding_ctx_length=False,
        chunk_size=EMBED_BATCH,
        timeout=60.0,
        max_retries=2,
    )


def tokenize_for_search(text: str) -> str:
    """jieba 分词 → 空格串，交 to_tsvector('simple') 建词位与位置。

    'simple' 配置不做词干化不删停用词——中文分词已在 Python 侧完成，PG 只
    负责倒排与位置信息。位置是 ts_rank 的输入：直接把字符串 cast 成
    tsvector 会丢位置，词法打分全废，必须走 to_tsvector。
    """
    return " ".join(tok for tok in jieba.lcut(text) if tok.strip())


def embed_chunks(
    chunks: list[Document], embeddings: Embeddings | None = None
) -> list[list[float]]:
    """全部块的向量（批 10 打端点）。与旧 append_to_index 的一体式不同：
    嵌入与持久化拆开——ingest 保持纯计算零 DB 依赖，入库事务归 worker。"""
    if embeddings is None:
        embeddings = make_embeddings()
    return embeddings.embed_documents([doc.page_content for doc in chunks])


def make_source_id(filename: str, sha256: str) -> str:
    return f"{Path(filename).stem}-{sha256[:8]}"


def load_pdf_pages(pdf_path: Path, source_id: str, company_key: str) -> list[Document]:
    """一页一 Document，metadata 即产品 chunk 契约字段；页码归一为 1 起。

    pypdf 直读：PyPDFLoader 默认模式就是逐页 extract_text()，行为等价；
    解析层完全归产品掌控（v0.2 天花板定性在解析层，掌控它是后续演进前提）。
    """
    reader = PdfReader(str(pdf_path))
    return [
        Document(
            page_content=page.extract_text() or "",
            metadata={"source_id": source_id, "company": company_key, "page": i + 1},
        )
        for i, page in enumerate(reader.pages)
    ]


# 章节头模式：编号标题（第X节/一、/5、/（1）…）与报表名（合并/母公司×四表）——
# 研究仓 iteration-2 五连语义错配的根因是块不带表格语境，跨页运行头是其修复
_HEADING_RE = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百]+节|[一二三四五六七八九十]+、|"
    r"（[一二三四五六七八九十]+）|\([一二三四五六七八九十]+\)|"
    r"\d{1,2}、|（\d{1,2}）\.?|\(\d{1,2}\)\.?)\s*\S{2,}"
)
_STATEMENT_RE = re.compile(
    r"^\s*(合并|母公司)(资产负债表|利润表|现金流量表|所有者权益变动表)"
)
_TABLE_ROW_RE = re.compile(r"\d{1,3}(?:,\d{3})+")  # 含千分位数字的行是表行不是标题


def annotate_page_sections(pages: list[Document]) -> None:
    """跨页运行章节头：每页 section=进入本页前最近的标题（表头在前页、数字在后页）。

    面包屑只进 metadata 与证据渲染，不改块文本——免重嵌设计：旧向量库照用，
    section 随 chunks.jsonl 在检索层回贴（见 retrieval.build_hybrid_search）。
    """
    major = ""
    minor = ""
    for page in pages:
        page.metadata["section"] = (
            f"{major} / {minor}" if major and minor else major or minor
        )
        for line in page.page_content.splitlines():
            stripped = line.strip()
            if _STATEMENT_RE.match(stripped):
                major, minor = stripped[:40], ""
            elif _HEADING_RE.match(stripped) and not _TABLE_ROW_RE.search(stripped):
                if stripped.startswith(("（", "(")):
                    minor = stripped[:40]  # 次级标题不覆盖主标题
                else:
                    major, minor = stripped[:40], ""


_YEAR_RE = re.compile(r"(20\d{2})")


def corpus_profile(filenames: list[str]) -> str:
    """按 ready 文档文件名生成公司语料概况，供研究图 prompt 注入。

    时间锚等先验的唯一来源：文件名本身携带「年报/公告」等类型词与年份，
    模型直接读；无年份时明示未知，图侧转宽泛查询探明（不凭空预设）。
    """
    if not filenames:
        return "该公司暂无语料。"
    years: set[str] = set()
    entries: list[str] = []
    for name in sorted(filenames):
        stem = Path(name).stem
        years.update(_YEAR_RE.findall(stem))
        entries.append(stem)
    year_line = (
        "、".join(sorted(years)) if years else "未知（先以宽泛查询探明时间范围）"
    )
    latest = max(years) if years else "未知"
    return (
        f"可用文档 {len(entries)} 份：" + "；".join(entries) + "。"
        f"覆盖年份：{year_line}；最新年份：{latest}。"
    )


def make_splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        separators=SEPARATORS,
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        keep_separator="end",
    )


def split_pages(pages: list[Document]) -> list[Document]:
    """切块并补 seq / chunk_id；splitter 自动继承页 metadata（含 section）。"""
    chunks = make_splitter().split_documents(pages)
    for seq, doc in enumerate(chunks):
        doc.metadata["seq"] = seq
        doc.metadata["chunk_id"] = f"{doc.metadata['source_id']}:{seq}"
    return chunks
