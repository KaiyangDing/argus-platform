"""research 端点集成测试：发起前置、列表、详情隔离、SSE 回放与终止。"""

import json
import uuid

from conftest import FakeArq
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models import Company, Document, ResearchTask

Factory = async_sessionmaker[AsyncSession]
EMAIL = "alice@example.com"
PASSWORD = "password-123"


async def _auth(client: AsyncClient, email: str = EMAIL) -> tuple[dict[str, str], str]:
    resp = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    me = await client.get("/api/auth/me", headers=headers)
    return headers, me.json()["id"]


async def _make_company(factory: Factory, owner_id: str, with_ready_doc: bool) -> str:
    async with factory() as session:
        company = Company(
            owner_id=uuid.UUID(owner_id), name=f"co-{uuid.uuid4().hex[:8]}"
        )
        session.add(company)
        await session.flush()
        if with_ready_doc:
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


async def test_start_research_without_ready_docs_409(
    client: AsyncClient, session_factory: Factory
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id, with_ready_doc=False)
    resp = await client.post(f"/api/companies/{company_id}/research", headers=headers)
    assert resp.status_code == 409


async def test_start_research_enqueues_task(
    client: AsyncClient, session_factory: Factory, arq_stub: FakeArq
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id, with_ready_doc=True)
    resp = await client.post(f"/api/companies/{company_id}/research", headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "queued"
    assert ("run_research", (body["id"],)) in arq_stub.jobs

    listed = await client.get(f"/api/companies/{company_id}/research", headers=headers)
    assert [t["id"] for t in listed.json()] == [body["id"]]


async def test_research_detail_isolated(
    client: AsyncClient, session_factory: Factory
) -> None:
    headers_a, user_a = await _auth(client, "a@example.com")
    company_id = await _make_company(session_factory, user_a, with_ready_doc=True)
    task_id = (
        await client.post(f"/api/companies/{company_id}/research", headers=headers_a)
    ).json()["id"]

    assert (
        await client.get(f"/api/research/{task_id}", headers=headers_a)
    ).status_code == 200
    headers_b, _ = await _auth(client, "b@example.com")
    assert (
        await client.get(f"/api/research/{task_id}", headers=headers_b)
    ).status_code == 404


async def test_events_sse_replays_and_terminates(
    client: AsyncClient, session_factory: Factory
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id, with_ready_doc=False)
    async with session_factory() as session:
        task = ResearchTask(
            owner_id=uuid.UUID(user_id),
            company_id=uuid.UUID(company_id),
            status="done",
        )
        session.add(task)
        await session.commit()
        task_id = str(task.id)

    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    key = f"research:events:{task_id}"
    await redis.xadd(key, {"node": "start", "detail": "开始"})
    await redis.xadd(key, {"node": "write", "detail": "报告撰写完成"})
    await redis.xadd(key, {"node": "done", "detail": "研究完成"})
    await redis.expire(key, 60)
    await redis.aclose()

    events: list[dict[str, str]] = []
    async with client.stream(
        "GET", f"/api/research/{task_id}/events", headers=headers
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    # done 事件触发服务端关流，aiter_lines 自然走完——回放序与写入序一致
    assert [e["node"] for e in events] == ["start", "write", "done"]
