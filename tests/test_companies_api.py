"""companies/documents API 集成测试：真 PG + 真 MinIO，覆盖 P1.3 验收行。"""

import hashlib
import uuid

import pytest
from conftest import FakeArq
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import async_url
from app.models import Document
from app.storage import _client

PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
)
PASSWORD = "password-123"


async def _auth_headers(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert resp.status_code == 201
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _create_company(
    client: AsyncClient, headers: dict[str, str], name: str = "Acme Corp"
) -> dict:
    resp = await client.post("/api/companies", json={"name": name}, headers=headers)
    assert resp.status_code == 201
    return resp.json()


async def _upload(
    client: AsyncClient,
    company_id: str,
    headers: dict[str, str],
    content: bytes = PDF_BYTES,
    filename: str = "report.pdf",
) -> Response:
    return await client.post(
        f"/api/companies/{company_id}/documents",
        files={"file": (filename, content, "application/pdf")},
        headers=headers,
    )


async def test_create_and_list_company(client: AsyncClient) -> None:
    headers = await _auth_headers(client, "alice@example.com")
    company = await _create_company(client, headers, name="腾讯控股")
    assert company["name"] == "腾讯控股"
    resp = await client.get("/api/companies", headers=headers)
    assert resp.status_code == 200
    assert [c["id"] for c in resp.json()] == [company["id"]]


async def test_create_duplicate_company_409(client: AsyncClient) -> None:
    headers = await _auth_headers(client, "alice@example.com")
    await _create_company(client, headers)
    resp = await client.post(
        "/api/companies", json={"name": "Acme Corp"}, headers=headers
    )
    assert resp.status_code == 409


async def test_companies_isolated_between_users(client: AsyncClient) -> None:
    headers_a = await _auth_headers(client, "a@example.com")
    await _create_company(client, headers_a)
    headers_b = await _auth_headers(client, "b@example.com")
    resp = await client.get("/api/companies", headers=headers_b)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_companies_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/companies")).status_code == 401


async def test_upload_pdf_creates_queued_document(client: AsyncClient) -> None:
    headers = await _auth_headers(client, "alice@example.com")
    company = await _create_company(client, headers)
    resp = await _upload(client, company["id"], headers)
    assert resp.status_code == 201
    doc = resp.json()
    assert doc["status"] == "queued"
    assert doc["filename"] == "report.pdf"
    assert doc["sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert doc["size_bytes"] == len(PDF_BYTES)

    listed = await client.get(
        f"/api/companies/{company['id']}/documents", headers=headers
    )
    assert [d["id"] for d in listed.json()] == [doc["id"]]

    # 验收行「MinIO 有原件」：object_key 不对外暴露，从测试库读出后 stat 原件
    engine = create_async_engine(
        async_url(get_settings().database_url), poolclass=NullPool
    )
    async with engine.connect() as conn:
        object_key = (await conn.execute(select(Document.object_key))).scalar_one()
    await engine.dispose()
    stat = _client().stat_object(get_settings().minio_bucket, object_key)
    assert stat.size == len(PDF_BYTES)


async def test_upload_rejects_non_pdf(client: AsyncClient) -> None:
    headers = await _auth_headers(client, "alice@example.com")
    company = await _create_company(client, headers)
    resp = await _upload(client, company["id"], headers, content=b"MZ not a pdf")
    assert resp.status_code == 415


async def test_upload_duplicate_409(client: AsyncClient) -> None:
    headers = await _auth_headers(client, "alice@example.com")
    company = await _create_company(client, headers)
    assert (await _upload(client, company["id"], headers)).status_code == 201
    resp = await _upload(client, company["id"], headers, filename="renamed.pdf")
    assert resp.status_code == 409


async def test_upload_to_foreign_company_404(client: AsyncClient) -> None:
    headers_a = await _auth_headers(client, "a@example.com")
    company = await _create_company(client, headers_a)
    headers_b = await _auth_headers(client, "b@example.com")
    resp = await _upload(client, company["id"], headers_b)
    assert resp.status_code == 404


async def test_list_documents_foreign_company_404(client: AsyncClient) -> None:
    headers_a = await _auth_headers(client, "a@example.com")
    company = await _create_company(client, headers_a)
    headers_b = await _auth_headers(client, "b@example.com")
    resp = await client.get(
        f"/api/companies/{company['id']}/documents", headers=headers_b
    )
    assert resp.status_code == 404


async def test_upload_oversize_413(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _auth_headers(client, "alice@example.com")
    company = await _create_company(client, headers)
    monkeypatch.setattr(get_settings(), "max_upload_mb", 0)
    resp = await _upload(client, company["id"], headers)
    assert resp.status_code == 413


async def test_upload_enqueues_ingest_job(
    client: AsyncClient, arq_stub: FakeArq
) -> None:
    headers = await _auth_headers(client, "alice@example.com")
    company = await _create_company(client, headers)
    resp = await _upload(client, company["id"], headers)
    doc_id = resp.json()["id"]
    assert arq_stub.jobs == [("ingest_document", (doc_id,))]


async def _mark_failed(
    factory: async_sessionmaker[AsyncSession], document_id: str
) -> None:
    await _mark_status(factory, document_id, "failed", error="boom")


async def _mark_status(
    factory: async_sessionmaker[AsyncSession],
    document_id: str,
    status_val: str,
    error: str | None = None,
) -> None:
    async with factory() as session:
        await session.execute(
            update(Document)
            .where(Document.id == uuid.UUID(document_id))
            .values(status=status_val, error=error)
        )
        await session.commit()


async def test_retry_failed_document(
    client: AsyncClient,
    arq_stub: FakeArq,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers = await _auth_headers(client, "alice@example.com")
    company = await _create_company(client, headers)
    doc = (await _upload(client, company["id"], headers)).json()
    await _mark_failed(session_factory, doc["id"])

    resp = await client.post(
        f"/api/companies/{company['id']}/documents/{doc['id']}/retry", headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["error"] is None
    assert len(arq_stub.jobs) == 2


async def test_retry_terminal_states_409(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """P3.5 批4 起 queued 可重试（孤儿自愈），守卫只拦 ready 与进行中状态。"""
    headers = await _auth_headers(client, "alice@example.com")
    company = await _create_company(client, headers)
    doc = (await _upload(client, company["id"], headers)).json()
    for blocked in ("ready", "parsing"):
        await _mark_status(session_factory, doc["id"], blocked)
        resp = await client.post(
            f"/api/companies/{company['id']}/documents/{doc['id']}/retry",
            headers=headers,
        )
        assert resp.status_code == 409


async def test_retry_queued_orphan_document(
    client: AsyncClient, arq_stub: FakeArq
) -> None:
    """queued 直接重试 200：孤儿（commit 后 enqueue 前崩）的自愈入口；
    正常排队误点由 _job_id 幂等兜底。"""
    headers = await _auth_headers(client, "alice@example.com")
    company = await _create_company(client, headers)
    doc = (await _upload(client, company["id"], headers)).json()
    resp = await client.post(
        f"/api/companies/{company['id']}/documents/{doc['id']}/retry", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert len(arq_stub.jobs) == 2


async def test_upload_enqueue_failure_marks_failed(
    client: AsyncClient,
    arq_stub: FakeArq,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入队失败不留静默孤儿：文档落 failed，前端重试按钮即恢复入口。"""
    headers = await _auth_headers(client, "alice@example.com")
    company = await _create_company(client, headers)

    async def boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(arq_stub, "enqueue_job", boom)
    resp = await _upload(client, company["id"], headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "failed"
    assert "入队失败" in body["error"]


async def test_retry_foreign_document_404(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers_a = await _auth_headers(client, "a@example.com")
    company = await _create_company(client, headers_a)
    doc = (await _upload(client, company["id"], headers_a)).json()
    await _mark_failed(session_factory, doc["id"])
    headers_b = await _auth_headers(client, "b@example.com")
    resp = await client.post(
        f"/api/companies/{company['id']}/documents/{doc['id']}/retry", headers=headers_b
    )
    assert resp.status_code == 404
