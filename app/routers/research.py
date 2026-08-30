import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.config import get_settings
from app.db import get_session
from app.deps import get_arq, get_current_user
from app.limits import RESEARCH_PER_MIN, rate_limit, sse_gate
from app.models import Document, ResearchTask, User
from app.routers.companies import _get_own_company
from app.schemas import ResearchTaskOut, ResearchTaskSummary
from app.usage import enforce_quota

router = APIRouter(prefix="/api", tags=["research"])


@router.post(
    "/companies/{company_id}/research",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("research", RESEARCH_PER_MIN))],
)
async def start_research(
    company_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    arq: Annotated[ArqRedis, Depends(get_arq)],
) -> ResearchTaskSummary:
    company = await _get_own_company(company_id, user, session)
    await enforce_quota(session, user.id, need_slot=True)
    res = await session.execute(
        select(func.count())
        .select_from(Document)
        .where(Document.company_id == company.id, Document.status == "ready")
    )
    if res.scalar_one() == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该公司还没有已入库（ready）的文档，请先上传语料并等待入库完成",
        )
    task = ResearchTask(owner_id=user.id, company_id=company.id, status="queued")
    session.add(task)
    await session.commit()
    await arq.enqueue_job("run_research", str(task.id))
    await session.refresh(task)
    return ResearchTaskSummary.model_validate(task)


@router.get("/companies/{company_id}/research")
async def list_research(
    company_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ResearchTaskSummary]:
    company = await _get_own_company(company_id, user, session)
    res = await session.execute(
        select(ResearchTask)
        .where(ResearchTask.company_id == company.id)
        .order_by(ResearchTask.created_at.desc())
    )
    return [ResearchTaskSummary.model_validate(t) for t in res.scalars()]


async def _get_own_task(
    task_id: uuid.UUID, user: User, session: AsyncSession
) -> ResearchTask:
    task = await session.get(ResearchTask, task_id)
    if task is None or task.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Research task not found"
        )
    return task


@router.get("/research/{task_id}")
async def get_research(
    task_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchTaskOut:
    task = await _get_own_task(task_id, user, session)
    return ResearchTaskOut.model_validate(task)


@router.get("/research/{task_id}/events")
async def research_events(
    task_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    await _get_own_task(task_id, user, session)
    user_key = f"u:{user.id}"
    if not await sse_gate.acquire(user_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="同时在线的流式连接已达上限，请先关闭其他研究进度或对话窗口",
        )
    stream_key = f"research:events:{task_id}"
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)

    async def cleanup() -> None:
        await redis.aclose()
        await sse_gate.release(user_key)

    async def gen() -> AsyncIterator[str]:
        last_id = "0"
        while True:
            batches = await redis.xread({stream_key: last_id}, block=15000, count=100)
            if not batches:
                yield ": keepalive\n\n"
                continue
            for _stream, entries in batches:
                for entry_id, fields in entries:
                    last_id = entry_id
                    yield f"data: {json.dumps(fields, ensure_ascii=False)}\n\n"
                    if fields.get("node") in ("done", "failed"):
                        return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
        background=BackgroundTask(cleanup),  # 同 chat：断开也保证 aclose + 还槽
    )
