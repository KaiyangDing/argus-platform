"""companies/documents API 集成测试：真 PG + 真 MinIO，覆盖 P1.3 验收行。"""

import hashlib

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
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
