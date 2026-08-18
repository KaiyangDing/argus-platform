"""检索层单测：分词、行转换、混合检索形状。纯内存为主，MinIO 只走空索引与往返。

BM25 侧用真实词频（jieba 分词后可验证强命中），向量侧 Fake embedding
只保证确定性排名——断言聚焦"混合检索返回形状与 BM25 强命中在场"。
"""

import uuid

from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.vectorstores import InMemoryVectorStore

from app.ingest import append_to_index, split_pages
from app.retrieval import (
    build_hybrid_search,
    jieba_tokenize,
    make_company_search,
    rows_to_documents,
)

EMBED_DIM = 1024


def _fake() -> DeterministicFakeEmbedding:
    return DeterministicFakeEmbedding(size=EMBED_DIM)


def test_jieba_tokenize_chinese() -> None:
    tokens = jieba_tokenize("永辉超市的营业收入同比增长")
    # jieba 词典粒度：营业收入 → 营业+收入；两侧同一分词器，粒度一致即可对撞
    assert "营业" in tokens
    assert "收入" in tokens
    assert len(tokens) > 3
    assert all(t.strip() for t in tokens)


def test_rows_to_documents() -> None:
    rows: list[dict[str, object]] = [
        {
            "chunk_id": "a-11111111:0",
            "source_id": "a-11111111",
            "company": "c1",
            "page": 3,
            "seq": 0,
            "text": "第三页的内容。",
        }
    ]
    docs = rows_to_documents(rows)
    assert len(docs) == 1
    assert docs[0].id == "a-11111111:0"
    assert docs[0].page_content == "第三页的内容。"
    assert docs[0].metadata["page"] == 3
    assert "seq" not in docs[0].metadata


def _corpus_docs() -> list[Document]:
    texts = [
        "公司二零二四年营业收入为八百亿元，同比增长百分之十二。",
        "董事会审议通过了新的股权激励计划，覆盖核心技术人员。",
        "华东区域新开门店四十家，供应链中心投入使用。",
        "公司发布可持续发展报告，披露碳排放数据。",
        "监事会对年度内部控制评价报告无异议。",
    ]
    return [
        Document(
            id=f"d-0000000{i}:0",
            page_content=t,
            metadata={
                "source_id": f"d-0000000{i}",
                "company": "c1",
                "page": 1,
                "chunk_id": f"d-0000000{i}:0",
            },
        )
        for i, t in enumerate(texts)
    ]


def test_build_hybrid_search_shape_and_bm25_hit() -> None:
    docs = _corpus_docs()
    store = InMemoryVectorStore(_fake())
    store.add_documents(docs)
    search = build_hybrid_search(docs, store, pool=50)

    hits = search("营业收入同比增长多少", "ignored-slug", 3)
    assert 0 < len(hits) <= 3
    assert all(isinstance(h, Document) for h in hits)
    # BM25 强词命中（营业收入/同比增长双词匹配）必须进入融合结果
    assert any("营业收入" in h.page_content for h in hits)


def test_hybrid_search_k_truncation() -> None:
    docs = _corpus_docs()
    store = InMemoryVectorStore(_fake())
    store.add_documents(docs)
    search = build_hybrid_search(docs, store, pool=50)
    assert len(search("公司经营情况", "ignored", 2)) == 2


def test_make_company_search_empty_corpus(_test_db: None) -> None:
    search = make_company_search(str(uuid.uuid4()), str(uuid.uuid4()), _fake())
    assert search("任何查询", "ignored", 5) == []


def test_make_company_search_roundtrip(_test_db: None) -> None:
    owner, company = str(uuid.uuid4()), str(uuid.uuid4())
    chunks = split_pages(
        [
            Document(
                page_content="本年度营业收入显著增长，主要来自线上渠道扩张。" * 20,
                metadata={"source_id": "rt-11111111", "company": company, "page": 1},
            )
        ]
    )
    append_to_index(owner, company, chunks, embeddings=_fake())

    search = make_company_search(owner, company, _fake())
    hits = search("营业收入增长", "ignored", 4)
    assert hits
    assert hits[0].metadata["source_id"] == "rt-11111111"
