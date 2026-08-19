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
- 索引：chunks.jsonl + InMemoryVectorStore dump，存 MinIO，对象键
  {owner_id}/{company_id}/index/(chunks.jsonl|vectors.json)
- corpus_profile：研究图时间锚先验的唯一来源，从 ready 文档文件名数据驱动生成
  （研究仓 v0.2 工程问题账「硬编码年份」条：产品语料任意上传，先验不可预设）

本模块全部同步函数：worker 以 asyncio.to_thread 调用，单测直接调用。
"""

import json
import re
import tempfile
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from minio.error import S3Error
from pypdf import PdfReader

from app.config import get_settings
from app.llm import DASHSCOPE_COMPAT_BASE, EMBED_MODEL
from app.storage import get_bytes, put_bytes

EMBED_BATCH = 10

# 中文语料分隔符优先级：段落 > 换行 > 句读 > 空格 > 硬切
SEPARATORS = ["\n\n", "\n", "。", "；", "，", " ", ""]
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


def make_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=EMBED_MODEL,
        base_url=DASHSCOPE_COMPAT_BASE,
        api_key=get_settings().dashscope_api_key,
        check_embedding_ctx_length=False,
        chunk_size=EMBED_BATCH,
    )


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


def chunks_to_rows(chunks: list[Document]) -> list[dict[str, object]]:
    return [{**doc.metadata, "text": doc.page_content} for doc in chunks]


def _index_keys(owner_id: str, company_id: str) -> tuple[str, str]:
    prefix = f"{owner_id}/{company_id}/index"
    return f"{prefix}/chunks.jsonl", f"{prefix}/vectors.json"


def load_company_rows(owner_id: str, company_id: str) -> list[dict[str, object]]:
    chunks_key, _ = _index_keys(owner_id, company_id)
    try:
        raw = get_bytes(chunks_key)
    except S3Error as exc:
        if exc.code == "NoSuchKey":
            return []
        raise
    return [
        json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()
    ]


def load_company_store(
    owner_id: str, company_id: str, embeddings: Embeddings
) -> InMemoryVectorStore:
    _, vectors_key = _index_keys(owner_id, company_id)
    try:
        raw = get_bytes(vectors_key)
    except S3Error as exc:
        if exc.code == "NoSuchKey":
            return InMemoryVectorStore(embeddings)
        raise
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "vectors.json"
        path.write_bytes(raw)
        return InMemoryVectorStore.load(str(path), embeddings)


def append_to_index(
    owner_id: str,
    company_id: str,
    chunks: list[Document],
    embeddings: Embeddings | None = None,
) -> int:
    """新文档的块并入公司索引并回写 MinIO；返回新增块数。

    embeddings 是注入缝：测试传 DeterministicFakeEmbedding 零真调零花费；
    生产走 None 分支。store.add_documents 是真 embedding 网络调用发生处
    （按批 10 条打 dashscope）。
    """
    if embeddings is None:
        embeddings = make_embeddings()

    rows = load_company_rows(owner_id, company_id)

    new_source = chunks[0].metadata["source_id"] if chunks else None
    if any(r.get("source_id") == new_source for r in rows):
        return 0  # 该文档已入库（上轮写完未置 ready 即崩的重跑），幂等跳过

    store = load_company_store(owner_id, company_id, embeddings)

    store.add_documents(chunks)
    all_rows = rows + chunks_to_rows(chunks)

    chunks_key, vectors_key = _index_keys(owner_id, company_id)
    jsonl = "\n".join(json.dumps(r, ensure_ascii=False) for r in all_rows) + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "vectors.json"
        store.dump(str(path))
        put_bytes(vectors_key, path.read_bytes(), "application/json")
    put_bytes(chunks_key, jsonl.encode("utf-8"), "application/x-ndjson")
    return len(chunks)
