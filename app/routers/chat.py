"""追问对话 API：历史读取 + 追问回答（SSE token 级流式）。

对话图在 API 进程内联执行（不走 worker，ADR-007）；P3.2 起图的宿主从
SSE 生成器挪进独立 asyncio.Task（ADR-010）：
- 断线续写：客户端断开只取消消费端 gen()，生产端任务继续跑完并照常
  落库与记账，刷新页面即见完整回答——断开不再等于白烧 token；
- 超时预算由生产端自守（断开后没有消费端替它计时）：块间 120s、全程
  300s；超时按失败处理（error 事件、不落半截 assistant——半截回答进
  历史会污染后续 condense 语境）。
持久化两段式不变：user 消息请求事务先落；assistant 由生产端在流完后以
SessionFactory 独立短事务落。SSE 事件协议不变：delta×N → done(message)；
异常 error 事件。前端零改动。
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from langchain_core.language_models import BaseChatModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat import HISTORY_LIMIT, ChatMsg, build_chat_graph
from app.db import SessionFactory, get_session
from app.deps import get_chat_model, get_current_user
from app.ingest import make_embeddings
from app.limits import CHAT_PER_MIN, rate_limit, sse_gate
from app.llm import CHAT_MODEL
from app.models import Message, User
from app.retrieval import SearchFn, make_company_search
from app.routers.companies import _get_own_company
from app.schemas import ChatIn, MessageOut
from app.usage import UsageCollector, enforce_quota, record_usage

log = structlog.get_logger()

router = APIRouter(prefix="/api", tags=["chat"])

# 阈值推导见 ADR-010：单调用 30s（get_chat_model）× RetryPolicy 3 + 退避
# ≈ 单节点最长合法静默 ~92s → 块间 120；三节点全打满 ≈270s + 索引载入 → 300
CHAT_GAP_TIMEOUT = 120.0
CHAT_TOTAL_TIMEOUT = 300.0

# 生产端与连接解耦后需要强引用防中途被 GC 回收（asyncio 只弱引用 task）
_producers: set[asyncio.Task[None]] = set()


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


async def _record_failed_usage(
    owner_key: str, company_key: str, usage: UsageCollector
) -> None:
    """失败/超时路径的记账：钱已经烧了，不记就是给失败开免费通道。"""
    try:
        async with SessionFactory() as db:
            await record_usage(
                db,
                owner_id=uuid.UUID(owner_key),
                company_id=uuid.UUID(company_key),
                kind="chat",
                ref_id=None,
                collector=usage,
            )
    except Exception:
        # 记账失败不能再抛：生产端此刻只剩收尾，.exception 带 traceback
        log.exception("usage_record_failed", company_id=company_key)


async def _produce(
    queue: asyncio.Queue[dict[str, object] | None],
    chat_model: BaseChatModel,
    owner_key: str,
    company_key: str,
    company_name: str,
    question: str,
    history: list[ChatMsg],
) -> None:
    """图执行与持久化的宿主：把 SSE 事件推入 queue，与连接生死解耦（ADR-010）。"""
    usage = UsageCollector(CHAT_MODEL)
    deadline = time.monotonic() + CHAT_TOTAL_TIMEOUT
    try:
        # 索引载入不在块间超时内（本地 IO，P3.4 迁 pgvector 后消失），受总预算外的
        # 事实上限约束：MinIO 读 + 反序列化，分钟级语料秒级完成
        search = await asyncio.to_thread(_build_search, owner_key, company_key)
        graph = build_chat_graph(chat_model, search).compile()
        answer = ""
        condensed = ""
        evidence: list[dict[str, object]] = []
        streamed = False
        stream = graph.astream(
            {
                "company": company_name,
                "slug": company_key,
                "history": history,
                "question": question,
            },
            {"callbacks": [usage]},
            stream_mode=["messages", "updates"],
        )
        try:
            while True:
                remaining = min(CHAT_GAP_TIMEOUT, deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError("chat_total_budget")
                try:
                    mode, payload = await asyncio.wait_for(anext(stream), remaining)
                except StopAsyncIteration:
                    break
                if mode == "messages":
                    chunk, meta = payload
                    text = str(chunk.content)
                    if meta.get("langgraph_node") == "answer" and text:
                        streamed = True
                        await queue.put({"type": "delta", "text": text})
                elif "condense" in payload:
                    condensed = payload["condense"]["condensed"]
                elif "retrieve" in payload:
                    evidence = payload["retrieve"]["evidence"]
                elif "answer" in payload:
                    answer = payload["answer"]["answer"]
        finally:
            await stream.aclose()
        if not streamed:
            # 零证据固定文案不过 LLM、无 token 可流，整段补发一个 delta
            await queue.put({"type": "delta", "text": answer})
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
            await record_usage(
                db,
                owner_id=uuid.UUID(owner_key),
                company_id=uuid.UUID(company_key),
                kind="chat",
                ref_id=msg.id,
                collector=usage,
            )
        log.info(
            "chat_turn",
            company_id=company_key,
            condensed=condensed,
            evidence=len(evidence),
            chars=len(answer),
        )
        await queue.put({"type": "done", "message": out.model_dump(mode="json")})
    except TimeoutError:
        log.warning("chat_timeout", company_id=company_key, budget=CHAT_TOTAL_TIMEOUT)
        await queue.put(
            {"type": "error", "detail": "回答超时，已中止本轮生成；请稍后重试"}
        )
        await _record_failed_usage(owner_key, company_key, usage)
    except Exception as exc:
        # 宽捕获是设计内：生产端没有请求上下文可抛，错误只能走事件面；
        # .exception 带 traceback 且构成 BLE001 豁免（.warning 不构成）
        log.exception("chat_failed", company_id=company_key, error=str(exc))
        await queue.put({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
        await _record_failed_usage(owner_key, company_key, usage)
    finally:
        await queue.put(None)


@router.post(
    "/companies/{company_id}/chat",
    dependencies=[Depends(rate_limit("chat", CHAT_PER_MIN))],
)
async def chat(
    company_id: uuid.UUID,
    body: ChatIn,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    chat_model: Annotated[BaseChatModel, Depends(get_chat_model)],
) -> StreamingResponse:
    company = await _get_own_company(company_id, user, session)
    await enforce_quota(session, user.id, need_slot=False)
    user_key = f"u:{user.id}"
    if not await sse_gate.acquire(user_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="同时在线的流式连接已达上限，请先关闭其他研究进度或对话窗口",
        )
    # 抢到槽之后任何一步失败都要还槽再抛；裸 raise 属重抛（BLE001 豁免）
    try:
        res = await session.execute(
            select(Message)
            .where(Message.company_id == company.id)
            .order_by(Message.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
        history: list[ChatMsg] = [
            {"role": m.role, "content": m.content}
            for m in reversed(list(res.scalars()))
        ]
        session.add(
            Message(
                owner_id=user.id,
                company_id=company.id,
                role="user",
                content=body.content,
            )
        )
        await session.commit()

        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        producer = asyncio.create_task(
            _produce(
                queue,
                chat_model,
                str(user.id),
                str(company.id),
                company.name,
                body.content,
                history,
            )
        )
        _producers.add(producer)
        producer.add_done_callback(_producers.discard)
    except BaseException:
        await sse_gate.release(user_key)
        raise

    async def gen() -> AsyncIterator[str]:
        try:
            while (event := await queue.get()) is not None:
                yield _sse(event)
        finally:
            # 断开只走到这：还槽即止，生产端继续活到落库完成
            await sse_gate.release(user_key)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
