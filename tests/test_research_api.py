"""research 端点集成测试：发起前置、列表、详情隔离、SSE 回放与终止；
P3.5 批4：重试端点（failed 断点续跑入口 + queued 孤儿自愈）与入队韧性。"""

import json
import uuid

import pytest
from conftest import FakeArq
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.domain.models import Company, Document, ResearchTask

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


async def _set_task_status(
    factory: Factory, task_id: str, status_val: str, error: str | None = None
) -> None:
    async with factory() as session:
        await session.execute(
            update(ResearchTask)
            .where(ResearchTask.id == uuid.UUID(task_id))
            .values(status=status_val, error=error)
        )
        await session.commit()


async def _start_task(
    client: AsyncClient, factory: Factory, headers: dict[str, str], user_id: str
) -> str:
    company_id = await _make_company(factory, user_id, with_ready_doc=True)
    resp = await client.post(f"/api/companies/{company_id}/research", headers=headers)
    return resp.json()["id"]


async def test_retry_failed_research_requeues(
    client: AsyncClient, session_factory: Factory, arq_stub: FakeArq
) -> None:
    """failed → retry：状态回 queued、error 清空、同 _job_id 重新入队
    （worker 侧同 thread_id 从 checkpoint 断点续跑）。"""
    headers, user_id = await _auth(client)
    task_id = await _start_task(client, session_factory, headers, user_id)
    await _set_task_status(session_factory, task_id, "failed", "boom")

    resp = await client.post(f"/api/research/{task_id}/retry", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["error"] is None
    assert arq_stub.jobs.count(("run_research", (task_id,))) == 2  # 发起 1 + 重试 1


async def test_retry_queued_orphan_requeues(
    client: AsyncClient, session_factory: Factory, arq_stub: FakeArq
) -> None:
    """queued 也可重新入队：孤儿自愈（commit 后 enqueue 前崩=队列无任务）；
    正常排队任务误点由 _job_id 幂等兜底（FakeArq 不模拟去重，只锁 200）。"""
    headers, user_id = await _auth(client)
    task_id = await _start_task(client, session_factory, headers, user_id)

    resp = await client.post(f"/api/research/{task_id}/retry", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


async def test_retry_running_or_done_409(
    client: AsyncClient, session_factory: Factory
) -> None:
    headers, user_id = await _auth(client)
    task_id = await _start_task(client, session_factory, headers, user_id)
    for terminal in ("running", "done"):
        await _set_task_status(session_factory, task_id, terminal)
        resp = await client.post(f"/api/research/{task_id}/retry", headers=headers)
        assert resp.status_code == 409


async def test_retry_research_isolated(
    client: AsyncClient, session_factory: Factory
) -> None:
    headers_a, user_a = await _auth(client, "a@example.com")
    task_id = await _start_task(client, session_factory, headers_a, user_a)
    await _set_task_status(session_factory, task_id, "failed", "boom")
    headers_b, _ = await _auth(client, "b@example.com")
    resp = await client.post(f"/api/research/{task_id}/retry", headers=headers_b)
    assert resp.status_code == 404


async def test_start_research_enqueue_failure_marks_failed(
    client: AsyncClient,
    session_factory: Factory,
    arq_stub: FakeArq,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入队失败（Redis 瞬断）不留静默孤儿：任务落 failed + error 可见，
    前端 failed 卡的重试按钮就是恢复入口。"""
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id, with_ready_doc=True)

    async def boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(arq_stub, "enqueue_job", boom)
    resp = await client.post(f"/api/companies/{company_id}/research", headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "failed"
    assert "入队失败" in body["error"]


async def _make_done_task_with_events(
    client: AsyncClient,
    factory: Factory,
    events: list[tuple[str, str]],
) -> tuple[dict[str, str], str]:
    headers, user_id = await _auth(client)
    company_id = await _make_company(factory, user_id, with_ready_doc=False)
    async with factory() as session:
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
    for node, detail in events:
        await redis.xadd(key, {"node": node, "detail": detail})
    await redis.expire(key, 60)
    await redis.aclose()
    return headers, task_id


async def _consume_events(
    client: AsyncClient, task_id: str, headers: dict[str, str]
) -> list[str]:
    nodes: list[str] = []
    async with client.stream(
        "GET", f"/api/research/{task_id}/events", headers=headers
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                nodes.append(json.loads(line[5:])["node"])
    return nodes


async def test_events_sse_survives_mid_stream_failed(
    client: AsyncClient, session_factory: Factory
) -> None:
    """failed 不再是无条件终止符（P3.5 重试可复活）：中途 failed 之后的
    复活事件必须全量回放，流走到真正的尾部终态才关——旧版见 failed 即
    return，重试后的 resume/researcher/done 在回放里全部丢失。"""
    headers, task_id = await _make_done_task_with_events(
        client,
        session_factory,
        [
            ("start", "开始"),
            ("retrying", "熔断保护中"),
            ("failed", "三试耗尽"),
            ("start", "第 2 次尝试"),
            ("resume", "断点续跑"),
            ("done", "完成"),
        ],
    )
    nodes = await _consume_events(client, task_id, headers)
    assert nodes == ["start", "retrying", "failed", "start", "resume", "done"]


async def test_events_sse_closes_after_trailing_failed(
    client: AsyncClient, session_factory: Factory
) -> None:
    """failed 真在流尾（无人重试）：短窗探尾后关流，不挂死在 keepalive。"""
    headers, task_id = await _make_done_task_with_events(
        client,
        session_factory,
        [("start", "开始"), ("failed", "三试耗尽")],
    )
    nodes = await _consume_events(client, task_id, headers)
    assert nodes == ["start", "failed"]


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
