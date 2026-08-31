"""对话流式韧性（ADR-010）：断线续写与超时预算。

生产端（图执行+落库+记账）与 SSE 消费端解耦成独立 task 后，这里锁两条：
- 断开连接只取消消费端：生产端跑完，assistant 照常落库、usage 照常记账
  （含 missing_calls 语义），刷新页面即见完整回答；
- 块间超时中止：卡死的流以 error 事件收尾，不落半截 assistant——半截
  回答进历史会污染后续 condense 语境。
接线沿用 test_chat_api 的 _wire 三件套；Fake 的 sleep 让流有时间跨度。
"""

import asyncio
import json
import uuid

import pytest
from httpx import AsyncClient
from langchain_core.documents import Document
from langchain_core.language_models import FakeListChatModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.routers.chat as chat_mod
from app.deps import get_chat_model
from app.domain.models import Company, Message, TokenUsage
from app.engine.retrieval import SearchFn
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


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: Factory,
    chat: FakeListChatModel,
) -> None:
    docs = [
        Document(
            page_content="2024 年营业收入 5 亿元",
            metadata={"chunk_id": "c1", "source_id": "src", "page": 3, "company": "t"},
        )
    ]

    def fake_build(owner_key: str, company_key: str) -> SearchFn:
        def search(query: str, slug: str, k: int) -> list[Document]:
            return docs

        return search

    monkeypatch.setattr(chat_mod, "_build_search", fake_build)
    monkeypatch.setattr(chat_mod, "SessionFactory", session_factory)
    app.dependency_overrides[get_chat_model] = lambda: chat


async def _wait_for_producers() -> None:
    """等生产端后台任务收尾（断开后它应继续活到落库完成，而非被连坐取消）。"""
    for _ in range(400):
        if not chat_mod._producers:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("producer 任务超时未结束")


async def test_disconnect_persists_answer_and_usage(
    client: AsyncClient, session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id)
    # sleep 拉开 token 节奏：收到第一个事件就断开，生成仍在半途
    _wire(
        monkeypatch,
        session_factory,
        # 首问也过 condense（P3.4 检索式改写）：第 1 条给改写、第 2 条给回答
        FakeListChatModel(responses=["改写查询", "营收 5 亿 [1]"], sleep=0.02),
    )

    async with client.stream(
        "POST",
        f"/api/companies/{company_id}/chat",
        headers=headers,
        json={"content": "营收多少"},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                break  # 第一个 delta 到手即断线

    await _wait_for_producers()
    async with session_factory() as session:
        msgs = (
            (await session.execute(select(Message).order_by(Message.created_at)))
            .scalars()
            .all()
        )
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[1].content == "营收 5 亿 [1]"
        usage_rows = (await session.execute(select(TokenUsage))).scalars().all()
        assert len(usage_rows) == 1
        assert usage_rows[0].kind == "chat"
        assert usage_rows[0].ref_id == msgs[1].id
        # Fake 不带 usage_metadata：断线路径同样走「计缺不计零」而不是丢账
        # （condense + answer 两次调用都缺账）
        assert usage_rows[0].missing_calls == 2


async def test_gap_timeout_emits_error_and_drops_partial(
    client: AsyncClient, session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id)
    # 每 token 间隔 0.5s ≫ 压到 0.1s 的块间超时：必然中止
    _wire(
        monkeypatch,
        session_factory,
        FakeListChatModel(responses=["慢速回答"], sleep=0.5),
    )
    monkeypatch.setattr(chat_mod, "CHAT_GAP_TIMEOUT", 0.1)

    events: list[dict[str, object]] = []
    async with client.stream(
        "POST",
        f"/api/companies/{company_id}/chat",
        headers=headers,
        json={"content": "问一句"},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))

    assert events[-1]["type"] == "error"
    assert "超时" in str(events[-1]["detail"])
    await _wait_for_producers()
    async with session_factory() as session:
        msgs = (await session.execute(select(Message))).scalars().all()
        assert [m.role for m in msgs] == ["user"]  # 不落半截 assistant
