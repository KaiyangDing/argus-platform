"""追问对话 API：历史读取 + 追问回答（SSE token 级流式）。

对话图在 API 进程内联执行（不走 worker，ADR-007）：追问是秒级交互，
排队徒增延迟；索引载入是同步 IO，甩 to_thread 不堵事件循环。
持久化两段式：user 消息在请求事务内先落（流式开始前已可见）；
assistant 消息在流式完成后以 SessionFactory 开独立短事务落——不复用
请求级 session：不依赖框架对 yield 依赖的清理时序（FastAPI 历史上
变过），也不让 DB 会话横跨分钟级流式期（与 worker 状态写法同型）。
SSE 事件协议：{"type":"delta","text"} × N → {"type":"done","message"}；
异常时 {"type":"error","detail"}——流式已开、HTTP 状态码改不了，
错误只能走事件面。客户端中途断开=生成器被取消，assistant 不落库，
用户重问即可（v1 可接受边界）。
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from langchain_core.language_models import BaseChatModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat import HISTORY_LIMIT, ChatMsg, build_chat_graph
from app.db import SessionFactory, get_session
from app.deps import get_chat_model, get_current_user
from app.ingest import make_embeddings
from app.models import Message, User
from app.retrieval import SearchFn, make_company_search
from app.routers.companies import _get_own_company
from app.schemas import ChatIn, MessageOut

log = structlog.get_logger()

router = APIRouter(prefix="/api", tags=["chat"])


def _build_search(owner_key: str, company_key: str) -> SearchFn:
    """索引载入 + SearchFn 组装（测试在此打桩，绕开 MinIO 与真 embedding）。"""
    return make_company_search(owner_key, company_key, make_embeddings())


def _sse(data: dict[str, object]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/companies/{company_id}/messages")
async def list_messages(
    company_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[MessageOut]:
    company = await _get_own_company(company_id, user, session)
    res = await session.execute(
        select(Message)
        .where(Message.company_id == company.id)
        .order_by(Message.created_at)
    )
    return [MessageOut.model_validate(m) for m in res.scalars()]


@router.post("/companies/{company_id}/chat")
async def chat(
    company_id: uuid.UUID,
    body: ChatIn,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    chat_model: Annotated[BaseChatModel, Depends(get_chat_model)],
) -> StreamingResponse:
    company = await _get_own_company(company_id, user, session)
    res = await session.execute(
        select(Message)
        .where(Message.company_id == company.id)
        .order_by(Message.created_at.desc())
        .limit(HISTORY_LIMIT)
    )
    history: list[ChatMsg] = [
        {"role": m.role, "content": m.content} for m in reversed(list(res.scalars()))
    ]
    session.add(
        Message(
            owner_id=user.id, company_id=company.id, role="user", content=body.content
        )
    )
    await session.commit()

    owner_key = str(user.id)
    company_key = str(company.id)
    company_name = company.name
    question = body.content

    async def gen() -> AsyncIterator[str]:
        try:
            search = await asyncio.to_thread(_build_search, owner_key, company_key)
            graph = build_chat_graph(chat_model, search).compile()
            answer = ""
            condensed = ""
            evidence: list[dict[str, object]] = []
            streamed = False
            async for mode, payload in graph.astream(
                {
                    "company": company_name,
                    "slug": company_key,
                    "history": history,
                    "question": question,
                },
                stream_mode=["messages", "updates"],
            ):
                if mode == "messages":
                    chunk, meta = payload
                    text = str(chunk.content)
                    if meta.get("langgraph_node") == "answer" and text:
                        streamed = True
                        yield _sse({"type": "delta", "text": text})
                elif "condense" in payload:
                    condensed = payload["condense"]["condensed"]
                elif "retrieve" in payload:
                    evidence = payload["retrieve"]["evidence"]
                elif "answer" in payload:
                    answer = payload["answer"]["answer"]
            if not streamed:
                # 零证据固定文案不过 LLM、无 token 可流，整段补发一个 delta
                yield _sse({"type": "delta", "text": answer})
            async with SessionFactory() as db:
                msg = Message(
                    owner_id=uuid.UUID(owner_key),
                    company_id=uuid.UUID(company_key),
                    role="assistant",
                    content=answer,
                    evidence=evidence or None,
                )
                db.add(msg)
                await db.commit()
                await db.refresh(msg)
                out = MessageOut.model_validate(msg)
            log.info(
                "chat_turn",
                company_id=company_key,
                condensed=condensed,
                evidence=len(evidence),
                chars=len(answer),
            )
            yield _sse({"type": "done", "message": out.model_dump(mode="json")})
        except Exception as exc:
            # 宽捕获是设计内：流式已开、状态码改不了，错误只能走事件面；
            # .exception 带 traceback 且构成 BLE001 豁免（.warning 不构成）
            log.exception("chat_failed", company_id=company_key, error=str(exc))
            yield _sse({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
