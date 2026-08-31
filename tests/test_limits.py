"""限流三层中的前两层（ADR-009）：HTTP 频率（fastapi-limiter）与 SSE 并发闸。

- fail-open 语义：FastAPILimiter 未 init 时 rate_limit 放行——其余全部 API
  测试不 init 就跑，本身就是该语义的持续回归；这里再补一条显式断言。
- 频率限流用真 Redis（db 1）+ 每测试唯一 prefix：init 是进程级全局态，
  fixture 拆干净（redis 置回 None），不让后续测试突然被限。
- SseGate 直接单测计数往返，再把上限压到 0 验证 chat 与研究进度流的
  429 出口（429 发生在流式开始前，能带真状态码）。
"""

import uuid

import pytest
from conftest import TEST_REDIS_URL
from fastapi_limiter import FastAPILimiter
from httpx import AsyncClient
from langchain_core.language_models import FakeListChatModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.limits import LOGIN_PER_MIN, REGISTER_PER_MIN, SseGate, sse_gate
from app.deps import get_chat_model
from app.domain.models import Company, ResearchTask
from app.main import app

Factory = async_sessionmaker[AsyncSession]
PASSWORD = "password-123"


async def _auth(client: AsyncClient) -> tuple[dict[str, str], str]:
    resp = await client.post(
        "/api/auth/register", json={"email": "alice@example.com", "password": PASSWORD}
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    me = await client.get("/api/auth/me", headers=headers)
    return headers, me.json()["id"]


async def _make_company(factory: Factory, owner_id: str) -> str:
    async with factory() as session:
        company = Company(
            owner_id=uuid.UUID(owner_id), name=f"co-{uuid.uuid4().hex[:8]}"
        )
        session.add(company)
        await session.commit()
        return str(company.id)


@pytest.fixture
async def limiter() -> object:
    """真 Redis 上激活限流；拆除时置回 None 恢复 fail-open。"""
    redis = Redis.from_url(TEST_REDIS_URL, encoding="utf8", decode_responses=True)
    await FastAPILimiter.init(redis, prefix=f"test-limit-{uuid.uuid4().hex[:8]}")
    yield None
    FastAPILimiter.redis = None
    await redis.aclose()


async def test_register_rate_limited_per_ip(client: AsyncClient, limiter: None) -> None:
    codes: list[int] = []
    for i in range(REGISTER_PER_MIN + 1):
        resp = await client.post(
            "/api/auth/register",
            json={"email": f"u{i}@example.com", "password": PASSWORD},
        )
        codes.append(resp.status_code)
    assert codes[:REGISTER_PER_MIN] == [201] * REGISTER_PER_MIN
    assert codes[-1] == 429
    assert "Retry-After" in resp.headers
    assert "频繁" in resp.json()["detail"]


async def test_rate_limit_fail_open_when_uninitialized(client: AsyncClient) -> None:
    assert FastAPILimiter.redis is None
    for _ in range(LOGIN_PER_MIN + 5):
        resp = await client.post(
            "/api/auth/login", json={"email": "ghost@example.com", "password": PASSWORD}
        )
        assert resp.status_code == 401  # 永远是业务 401，不是限流 429


async def test_sse_gate_counts_and_releases() -> None:
    gate = SseGate(max_concurrent=1, ttl_seconds=60)
    key = f"u:{uuid.uuid4()}"
    assert await gate.acquire(key)
    assert not await gate.acquire(key)
    await gate.release(key)
    assert await gate.acquire(key)
    await gate.release(key)


async def test_sse_gate_negative_drift_self_heals() -> None:
    gate = SseGate(max_concurrent=1, ttl_seconds=60)
    key = f"u:{uuid.uuid4()}"
    await gate.release(key)  # 多还一次：应删键归零而不是留下 -1 白赚一个槽
    assert await gate.acquire(key)
    assert not await gate.acquire(key)
    await gate.release(key)


async def test_chat_429_when_stream_slots_full(
    client: AsyncClient, session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id)
    fake = FakeListChatModel(responses=["x"])
    app.dependency_overrides[get_chat_model] = lambda: fake
    monkeypatch.setattr(sse_gate, "max_concurrent", 0)
    resp = await client.post(
        f"/api/companies/{company_id}/chat", headers=headers, json={"content": "hi"}
    )
    assert resp.status_code == 429
    assert "流式连接" in resp.json()["detail"]


async def test_research_events_429_when_stream_slots_full(
    client: AsyncClient, session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id)
    async with session_factory() as session:
        task = ResearchTask(
            owner_id=uuid.UUID(user_id),
            company_id=uuid.UUID(company_id),
            status="running",
        )
        session.add(task)
        await session.commit()
        task_id = str(task.id)
    monkeypatch.setattr(sse_gate, "max_concurrent", 0)
    resp = await client.get(f"/api/research/{task_id}/events", headers=headers)
    assert resp.status_code == 429
    assert "流式连接" in resp.json()["detail"]
