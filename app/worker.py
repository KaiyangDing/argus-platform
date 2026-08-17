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
from arq.connections import RedisSettings
from langchain_core.documents import Document as LCDocument
from sqlalchemy import update

from app.config import get_settings
from app.db import SessionFactory, engine
from app.ingest import append_to_index, load_pdf_pages, make_source_id, split_pages
from app.logs import setup_logging
from app.models import Document
from app.storage import get_bytes

setup_logging()
log = structlog.get_logger()

JOB_TIMEOUT = 30 * 60  # 解析+embedding 按分钟计，大文档给足
MAX_TRIES = 3


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
            raise ValueError("PDF 无可提取文本（扫描件或空文档）")

        await _set_status(doc_id, "embedding")
        added = await asyncio.to_thread(
            append_to_index, owner_key, company_key, chunks, ctx.get("embeddings")
        )

        await _set_status(doc_id, "ready")
        log.info("document_ready", document_id=document_id, chunks=added)
        return f"ready:{added}"
    except Exception as exc:
        if ctx.get("job_try", 1) >= MAX_TRIES:
            await _set_status(doc_id, "failed", error=f"{type(exc).__name__}: {exc}")
            log.warning("document_failed", document_id=document_id, error=str(exc))
            return f"failed:{type(exc).__name__}"
        raise


async def shutdown(ctx: dict) -> None:
    await engine.dispose()


class WorkerSettings:
    functions: ClassVar[list] = [ingest_document]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_shutdown = shutdown
    max_jobs = 1
    job_timeout = JOB_TIMEOUT
    max_tries = MAX_TRIES
