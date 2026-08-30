"""worker 编排测试：状态机全路径。DB 直连 NullPool；embedding 走 ctx 注入缝。

PDF 真解析只测空白页失败路径；happy path monkeypatch _load_pages——
解析质量归 ingest 测试与研究仓评测，此处测任务编排：状态流转、
重试分流（终试落 failed / 非终试抛出）、幂等跳过、处置记录。
"""

import hashlib
import io
import uuid

import pytest
from arq import Retry
from langchain_core.documents import Document as LCDocument
from langchain_core.embeddings import DeterministicFakeEmbedding
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.worker as worker_mod
from app.models import Chunk, Company, Document, User
from app.storage import put_bytes

Factory = async_sessionmaker[AsyncSession]


async def _make_document(
    factory: Factory, status: str = "queued"
) -> tuple[str, str, str, str]:
    """建 user→company→document 链；返回 (doc_id, owner_id, company_id, object_key)。"""
    sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
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
            object_key=f"{user.id}/{company.id}/{sha}.pdf",
            sha256=sha,
            size_bytes=10,
            status=status,
        )
        session.add(doc)
        await session.commit()
        return str(doc.id), str(user.id), str(company.id), doc.object_key


async def _get_status(factory: Factory, doc_id: str) -> tuple[str, str | None]:
    async with factory() as session:
        doc = await session.get(Document, uuid.UUID(doc_id))
        assert doc is not None
        return doc.status, doc.error


def _fake_pages(source_id: str, company_key: str) -> list[LCDocument]:
    return [
        LCDocument(
            page_content="营业收入实现稳健增长，同比上升百分之十二。" * 30,
            metadata={"source_id": source_id, "company": company_key, "page": 1},
        )
    ]


async def test_happy_path_to_ready(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _owner_id, company_id, object_key = await _make_document(session_factory)
    put_bytes(object_key, b"%PDF-placeholder", "application/pdf")
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)
    monkeypatch.setattr(
        worker_mod, "_load_pages", lambda _b, sid, ck: _fake_pages(sid, ck)
    )

    ctx = {"job_try": 1, "embeddings": DeterministicFakeEmbedding(size=1024)}
    result = await worker_mod.ingest_document(ctx, doc_id)

    assert result.startswith("ready:")
    status, error = await _get_status(session_factory, doc_id)
    assert status == "ready"
    assert error is None
    async with session_factory() as session:
        res = await session.execute(
            select(Chunk).where(Chunk.company_id == uuid.UUID(company_id))
        )
        chunks = list(res.scalars())
    assert chunks
    assert chunks[0].document_id == uuid.UUID(doc_id)
    assert chunks[0].embedding is not None


async def test_blank_pdf_fails_at_first_try(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无可提取文本是终态错误：重试改变不了结果，首试即落 failed，不占重试额度。"""
    doc_id, _owner, _company, object_key = await _make_document(session_factory)
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    put_bytes(object_key, buf.getvalue(), "application/pdf")
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)

    result = await worker_mod.ingest_document({"job_try": 1}, doc_id)

    assert result == "failed:NoTextError"
    status, error = await _get_status(session_factory, doc_id)
    assert status == "failed"
    assert error is not None
    assert "无可提取文本" in error


def _boom(_b: bytes, _s: str, _c: str) -> list[LCDocument]:
    msg = "transient network error"
    raise RuntimeError(msg)


async def test_transient_error_raises_arq_retry(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非终试的可重试错误必须抛 arq.Retry——普通异常 arq 不会重投（源码实证）。"""
    doc_id, _owner, _company, object_key = await _make_document(session_factory)
    put_bytes(object_key, b"%PDF-placeholder", "application/pdf")
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)
    monkeypatch.setattr(worker_mod, "_load_pages", _boom)

    with pytest.raises(Retry):
        await worker_mod.ingest_document({"job_try": 1}, doc_id)
    status, _error = await _get_status(session_factory, doc_id)
    assert status == "parsing"


async def test_transient_error_fails_on_final_try(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重试耗尽：落 failed + 处置记录，交给人工重试端点。"""
    doc_id, _owner, _company, object_key = await _make_document(session_factory)
    put_bytes(object_key, b"%PDF-placeholder", "application/pdf")
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)
    monkeypatch.setattr(worker_mod, "_load_pages", _boom)

    result = await worker_mod.ingest_document({"job_try": worker_mod.MAX_TRIES}, doc_id)

    assert result == "failed:RuntimeError"
    status, error = await _get_status(session_factory, doc_id)
    assert status == "failed"
    assert error is not None
    assert "transient network error" in error


async def test_missing_document(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)
    result = await worker_mod.ingest_document({"job_try": 1}, str(uuid.uuid4()))
    assert result == "missing"


async def test_ready_document_skipped(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _owner, _company, _key = await _make_document(
        session_factory, status="ready"
    )
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)
    result = await worker_mod.ingest_document({"job_try": 1}, doc_id)
    assert result == "already-ready"
