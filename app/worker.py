"""arq worker:文档入库任务。app 与 worker 同代码库不同进程（ADR-001/003）。

任务全链:queued → parsing → chunking → embedding → ready | failed。
管线本体是同步函数（ingest.py），逐段甩 asyncio.to_thread，不堵 worker 事件循环。
max_jobs=4：chunks 行级 INSERT + ON CONFLICT 天然并发安全（P3.4 迁 pgvector
后旧索引对象的"载入-追加-回写"竞态结构性消失），入库与研究并行不悖。
重试策略:非终试异常向上抛、由 arq 自动重投（瞬时故障自愈）；
终试（job_try >= MAX_TRIES）落 failed + error 处置记录，等人工重试。
"""

import asyncio
import sys
import tempfile
import time
import uuid
from itertools import batched
from pathlib import Path
from typing import ClassVar

import structlog
from arq import Retry
from arq.connections import RedisSettings
from arq.worker import func as arq_func
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.documents import Document as LCDocument
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pybreaker import CircuitBreakerError
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.breakers import LLM_RESET_TIMEOUT
from app.core.config import get_settings
from app.core.db import SessionFactory, engine, sync_engine
from app.core.logs import setup_logging
from app.core.storage import get_bytes
from app.domain.models import Chunk, Company, Document, ResearchTask
from app.domain.usage import UsageCollector, record_usage
from app.engine.ingest import (
    annotate_page_sections,
    corpus_profile,
    embed_chunks,
    load_pdf_pages,
    make_embeddings,
    make_source_id,
    split_pages,
    tokenize_for_search,
)
from app.engine.llm import CHAT_MODEL, make_chat
from app.engine.research import build_graph
from app.engine.retrieval import make_company_search

if sys.platform == "win32":
    # psycopg async（checkpointer）不支持 Windows 默认的 Proactor 循环；
    # arq 没有 loop_factory 入口，模块 import 时全局换 Selector——
    # asyncpg/redis 两种循环皆可，psycopg 只认这个。Linux 部署无此事。
    # py3.14 起 policy 系统弃用（3.16 移除），届时随 arq 新口径迁移
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
setup_logging()
log = structlog.get_logger()

JOB_TIMEOUT = 30 * 60  # 解析+embedding 按分钟计，大文档给足
MAX_TRIES = 3
RETRY_DEFER = 10  # 瞬态错误重投间隔
BREAKER_RETRY_DEFER = LLM_RESET_TIMEOUT + 30  # 熔断 open：重投等冷却窗过后


def _retry_defer(exc: BaseException) -> int:
    """熔断 open 时 10s 重投必再撞闸（闸至少开 60s）——三试半分钟内烧完、
    任务白落 failed。拉长到冷却窗之后，重投的第一个调用恰好当探针。"""
    return BREAKER_RETRY_DEFER if isinstance(exc, CircuitBreakerError) else RETRY_DEFER


def _error_detail(exc: BaseException) -> str:
    """用户可见错误文案：熔断器的英文内部话不穿到时间线与处置记录。"""
    if isinstance(exc, CircuitBreakerError):
        return "模型服务暂时不可用（熔断保护中）"
    return f"{type(exc).__name__}: {exc}"


class NoTextError(ValueError):
    """PDF 无可提取文本：终态错误，重试改变不了结果。"""


def _load_pages(pdf_bytes: bytes, source_id: str, company_key: str) -> list[LCDocument]:
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / f"{source_id}.pdf"
        pdf_path.write_bytes(pdf_bytes)
        pages = load_pdf_pages(pdf_path, source_id, company_key)
    annotate_page_sections(pages)  # 跨页章节面包屑：只进 metadata，免重嵌
    return pages


async def _set_status(doc_id: uuid.UUID, status: str, error: str | None = None) -> None:
    async with SessionFactory() as session:
        await session.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(status=status, error=error)
        )
        await session.commit()


async def store_chunks(
    owner_id: uuid.UUID,
    company_id: uuid.UUID,
    document_id: uuid.UUID,
    chunks: list[LCDocument],
    vectors: list[list[float]],
) -> int:
    """块与向量入 chunks 表；返回实际插入数。

    幂等靠 (company_id, chunk_id) 唯一约束 + ON CONFLICT DO NOTHING：
    旧 MinIO 索引的幂等是读全量 jsonl 查 source_id（应用层、有竞态窗口），
    换成数据库约束后行级兜底、并发安全——max_jobs 敢放开的根据。
    """
    inserted = 0
    async with SessionFactory() as session:
        for batch in batched(zip(chunks, vectors, strict=True), 100):
            rows = [
                {
                    "owner_id": owner_id,
                    "company_id": company_id,
                    "document_id": document_id,
                    "chunk_id": doc.metadata["chunk_id"],
                    "source_id": doc.metadata["source_id"],
                    "page": doc.metadata["page"],
                    "seq": doc.metadata["seq"],
                    "section": doc.metadata.get("section", ""),
                    "text": doc.page_content,
                    "text_tokens": func.to_tsvector(
                        "simple", tokenize_for_search(doc.page_content)
                    ),
                    "embedding": vec,
                }
                for doc, vec in batch
            ]
            stmt = (
                pg_insert(Chunk)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["company_id", "chunk_id"])
            )
            res = await session.execute(stmt)
            inserted += res.rowcount
        await session.commit()
    return inserted


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
        vectors = await asyncio.to_thread(embed_chunks, chunks, ctx.get("embeddings"))
        added = await store_chunks(
            uuid.UUID(owner_key), uuid.UUID(company_key), doc_id, chunks, vectors
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
            await _set_status(doc_id, "failed", error=_error_detail(exc))
            log.warning("document_failed", document_id=document_id, error=str(exc))
            return f"failed:{type(exc).__name__}"
        log.warning(
            "document_retrying",
            document_id=document_id,
            error=str(exc),
            job_try=ctx.get("job_try", 1),
        )
        raise Retry(defer=_retry_defer(exc)) from exc


class LLMCallLogger(BaseCallbackHandler):
    """调用级观测：每次 LLM 调用的节点/耗时/错误打进 worker 日志。

    看不到 openai 客户端内部的静默重试，但「start 后迟迟无 end」本身就是
    正在重试/慢跑的信号——黑盒静默事故（v0.2 首跑 19 分钟无声）的解药。
    """

    def __init__(self) -> None:
        self._started: dict[object, tuple[str, float]] = {}

    def on_chat_model_start(
        self,
        _serialized: dict,
        _messages: list,
        *,
        run_id: object,
        metadata: dict | None = None,
        **_kw: object,
    ) -> None:
        node = (metadata or {}).get("langgraph_node", "?")
        self._started[run_id] = (node, time.monotonic())
        log.info("llm_call_start", node=node)

    def on_llm_end(self, _response: object, *, run_id: object, **_kw: object) -> None:
        node, t0 = self._started.pop(run_id, ("?", time.monotonic()))
        log.info("llm_call_end", node=node, seconds=round(time.monotonic() - t0, 1))

    def on_llm_error(
        self, error: BaseException, *, run_id: object, **_kw: object
    ) -> None:
        node, t0 = self._started.pop(run_id, ("?", time.monotonic()))
        log.warning(
            "llm_call_error",
            node=node,
            seconds=round(time.monotonic() - t0, 1),
            error=str(error),
        )


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
    if node == "review":
        followups = payload.get("followup_aspects") or []
        if followups:
            names = "、".join(a["name"] for a in followups)
            return f"复审：补派 {len(followups)} 项补充研究（{names}）"
        return "复审通过，无补派"
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
        result = await session.execute(
            select(Document.filename).where(
                Document.company_id == task.company_id,
                Document.status == "ready",
            )
        )
        profile = corpus_profile(list(result.scalars()))

    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    stream_key = f"research:events:{task_id}"

    async def emit(node: str, detail: str) -> None:
        await redis.xadd(stream_key, {"node": node, "detail": detail}, maxlen=1000)

    usage = UsageCollector(CHAT_MODEL)
    try:
        async with SessionFactory() as session:
            await session.execute(
                update(ResearchTask)
                .where(ResearchTask.id == tid)
                .values(status="running")
            )
            await session.commit()
        attempt = ctx.get("job_try", 1)
        await emit(
            "start",
            f"研究任务开始（第 {attempt} 次尝试）" if attempt > 1 else "研究任务开始",
        )

        embeddings = ctx.get("embeddings") or make_embeddings()
        chat = ctx.get("chat") or make_chat()
        search = await asyncio.to_thread(
            make_company_search, owner_key, company_key, embeddings
        )

        state = {
            "company": company_name,
            "slug": company_key,
            "corpus_profile": profile,
        }
        async with AsyncPostgresSaver.from_conn_string(
            get_settings().database_url
        ) as saver:
            graph = build_graph(
                chat=chat, search=search, struct_factory=ctx.get("struct_factory")
            ).compile(checkpointer=saver)
            config = {
                "callbacks": [LLMCallLogger(), usage],
                "configurable": {"thread_id": task_id},
            }
            snapshot = await graph.aget_state(config)
            resumed = bool(snapshot.values)
            if resumed and not snapshot.next:
                # 上次图已跑完、done 落库前崩的毫秒级窗口：直接取终态不重执行
                # （幂等窗口审计，与 P1.4「索引已写 ready 未置」同款方法论）
                final_values = snapshot.values
            else:
                if resumed:
                    await emit("resume", "从断点续跑：已完成节点不重算")
                async for chunk in graph.astream(
                    None if resumed else state, config, stream_mode="updates"
                ):
                    for node, payload in chunk.items():
                        data = payload if isinstance(payload, dict) else {}
                        await emit(node, _describe(node, data))
                final_values = (await graph.aget_state(config)).values
            report = final_values.get("report")
            evidence = final_values.get("evidence")
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
            # 成功即删 checkpoint（省空间）；失败/取消保留——arq 重投与
            # 手动重试都从断点续跑（批4 的 retry 端点吃的就是这份保留）
            await saver.adelete_thread(task_id)
        await emit("done", "研究完成")
        await redis.expire(stream_key, 3600)
        log.info("research_done", task_id=task_id)
        return "done"
    except asyncio.CancelledError:
        await emit("retrying", "任务被取消（超时或 worker 重启），等待自动重跑")
        raise
    except Exception as exc:
        if ctx.get("job_try", 1) >= MAX_TRIES:
            async with SessionFactory() as session:
                await session.execute(
                    update(ResearchTask)
                    .where(ResearchTask.id == tid)
                    .values(
                        status="failed",
                        error=_error_detail(exc),
                        finished_at=func.now(),
                    )
                )
                await session.commit()
            await emit("failed", _error_detail(exc))
            await redis.expire(stream_key, 3600)
            log.warning("research_failed", task_id=task_id, error=str(exc))
            return f"failed:{type(exc).__name__}"
        await emit("retrying", _error_detail(exc))
        raise Retry(defer=_retry_defer(exc)) from exc
    finally:
        await redis.aclose()
        try:
            async with SessionFactory() as session:
                await record_usage(
                    session,
                    owner_id=uuid.UUID(owner_key),
                    company_id=uuid.UUID(company_key),
                    kind="research",
                    ref_id=tid,
                    collector=usage,
                )
        except Exception:
            # 记账失败不能吃掉任务本身的结果或异常；.exception 带 traceback（BLE001 豁免）
            log.exception("usage_record_failed", task_id=task_id)


async def startup(ctx: dict) -> None:
    # checkpoints 表由 saver 自管迁移（不走 alembic：第三方库私有 schema，
    # 与业务表解耦）；重复调用安全，每次启动确保到位
    async with AsyncPostgresSaver.from_conn_string(
        get_settings().database_url
    ) as saver:
        await saver.setup()


async def shutdown(ctx: dict) -> None:
    await engine.dispose()
    sync_engine.dispose()


class WorkerSettings:
    functions: ClassVar[list] = [
        ingest_document,
        arq_func(run_research, timeout=60 * 60),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 4
    keep_result = 0  # 不留 result key：_job_id 去重才能在任务完成后立即重投
    job_timeout = JOB_TIMEOUT
    max_tries = MAX_TRIES
