import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.core.config import get_settings
from app.core.db import get_session
from app.core.limits import RESEARCH_PER_MIN, rate_limit, sse_gate
from app.deps import get_arq, get_current_user
from app.domain.models import Document, ResearchTask, User
from app.domain.schemas import ResearchTaskOut, ResearchTaskSummary
from app.domain.usage import enforce_quota
from app.routers.companies import _get_own_company

router = APIRouter(prefix="/api", tags=["research"])
log = structlog.get_logger()


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
    try:
        await arq.enqueue_job(
            "run_research", str(task.id), _job_id=f"research:{task.id}"
        )
    except Exception:
        # Redis 瞬断：任务已落库，标 failed 让用户可见可重试，不留静默孤儿
        log.exception("research_enqueue_failed", task_id=str(task.id))
        task.status = "failed"
        task.error = "任务入队失败，请点重试"
        await session.commit()
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


@router.post(
    "/research/{task_id}/retry",
    dependencies=[Depends(rate_limit("research", RESEARCH_PER_MIN))],
)
async def retry_research(
    task_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    arq: Annotated[ArqRedis, Depends(get_arq)],
) -> ResearchTaskSummary:
    task = await _get_own_task(task_id, user, session)
    if task.status not in ("failed", "queued"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="只有 failed（断点续跑）或卡在 queued 的任务可以重新入队",
        )
    # failed 才要新槽；queued 已在并发计数里，再要一个会自己挤自己
    await enforce_quota(session, user.id, need_slot=task.status == "failed")
    task.status = "queued"
    task.error = None
    await session.commit()
    try:
        # _job_id 幂等：同任务已在队/在跑时 no-op——孤儿误判与双击都无害
        await arq.enqueue_job(
            "run_research", str(task.id), _job_id=f"research:{task.id}"
        )
    except Exception:
        log.exception("research_enqueue_failed", task_id=str(task.id))
        task.status = "failed"
        task.error = "任务入队失败，请点重试"
        await session.commit()
    await session.refresh(task)
    return ResearchTaskSummary.model_validate(task)


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
        ended = False  # 上一条已吐出的事件是否终态（done/failed）
        while True:
            # 终态后短窗探尾：failed 可能被重试复活（P3.5），流里若还有
            # 后续事件必须继续吐；1s 静默才确认真到流尾、关流还槽
            block_ms = 1000 if ended else 15000
            batches = await redis.xread(
                {stream_key: last_id}, block=block_ms, count=100
            )
            if not batches:
                if ended:
                    return
                yield ": keepalive\n\n"
                continue
            for _stream, entries in batches:
                for entry_id, fields in entries:
                    last_id = entry_id
                    yield f"data: {json.dumps(fields, ensure_ascii=False)}\n\n"
                    ended = fields.get("node") in ("done", "failed")

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
        background=BackgroundTask(cleanup),  # 同 chat：断开也保证 aclose + 还槽
    )
