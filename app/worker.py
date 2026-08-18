"""arq worker:文档入库任务。app 与 worker 同代码库不同进程（ADR-001/003）。

任务全链:queued → parsing → chunking → embedding → ready | failed。
管线本体是同步函数（ingest.py），逐段甩 asyncio.to_thread，不堵 worker 事件循环。
max_jobs=1:索引"载入-追加-回写"非原子，串行排除同公司并发交错。
重试策略:非终试异常向上抛、由 arq 自动重投（瞬时故障自愈）；
终试（job_try >= MAX_TRIES）落 failed + error 处置记录，等人工重试。
"""

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import ClassVar

import structlog
from arq import Retry
from arq.connections import RedisSettings
from langchain_core.documents import Document as LCDocument
from redis.asyncio import Redis
from sqlalchemy import func, update

from app.config import get_settings
from app.db import SessionFactory, engine
from app.ingest import (
    append_to_index,
    load_pdf_pages,
    make_embeddings,
    make_source_id,
    split_pages,
)
from app.llm import make_chat
from app.logs import setup_logging
from app.models import Company, Document, ResearchTask
from app.research import build_graph
from app.retrieval import make_company_search
from app.storage import get_bytes

setup_logging()
log = structlog.get_logger()

JOB_TIMEOUT = 30 * 60  # 解析+embedding 按分钟计，大文档给足
MAX_TRIES = 3


class NoTextError(ValueError):
    """PDF 无可提取文本：终态错误，重试改变不了结果。"""


def _load_pages(pdf_bytes: bytes, source_id: str, company_key: str) -> list[LCDocument]:
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / f"{source_id}.pdf"
        pdf_path.write_bytes(pdf_bytes)
        return load_pdf_pages(pdf_path, source_id, company_key)


async def _set_status(doc_id: uuid.UUID, status: str, error: str | None = None) -> None:
    async with SessionFactory() as session:
        await session.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(status=status, error=error)
        )
        await session.commit()


async def ingest_document(ctx: dict, document_id: str) -> str:
    doc_id = uuid.UUID(document_id)
    async with SessionFactory() as session:
        doc = await session.get(Document, doc_id)
        if doc is None:
            log.warning("document_missing", document_id=document_id)
            return "missing"
        if doc.status == "ready":
            return "already-ready"
        object_key = doc.object_key
        source_id = make_source_id(doc.filename, doc.sha256)
        owner_key = str(doc.owner_id)
        company_key = str(doc.company_id)

    try:
        await _set_status(doc_id, "parsing")
        pdf_bytes = await asyncio.to_thread(get_bytes, object_key)
        pages = await asyncio.to_thread(_load_pages, pdf_bytes, source_id, company_key)

        await _set_status(doc_id, "chunking")
        chunks = await asyncio.to_thread(split_pages, pages)
        if not chunks:
            raise NoTextError("PDF 无可提取文本（扫描件或空文档）")

        await _set_status(doc_id, "embedding")
        added = await asyncio.to_thread(
            append_to_index, owner_key, company_key, chunks, ctx.get("embeddings")
        )

        await _set_status(doc_id, "ready")
        log.info("document_ready", document_id=document_id, chunks=added)
        return f"ready:{added}"
    except NoTextError as exc:
        await _set_status(doc_id, "failed", error=f"NoTextError: {exc}")
        log.warning("document_failed", document_id=document_id, error=str(exc))
        return "failed:NoTextError"
    except Exception as exc:
        if ctx.get("job_try", 1) >= MAX_TRIES:
            await _set_status(doc_id, "failed", error=f"{type(exc).__name__}: {exc}")
            log.warning("document_failed", document_id=document_id, error=str(exc))
            return f"failed:{type(exc).__name__}"
        log.warning(
            "document_retrying",
            document_id=document_id,
            error=str(exc),
            job_try=ctx.get("job_try", 1),
        )
        raise Retry(defer=10) from exc


def _describe(node: str, payload: dict) -> str:
    if node == "supervisor":
        aspects = payload.get("aspects", [])
        names = "、".join(a["name"] for a in aspects)
        return f"拆解为 {len(aspects)} 个研究方面：{names}"
    if node == "researcher":
        findings = payload.get("findings") or [{}]
        f = findings[0]
        return f"「{f.get('name', '?')}」研究完成，证据 {len(f.get('evidence', []))} 条"
    if node == "merge":
        return f"证据归并完成：全局 {len(payload.get('evidence', []))} 条"
    if node == "write":
        return "报告撰写完成"
    return node


async def run_research(ctx: dict, task_id: str) -> str:
    tid = uuid.UUID(task_id)
    async with SessionFactory() as session:
        task = await session.get(ResearchTask, tid)
        if task is None:
            log.warning("research_missing", task_id=task_id)
            return "missing"
        if task.status == "done":
            return "already-done"
        owner_key = str(task.owner_id)
        company_key = str(task.company_id)
        company = await session.get(Company, task.company_id)
        company_name = company.name if company else company_key

    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    stream_key = f"research:events:{task_id}"

    async def emit(node: str, detail: str) -> None:
        await redis.xadd(stream_key, {"node": node, "detail": detail}, maxlen=1000)

    try:
        async with SessionFactory() as session:
            await session.execute(
                update(ResearchTask)
                .where(ResearchTask.id == tid)
                .values(status="running")
            )
            await session.commit()
        await emit("start", "研究任务开始")

        embeddings = ctx.get("embeddings") or make_embeddings()
        chat = ctx.get("chat") or make_chat()
        search = await asyncio.to_thread(
            make_company_search, owner_key, company_key, embeddings
        )
        graph = build_graph(
            chat=chat, search=search, struct_factory=ctx.get("struct_factory")
        ).compile()

        report: str | None = None
        evidence: list[dict[str, object]] | None = None
        state = {"company": company_name, "slug": company_key}
        async for chunk in graph.astream(state, stream_mode="updates"):
            for node, payload in chunk.items():
                data = payload if isinstance(payload, dict) else {}
                if node == "merge":
                    evidence = data.get("evidence")
                elif node == "write":
                    report = data.get("report")
                await emit(node, _describe(node, data))
        if report is None:
            raise RuntimeError("图执行未产出报告")

        async with SessionFactory() as session:
            await session.execute(
                update(ResearchTask)
                .where(ResearchTask.id == tid)
                .values(
                    status="done",
                    report_md=report,
                    evidence=evidence,
                    error=None,
                    finished_at=func.now(),
                )
            )
            await session.commit()
        await emit("done", "研究完成")
        await redis.expire(stream_key, 3600)
        log.info("research_done", task_id=task_id)
        return "done"
    except Exception as exc:
        if ctx.get("job_try", 1) >= MAX_TRIES:
            async with SessionFactory() as session:
                await session.execute(
                    update(ResearchTask)
                    .where(ResearchTask.id == tid)
                    .values(
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                        finished_at=func.now(),
                    )
                )
                await session.commit()
            await emit("failed", f"{type(exc).__name__}: {exc}")
            await redis.expire(stream_key, 3600)
            log.warning("research_failed", task_id=task_id, error=str(exc))
            return f"failed:{type(exc).__name__}"
        await emit("retrying", f"{type(exc).__name__}: {exc}")
        raise Retry(defer=10) from exc
    finally:
        await redis.aclose()


async def shutdown(ctx: dict) -> None:
    await engine.dispose()


class WorkerSettings:
    functions: ClassVar[list] = [ingest_document, run_research]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_shutdown = shutdown
    max_jobs = 1
    job_timeout = JOB_TIMEOUT
    max_tries = MAX_TRIES
