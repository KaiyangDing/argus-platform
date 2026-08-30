"""SQL 内混合检索（P3.4 批2）：词法 OR-tsquery + 向量 HNSW + RRF 融合。

数据经 store_chunks 走真测试库；向量全程 DeterministicFakeEmbedding——
同文本同向量的确定性让「向量距离 0」可精确构造，RRF 名次断言不靠运气：
全部块都进向量深池（各有向量名次分），词法独中的块必然两路得分居首。
"""

import uuid

import pytest
from langchain_core.documents import Document as LCDocument
from langchain_core.embeddings import DeterministicFakeEmbedding
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.worker as worker_mod
from app.ingest import embed_chunks
from app.models import Company, Document, User
from app.retrieval import make_company_search

Factory = async_sessionmaker[AsyncSession]

EMBED_DIM = 1024

# 文本刻意互斥用词（连「的」都避开）：词法命中的归属在断言里可推理
TEXTS = [
    "甲烷传感器业务毛利率百分之四十。",
    "经营活动现金流量净额充足且稳定。",
    "人力成本上升构成主要经营风险。",
]


def _fake() -> DeterministicFakeEmbedding:
    return DeterministicFakeEmbedding(size=EMBED_DIM)


class _NoCallEmbeddings(DeterministicFakeEmbedding):
    def embed_query(self, text: str) -> list[float]:
        raise AssertionError("空语料必须短路，不该触发 embed_query")


async def _seed_company(
    factory: Factory, texts: list[str], monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str]:
    """user→company→document→chunks 全链入库；返回 (owner_key, company_key)。"""
    monkeypatch.setattr(worker_mod, "SessionFactory", factory)
    async with factory() as session:
        user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        company = Company(owner_id=user.id, name=f"co-{uuid.uuid4().hex[:8]}")
        session.add(company)
        await session.flush()
        doc = Document(
            owner_id=user.id,
            company_id=company.id,
            filename="r.pdf",
            object_key="k.pdf",
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            size_bytes=1,
            status="ready",
        )
        session.add(doc)
        await session.commit()
        owner_id, company_id, document_id = user.id, company.id, doc.id

    source = f"r-{uuid.uuid4().hex[:8]}"
    chunks = [
        LCDocument(
            page_content=text,
            metadata={
                "source_id": source,
                "company": str(company_id),
                "page": i + 1,
                "seq": i,
                "chunk_id": f"{source}:{i}",
                "section": "第一节 概况" if i == 0 else "",
            },
        )
        for i, text in enumerate(texts)
    ]
    await worker_mod.store_chunks(
        owner_id, company_id, document_id, chunks, embed_chunks(chunks, _fake())
    )
    return str(owner_id), str(company_id)


async def test_lexical_hit_ranks_first(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """词法独中的块居首：它独得词法名次分，且与其他块一样有向量名次分。"""
    owner, company = await _seed_company(session_factory, TEXTS, monkeypatch)
    search = make_company_search(owner, company, _fake())
    out = search("甲烷传感器毛利率如何", company, 3)
    assert out
    assert out[0].page_content == TEXTS[0]
    assert out[0].metadata["section"] == "第一节 概况"


async def test_identical_text_vector_hit(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """query 与某块全文相同：确定性 fake 下向量距离为 0，该块必居首。"""
    owner, company = await _seed_company(session_factory, TEXTS, monkeypatch)
    search = make_company_search(owner, company, _fake())
    out = search(TEXTS[1], company, 3)
    assert out[0].page_content == TEXTS[1]


async def test_or_semantics_partial_match_still_hits(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OR 组词语义：查询里一堆词只有部分命中，词法路仍然计分（AND 会整路打灭）。"""
    owner, company = await _seed_company(session_factory, TEXTS, monkeypatch)
    search = make_company_search(owner, company, _fake())
    # 「人力成本」「风险」只在 TEXTS[2]；「境外收购」全库无命中
    out = search("境外收购带来人力成本与整合风险", company, 3)
    assert out[0].page_content == TEXTS[2]


async def test_k_truncates_after_fusion(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, company = await _seed_company(session_factory, TEXTS, monkeypatch)
    search = make_company_search(owner, company, _fake())
    assert len(search("现金流量与毛利率", company, 2)) == 2


async def test_metadata_contract_fields(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SearchFn 契约的 metadata 字段齐全（doc_to_ref 的输入面）。"""
    owner, company = await _seed_company(session_factory, TEXTS, monkeypatch)
    search = make_company_search(owner, company, _fake())
    doc = search("毛利率", company, 1)[0]
    for key in ("chunk_id", "source_id", "company", "page", "section"):
        assert key in doc.metadata
    assert doc.metadata["company"] == company


async def test_empty_corpus_short_circuits(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空语料恒空函数：不打 embed_query（研究图空语料路径省十几次白调）。"""
    owner, company = await _seed_company(session_factory, [], monkeypatch)
    search = make_company_search(owner, company, _NoCallEmbeddings(size=EMBED_DIM))
    assert search("任意问题", company, 5) == []


async def test_company_isolation(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_a, company_a = await _seed_company(session_factory, TEXTS, monkeypatch)
    _owner_b, company_b = await _seed_company(
        session_factory, ["另一家公司专属内容。"], monkeypatch
    )
    search_a = make_company_search(owner_a, company_a, _fake())
    out = search_a("另一家公司专属内容", company_a, 5)
    assert all(doc.metadata["company"] == company_a for doc in out)
    assert all("另一家" not in doc.page_content for doc in out)
    assert company_b != company_a
