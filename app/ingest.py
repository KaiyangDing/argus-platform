"""语料入库管线：MinIO 原件 → PDF 解析 → 中文切块 → embedding → per-company 索引。

设计源自研究仓 argus-lg 的验证结论（ADR-005，产品化重写）：
- PyPDFLoader 页粒度加载，页码归一为 1 起
- RecursiveCharacterTextSplitter：中文分隔符优先级，500/50，keep_separator=end
- chunk 字段：source_id / company / page / seq / chunk_id="{source_id}:{seq}" / text
- embedding：text-embedding-v4 经 dashscope OpenAI 兼容端点；
  check_embedding_ctx_length=False 必设；单请求批 10 条为端点上限
- 索引：chunks.jsonl + InMemoryVectorStore dump，存 MinIO，对象键
  {owner_id}/{company_id}/index/(chunks.jsonl|vectors.json)

本模块全部同步函数：worker 以 asyncio.to_thread 调用，单测直接调用。
"""

import json
import tempfile
from pathlib import Path

from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from minio.error import S3Error

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
    """一页一 Document，metadata 即产品 chunk 契约字段；页码归一为 1 起。"""
    pages = PyPDFLoader(str(pdf_path)).load()
    for doc in pages:
        doc.metadata = {
            "source_id": source_id,
            "company": company_key,
            "page": int(doc.metadata.get("page", 0)) + 1,
        }
    return pages


def make_splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        separators=SEPARATORS,
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        keep_separator="end",
    )


def split_pages(pages: list[Document]) -> list[Document]:
    """切块并补 seq / chunk_id；splitter 自动继承页 metadata。"""
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
