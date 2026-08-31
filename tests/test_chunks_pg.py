"""chunks 表写入路径（P3.4 批1）：分词、嵌入拆分、幂等入库、检索列可查。

store_chunks 走真测试库（含 pgvector 扩展与 tsvector 列），嵌入全程
DeterministicFakeEmbedding 零真调。
"""

import uuid

import pytest
from langchain_core.documents import Document as LCDocument
from langchain_core.embeddings import DeterministicFakeEmbedding
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.worker as worker_mod
from app.domain.models import Chunk, Company, Document, User
from app.engine.ingest import embed_chunks, tokenize_for_search

Factory = async_sessionmaker[AsyncSession]

EMBED_DIM = 1024


def _fake() -> DeterministicFakeEmbedding:
    return DeterministicFakeEmbedding(size=EMBED_DIM)


def _chunks(source_id: str, company_key: str, n: int = 3) -> list[LCDocument]:
    texts = [
        "营业收入同比增长百分之十二点五，毛利率保持稳定。",
        "经营活动现金流量净额与净利润基本匹配。",
        "主要经营风险包括应收账款回收与人力成本上升。",
    ]
    return [
        LCDocument(
            page_content=texts[i % len(texts)],
            metadata={
                "source_id": source_id,
                "company": company_key,
                "page": 1,
                "seq": i,
                "chunk_id": f"{source_id}:{i}",
                "section": "第一节 经营情况" if i == 0 else "",
            },
        )
        for i in range(n)
    ]


async def _make_scope(factory: Factory) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """user→company→document 链，返回三者 id。"""
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
            filename="report.pdf",
            object_key=f"{user.id}/{company.id}/x.pdf",
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            size_bytes=10,
            status="embedding",
        )
        session.add(doc)
        await session.commit()
        return user.id, company.id, doc.id


def test_tokenize_for_search_splits_chinese() -> None:
    out = tokenize_for_search("营业收入同比增长")
    assert " " in out  # jieba 切出多词，空格串形态
    # jieba 默认词典把「营业收入」切成「营业/收入」两词——粒度本身不重要，
    # 入库列与查询两侧同一分词器、同一粒度即可对撞（P1.5 同款断言教训，勿改回整词）
    assert "营业" in out.split()
    assert "收入" in out.split()


def test_tokenize_for_search_drops_whitespace_tokens() -> None:
    assert tokenize_for_search("  \n\t ") == ""


def test_embed_chunks_aligned_dims() -> None:
    chunks = _chunks("s-00000000", "c1")
    vectors = embed_chunks(chunks, _fake())
    assert len(vectors) == len(chunks)
    assert all(len(v) == EMBED_DIM for v in vectors)


async def test_store_chunks_inserts_then_idempotent(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)
    owner_id, company_id, document_id = await _make_scope(session_factory)
    chunks = _chunks("s-11111111", str(company_id))
    vectors = embed_chunks(chunks, _fake())

    added = await worker_mod.store_chunks(
        owner_id, company_id, document_id, chunks, vectors
    )
    assert added == 3
    # 同批重放（上轮写完未置 ready 即崩的重跑）：ON CONFLICT 全跳过
    again = await worker_mod.store_chunks(
        owner_id, company_id, document_id, chunks, vectors
    )
    assert again == 0

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.company_id == company_id)
        )
    assert count == 3


async def test_store_chunks_text_tokens_queryable(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tsvector 列必须经 to_tsvector 落库（带位置信息），词法查询能命中。"""
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)
    owner_id, company_id, document_id = await _make_scope(session_factory)
    chunks = _chunks("s-22222222", str(company_id))
    await worker_mod.store_chunks(
        owner_id, company_id, document_id, chunks, embed_chunks(chunks, _fake())
    )

    async with session_factory() as session:
        hits = await session.execute(
            select(Chunk.chunk_id).where(
                Chunk.company_id == company_id,
                # 查询侧过同一分词器；此处 plainto(AND) 只验证「列可查」，
                # 生产检索是 OR 组词（见 retrieval.py 与 ADR-012）
                Chunk.text_tokens.op("@@")(
                    func.plainto_tsquery("simple", tokenize_for_search("营业收入"))
                ),
            )
        )
        ids = [row[0] for row in hits]
    assert ids == ["s-22222222:0"]


async def test_store_chunks_embedding_roundtrip(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """向量真实入库：同向量余弦距离为 0（确定性 fake 可复算）。"""
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)
    owner_id, company_id, document_id = await _make_scope(session_factory)
    chunks = _chunks("s-33333333", str(company_id), n=1)
    vectors = embed_chunks(chunks, _fake())
    await worker_mod.store_chunks(owner_id, company_id, document_id, chunks, vectors)

    async with session_factory() as session:
        distance = await session.scalar(
            select(Chunk.embedding.cosine_distance(vectors[0])).where(
                Chunk.company_id == company_id
            )
        )
    assert distance is not None
    assert distance < 1e-6
