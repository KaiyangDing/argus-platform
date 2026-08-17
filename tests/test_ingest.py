"""ingest 管线单测：切块契约、索引往返与累加。全 Fake embedding 零真调。

PDF 解析质量归研究仓评测；此处只验产品适配层：metadata 契约、
seq/chunk_id 补齐、空白页零块、MinIO 索引往返、多次追加累加。
"""

import uuid
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding
from pypdf import PdfWriter

from app.ingest import (
    append_to_index,
    load_company_rows,
    load_company_store,
    load_pdf_pages,
    make_source_id,
    split_pages,
)

EMBED_DIM = 1024


def _fake() -> DeterministicFakeEmbedding:
    return DeterministicFakeEmbedding(size=EMBED_DIM)


def _page(source_id: str, company: str, page: int, text: str) -> Document:
    return Document(
        page_content=text,
        metadata={"source_id": source_id, "company": company, "page": page},
    )


def _keys() -> tuple[str, str]:
    return str(uuid.uuid4()), str(uuid.uuid4())


def test_make_source_id() -> None:
    assert make_source_id("2024年报.pdf", "a" * 64) == "2024年报-aaaaaaaa"


def test_split_pages_fills_seq_and_chunk_id() -> None:
    long_text = "国际财务报告准则下的营业收入确认与计量。" * 60
    pages = [
        _page("doc-12345678", "c1", 1, long_text),
        _page("doc-12345678", "c1", 2, "第二页短文本。"),
    ]
    chunks = split_pages(pages)
    assert len(chunks) > 2
    for seq, chunk in enumerate(chunks):
        assert chunk.metadata["seq"] == seq
        assert chunk.metadata["chunk_id"] == f"doc-12345678:{seq}"
        assert chunk.metadata["source_id"] == "doc-12345678"
    assert {c.metadata["page"] for c in chunks} == {1, 2}


def test_load_pdf_pages_blank(tmp_path: Path) -> None:
    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with pdf_path.open("wb") as f:
        writer.write(f)

    pages = load_pdf_pages(pdf_path, "blank-00000000", "c1")
    assert len(pages) == 1
    assert pages[0].metadata == {
        "source_id": "blank-00000000",
        "company": "c1",
        "page": 1,
    }
    assert split_pages(pages) == []


def test_index_roundtrip(_test_db: None) -> None:
    owner, company = _keys()
    chunks = split_pages(
        [_page("r-11111111", company, 1, "营业收入同比增长百分之十二。" * 40)]
    )
    added = append_to_index(owner, company, chunks, embeddings=_fake())
    assert added == len(chunks)
    assert added > 0

    rows = load_company_rows(owner, company)
    assert len(rows) == added
    assert rows[0]["chunk_id"] == "r-11111111:0"
    assert rows[0]["company"] == company
    assert rows[0]["page"] == 1

    store = load_company_store(owner, company, _fake())
    hits = store.similarity_search("营业收入", k=1)
    assert hits
    assert hits[0].metadata["source_id"] == "r-11111111"


def test_append_accumulates(_test_db: None) -> None:
    owner, company = _keys()
    first = split_pages([_page("a-11111111", company, 1, "第一份文档的内容。" * 50)])
    second = split_pages([_page("b-22222222", company, 1, "第二份文档的内容。" * 50)])
    append_to_index(owner, company, first, embeddings=_fake())
    append_to_index(owner, company, second, embeddings=_fake())

    rows = load_company_rows(owner, company)
    assert len(rows) == len(first) + len(second)
    assert {r["source_id"] for r in rows} == {"a-11111111", "b-22222222"}


def test_load_missing_index_is_empty(_test_db: None) -> None:
    owner, company = _keys()
    assert load_company_rows(owner, company) == []
    store = load_company_store(owner, company, _fake())
    assert store.similarity_search("任何查询", k=1) == []


def test_append_is_idempotent_per_source(_test_db: None) -> None:
    """写完索引未置 ready 即崩的重跑场景：同 source 二次 append 必须零副作用。"""
    owner, company = _keys()
    chunks = split_pages([_page("i-11111111", company, 1, "重复入库的文档内容。" * 50)])
    first = append_to_index(owner, company, chunks, embeddings=_fake())
    assert first == len(chunks)
    assert first > 0

    second = append_to_index(owner, company, chunks, embeddings=_fake())
    assert second == 0

    rows = load_company_rows(owner, company)
    assert len(rows) == first
