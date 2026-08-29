"""chat 端点集成测试：SSE token 级流式、两段式持久化、condense 历史接续、隔离。

图的行为面已在 test_chat.py 锁定，这里只测 API 编排：鉴权/隔离、消息
持久化顺序（user 请求内先落、assistant 流完独立事务落）、SSE 事件协议
（delta×N → done；零证据整段补发；异常走 error 事件）、跨请求的历史接续。
接线三件套见 _wire：_build_search 打桩（绕 MinIO 与真 embedding）、
SessionFactory 换 NullPool 工厂（模块级引擎跨测试事件循环会炸，worker
测试同型教训）、get_chat_model override 成 Fake。
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from langchain_core.documents import Document
from langchain_core.language_models import FakeListChatModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.routers.chat as chat_mod
from app.chat import NO_EVIDENCE_ANSWER
from app.deps import get_chat_model
from app.main import app
from app.models import Company
from app.retrieval import SearchFn

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
    async with factory() as session:
        company = Company(
            owner_id=uuid.UUID(owner_id), name=f"co-{uuid.uuid4().hex[:8]}"
        )
        session.add(company)
        await session.commit()
        return str(company.id)


def _doc(cid: str, text: str) -> Document:
    return Document(
        page_content=text,
        metadata={"chunk_id": cid, "source_id": "src", "page": 3, "company": "t"},
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: Factory,
    docs: list[Document],
    chat: FakeListChatModel,
) -> list[str]:
    """接线三件套；返回检索查询记录（断言 condense 是否生效）。"""
    queries: list[str] = []

    def fake_build(owner_key: str, company_key: str) -> SearchFn:
        def search(query: str, slug: str, k: int) -> list[Document]:
            queries.append(query)
            return docs

        return search

    monkeypatch.setattr(chat_mod, "_build_search", fake_build)
    monkeypatch.setattr(chat_mod, "SessionFactory", session_factory)
    app.dependency_overrides[get_chat_model] = lambda: chat
    return queries


async def _post_chat(
    client: AsyncClient, company_id: str, headers: dict[str, str], content: str
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    async with client.stream(
        "POST",
        f"/api/companies/{company_id}/chat",
        headers=headers,
        json={"content": content},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    return events


async def test_chat_turn_streams_and_persists(
    client: AsyncClient, session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id)
    queries = _wire(
        monkeypatch,
        session_factory,
        [_doc("c1", "2024 年营业收入 5 亿元")],
        FakeListChatModel(responses=["营业收入为 5 亿元 [1]。"]),
    )

    events = await _post_chat(client, company_id, headers, "2024 年营收多少")

    deltas = [e for e in events if e["type"] == "delta"]
    assert len(deltas) > 1  # token 级流式：多个 delta 而非整段一次
    assert "".join(str(e["text"]) for e in deltas) == "营业收入为 5 亿元 [1]。"
    done = events[-1]
    assert done["type"] == "done"
    message = done["message"]
    assert message["role"] == "assistant"
    assert [e["chunk_id"] for e in message["evidence"]] == ["c1"]
    assert queries == ["2024 年营收多少"]  # 首问无历史：原问直接检索

    listed = (
        await client.get(f"/api/companies/{company_id}/messages", headers=headers)
    ).json()
    assert [(m["role"], m["content"]) for m in listed] == [
        ("user", "2024 年营收多少"),
        ("assistant", "营业收入为 5 亿元 [1]。"),
    ]
    assert listed[1]["evidence"] is not None
    assert listed[1]["id"] == message["id"]  # done 事件里的就是落库的那条


async def test_second_turn_condenses_with_history(
    client: AsyncClient, session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id)
    # 同一 Fake 实例跨两次请求：轮1 无历史只耗 1 条；轮2 condense+answer 耗 2 条
    queries = _wire(
        monkeypatch,
        session_factory,
        [_doc("c1", "毛利率 12.3%")],
        FakeListChatModel(
            responses=["营收答 [1]。", "公司 2024 年毛利率", "毛利率为 12.3% [1]。"]
        ),
    )

    await _post_chat(client, company_id, headers, "2024 年营收多少")
    events = await _post_chat(client, company_id, headers, "那毛利率呢")

    assert queries == ["2024 年营收多少", "公司 2024 年毛利率"]  # 轮2 用改写查询检索
    text = "".join(str(e["text"]) for e in events if e["type"] == "delta")
    assert text == "毛利率为 12.3% [1]。"

    listed = (
        await client.get(f"/api/companies/{company_id}/messages", headers=headers)
    ).json()
    assert [m["role"] for m in listed] == ["user", "assistant", "user", "assistant"]


async def test_zero_evidence_sends_answer_as_single_delta(
    client: AsyncClient, session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id)
    _wire(
        monkeypatch,
        session_factory,
        [],
        FakeListChatModel(responses=["不应被调用"]),
    )

    events = await _post_chat(client, company_id, headers, "营收多少")

    deltas = [e for e in events if e["type"] == "delta"]
    assert [e["text"] for e in deltas] == [
        NO_EVIDENCE_ANSWER
    ]  # 无 token 可流，整段补发
    done = events[-1]
    assert done["message"]["content"] == NO_EVIDENCE_ANSWER  # 哨兵未浮出=没调 LLM
    assert done["message"]["evidence"] is None


async def test_error_surfaces_as_event_user_message_kept(
    client: AsyncClient, session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id)
    _wire(monkeypatch, session_factory, [], FakeListChatModel(responses=["x"]))

    def broken_build(owner_key: str, company_key: str) -> SearchFn:
        raise RuntimeError("索引载入失败")

    monkeypatch.setattr(chat_mod, "_build_search", broken_build)

    events = await _post_chat(client, company_id, headers, "问一句")

    assert events[-1]["type"] == "error"
    assert "RuntimeError" in str(events[-1]["detail"])
    listed = (
        await client.get(f"/api/companies/{company_id}/messages", headers=headers)
    ).json()
    # 两段式持久化：user 消息请求内已落库，assistant 因失败未落
    assert [m["role"] for m in listed] == ["user"]


async def test_messages_isolated_and_initially_empty(
    client: AsyncClient, session_factory: Factory
) -> None:
    headers_a, user_a = await _auth(client, "a@example.com")
    company_id = await _make_company(session_factory, user_a)
    resp = await client.get(f"/api/companies/{company_id}/messages", headers=headers_a)
    assert resp.status_code == 200
    assert resp.json() == []

    headers_b, _ = await _auth(client, "b@example.com")
    resp = await client.get(f"/api/companies/{company_id}/messages", headers=headers_b)
    assert resp.status_code == 404  # 404 而非 403：不泄露公司存在性


async def test_chat_requires_auth_and_validates_content(
    client: AsyncClient, session_factory: Factory
) -> None:
    resp = await client.post(
        f"/api/companies/{uuid.uuid4()}/chat", json={"content": "问"}
    )
    assert resp.status_code == 401

    # 422 分支也要 override：FastAPI 在 body 校验失败时依然会执行依赖，
    # 不挂桩会真跑 make_chat()，在无 API key 环境炸 500（零真凭据口径）
    app.dependency_overrides[get_chat_model] = lambda: FakeListChatModel(
        responses=["不应被调用"]
    )
    headers, user_id = await _auth(client)
    company_id = await _make_company(session_factory, user_id)
    resp = await client.post(
        f"/api/companies/{company_id}/chat", headers=headers, json={"content": "   "}
    )
    assert resp.status_code == 422  # 全空白经 strip 后为空串，长度校验拦下
