"""ingest 管线单测：解析与切块契约。全 Fake embedding 零真调。

PDF 解析质量归研究仓评测；此处只验产品适配层：metadata 契约、
seq/chunk_id 补齐、空白页零块、章节面包屑。写入路径（分词/嵌入/入库）
在 test_chunks_pg。
"""

from pathlib import Path

from langchain_core.documents import Document
from pypdf import PdfWriter

from app.engine.ingest import (
    annotate_page_sections,
    corpus_profile,
    load_pdf_pages,
    make_source_id,
    split_pages,
)


def _page(source_id: str, company: str, page: int, text: str) -> Document:
    return Document(
        page_content=text,
        metadata={"source_id": source_id, "company": company, "page": page},
    )


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


def test_annotate_page_sections_running_heads() -> None:
    """跨页运行章节头状态机：编号标题/括号次级/报表名/千分位表行守卫。"""
    pages = [
        _page("s-11111111", "c1", 1, "封面说明\n一、公司简介\n本公司主营……"),
        _page("s-11111111", "c1", 2, "5、应收账款\n3、2,345,678\n其他内容"),
        _page("s-11111111", "c1", 3, "（1）按账龄披露\n一年以内 1,234,567"),
        _page("s-11111111", "c1", 4, "延续上一页的表格数字"),
        _page("s-11111111", "c1", 5, "母公司资产负债表\n货币资金 999"),
        _page("s-11111111", "c1", 6, "延续母公司报表"),
    ]
    annotate_page_sections(pages)
    assert [p.metadata["section"] for p in pages] == [
        "",  # 首页：进入本页前无标题
        "一、公司简介",  # 跨页运行头
        "5、应收账款",  # 表行「3、2,345,678」被千分位守卫拦下，不当标题
        "5、应收账款 / （1）按账龄披露",  # 括号级=次级，不覆盖主标题
        "5、应收账款 / （1）按账龄披露",
        "母公司资产负债表",  # 报表名设 major 并清 minor
    ]


def test_split_inherits_section_into_rows() -> None:
    pages = [
        _page("s-11111111", "c1", 1, "一、经营情况\n" + "营业收入稳步增长。" * 40),
        _page("s-11111111", "c1", 2, "第二页内容。" * 30),
    ]
    annotate_page_sections(pages)
    chunks = split_pages(pages)
    assert all("section" in c.metadata for c in chunks)
    assert all(
        c.metadata["section"] == "一、经营情况"
        for c in chunks
        if c.metadata["page"] == 2
    )  # 标题在前页、正文在后页的场景


def test_corpus_profile_years_and_fallbacks() -> None:
    p = corpus_profile(["李子园2024年年报.pdf", "2023年监管公告.pdf"])
    assert "可用文档 2 份" in p
    assert "覆盖年份：2023、2024" in p
    assert "最新年份：2024" in p

    assert corpus_profile([]) == "该公司暂无语料。"

    p2 = corpus_profile(["招股说明书.pdf"])
    assert "未知（先以宽泛查询探明时间范围）" in p2
    assert "最新年份：未知" in p2
