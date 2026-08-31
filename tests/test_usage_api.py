"""usage 端点与配额闸：24 小时窗口聚合、超预算 429、并发满 429、跨用户隔离。

配额判定走 PG（ADR-008），所以这里全是真库断言：窗口边界用 created_at 显式
落到 25 小时前，靠聚合把它排除。两道闸分别单测——预算闸对研究与对话都生效，
并发闸只拦研究（对话是秒级交互，并发归 P3.2 的 HTTP 限流）。
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from langchain_core.language_models import FakeListChatModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import get_chat_model
from app.domain.models import Company, Document, ResearchTask, TokenUsage
from app.main import app

Factory = async_sessionmaker[AsyncSession]
PASSWORD = "password-123"


async def _auth(
    client: AsyncClient, email: str = "alice@example.com"
) -> tuple[dict[str, str], str]:
    resp = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    me = await client.get("/api/auth/me", headers=headers)
    return headers, me.json()["id"]


async def _make_company(factory: Factory, owner_id: str) -> str:
    """建公司并挂一份 ready 文档：让研究端点的 409 前置检查不挡路。"""
    async with factory() as session:
        company = Company(
            owner_id=uuid.UUID(owner_id), name=f"co-{uuid.uuid4().hex[:8]}"
        )
        session.add(company)
        await session.flush()
        sha = uuid.uuid4().hex * 2
        session.add(
            Document(
                owner_id=uuid.UUID(owner_id),
                company_id=company.id,
                filename="r.pdf",
                object_key=f"{owner_id}/{company.id}/{sha}.pdf",
                sha256=sha,
                size_bytes=1,
                status="ready",
            )
        )
        await session.commit()
        return str(company.id)


async def _add_usage(
    factory: Factory,
    owner_id: str,
    company_id: str,
    *,
    cost: str,
    hours_ago: float = 0.0,
    input_tokens: int = 1000,
    output_tokens: int = 100,
) -> None:
    async with factory() as session:
        session.add(
            TokenUsage(
                owner_id=uuid.UUID(owner_id),
                company_id=uuid.UUID(company_id),
                kind="research",
                model="qwen-flash",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_cny=Decimal(cost),
                created_at=datetime.now(UTC) - timedelta(hours=hours_ago),
            )
        )
        await session.commit()


async def test_usage_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/usage")).status_code == 401


async def test_usage_empty_account_reports_defaults(client: AsyncClient) -> None:
    headers, _ = await _auth(client)
    body = (await client.get("/api/usage", headers=headers)).json()
    assert body["window_hours"] == 24
    assert body["spend_cny"] == 0
    assert (body["input_tokens"], body["output_tokens"]) == (0, 0)
    assert body["running_tasks"] == 0
    assert body["budget_cny"] > 0
    assert body["max_running"] > 0


async def test_usage_window_excludes_rows_older_than_24h(
    client: AsyncClient, session_factory: Factory
) -> None:
    headers, uid = await _auth(client)
    cid = await _make_company(session_factory, uid)
    await _add_usage(session_factory, uid, cid, cost="0.30", hours_ago=1)
    await _add_usage(session_factory, uid, cid, cost="9.00", hours_ago=25)
    body = (await client.get("/api/usage", headers=headers)).json()
    assert body["spend_cny"] == pytest.approx(0.3)
    assert body["input_tokens"] == 1000


async def test_research_rejected_when_over_budget(
    client: AsyncClient, session_factory: Factory
) -> None:
    headers, uid = await _auth(client)
    cid = await _make_company(session_factory, uid)
    await _add_usage(session_factory, uid, cid, cost="999.00")
    resp = await client.post(f"/api/companies/{cid}/research", headers=headers)
    assert resp.status_code == 429
    assert "预算" in resp.json()["detail"]


async def test_chat_rejected_when_over_budget(
    client: AsyncClient, session_factory: Factory
) -> None:
    headers, uid = await _auth(client)
    cid = await _make_company(session_factory, uid)
    await _add_usage(session_factory, uid, cid, cost="999.00")
    # 配额闸在流式开始前拒，模型不会被调用；override 只为绕开真 key 构造
    fake = FakeListChatModel(responses=["x"])
    app.dependency_overrides[get_chat_model] = lambda: fake
    resp = await client.post(
        f"/api/companies/{cid}/chat", headers=headers, json={"content": "问一句"}
    )
    assert resp.status_code == 429
    assert "预算" in resp.json()["detail"]


async def test_research_rejected_when_slots_full(
    client: AsyncClient, session_factory: Factory
) -> None:
    headers, uid = await _auth(client)
    cid = await _make_company(session_factory, uid)
    async with session_factory() as session:
        for st in ("queued", "running"):
            session.add(
                ResearchTask(
                    owner_id=uuid.UUID(uid), company_id=uuid.UUID(cid), status=st
                )
            )
        await session.commit()
    resp = await client.post(f"/api/companies/{cid}/research", headers=headers)
    assert resp.status_code == 429
    assert "上限" in resp.json()["detail"]
    body = (await client.get("/api/usage", headers=headers)).json()
    assert body["running_tasks"] == 2


async def test_usage_is_isolated_between_users(
    client: AsyncClient, session_factory: Factory
) -> None:
    _, uid_a = await _auth(client, "a@example.com")
    cid = await _make_company(session_factory, uid_a)
    await _add_usage(session_factory, uid_a, cid, cost="1.50")
    headers_b, _ = await _auth(client, "b@example.com")
    body = (await client.get("/api/usage", headers=headers_b)).json()
    assert body["spend_cny"] == 0
    assert body["running_tasks"] == 0
