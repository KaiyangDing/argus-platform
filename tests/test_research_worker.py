"""run_research 全链测试：ctx 三缝全 Fake，真 PG + 真 Redis，零 LLM 花费。

零证据路径连 MinIO 索引都不需要（空索引→恒空 search→图走占位分支），
有证据路径先 append 索引再跑，断言 evidence 落库与事件序列完整。
"""

import uuid

import pytest
from langchain_core.documents import Document as LCDocument
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.language_models import FakeListChatModel
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.worker as worker_mod
from app.config import get_settings
from app.ingest import append_to_index, split_pages
from app.models import Company, ResearchTask, User
from app.research import AspectPlan, AspectSpec, QueryList

Factory = async_sessionmaker[AsyncSession]

_PLAN = AspectPlan(
    aspects=[
        AspectSpec(name="财务表现", focus="营收如何"),
        AspectSpec(name="公司治理", focus="股权如何"),
    ]
)


class _StructStub:
    def __init__(self, value: object) -> None:
        self.value = value

    def invoke(self, _msgs: object) -> object:
        return self.value


def _struct_factory(model_cls: type[BaseModel]) -> _StructStub:
    if model_cls is AspectPlan:
        return _StructStub(_PLAN)
    if model_cls is QueryList:
        return _StructStub(QueryList(queries=["查询一", "查询二"]))
    raise AssertionError(f"未预期的结构化请求：{model_cls}")


async def _make_task(factory: Factory) -> tuple[str, str, str]:
    """返回 (task_id, owner_id, company_id)。"""
    async with factory() as session:
        user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        company = Company(owner_id=user.id, name=f"研究对象-{uuid.uuid4().hex[:6]}")
        session.add(company)
        await session.flush()
        task = ResearchTask(owner_id=user.id, company_id=company.id, status="queued")
        session.add(task)
        await session.commit()
        return str(task.id), str(user.id), str(company.id)


async def _task_row(factory: Factory, task_id: str) -> ResearchTask:
    async with factory() as session:
        task = await session.get(ResearchTask, uuid.UUID(task_id))
        assert task is not None
        return task


async def _event_nodes(task_id: str) -> list[str]:
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    entries = await redis.xrange(f"research:events:{task_id}")
    await redis.aclose()
    return [fields["node"] for _id, fields in entries]


async def test_run_research_zero_evidence_to_done(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _owner, _company = await _make_task(session_factory)
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)

    ctx = {
        "job_try": 1,
        "embeddings": DeterministicFakeEmbedding(size=1024),
        "chat": FakeListChatModel(responses=["边界：语料为空"]),
        "struct_factory": _struct_factory,
    }
    result = await worker_mod.run_research(ctx, task_id)

    assert result == "done"
    task = await _task_row(session_factory, task_id)
    assert task.status == "done"
    assert task.report_md is not None
    assert "证据不足" in task.report_md
    assert task.evidence == []
    assert task.finished_at is not None

    nodes = await _event_nodes(task_id)
    assert nodes[0] == "start"
    assert nodes[-1] == "done"
    assert nodes.count("researcher") == 2
    assert "supervisor" in nodes
    assert "merge" in nodes
    assert "write" in nodes


async def test_run_research_with_corpus_records_evidence(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, owner_id, company_id = await _make_task(session_factory)
    chunks = split_pages(
        [
            LCDocument(
                page_content="营业收入八百亿元，同比增长百分之十二。" * 30,
                metadata={"source_id": "rw-11111111", "company": company_id, "page": 1},
            )
        ]
    )
    append_to_index(
        owner_id, company_id, chunks, embeddings=DeterministicFakeEmbedding(size=1024)
    )
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)

    ctx = {
        "job_try": 1,
        "embeddings": DeterministicFakeEmbedding(size=1024),
        "chat": FakeListChatModel(
            responses=["小结甲", "小结乙", "正文 [1]。", "正文 [2]。", "边界说明"]
        ),
        "struct_factory": _struct_factory,
    }
    result = await worker_mod.run_research(ctx, task_id)

    assert result == "done"
    task = await _task_row(session_factory, task_id)
    assert task.status == "done"
    assert task.evidence
    assert all("chunk_id" in e for e in task.evidence)
    assert task.report_md is not None
    assert task.report_md.startswith("# ")


async def test_run_research_failed_on_final_try(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _owner, _company = await _make_task(session_factory)
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)

    def _boom(_model_cls: type[BaseModel]) -> _StructStub:
        msg = "structured output unavailable"
        raise RuntimeError(msg)

    ctx = {
        "job_try": worker_mod.MAX_TRIES,
        "embeddings": DeterministicFakeEmbedding(size=1024),
        "chat": FakeListChatModel(responses=["未使用"]),
        "struct_factory": _boom,
    }
    result = await worker_mod.run_research(ctx, task_id)

    assert result == "failed:RuntimeError"
    task = await _task_row(session_factory, task_id)
    assert task.status == "failed"
    assert task.error is not None
    assert "structured output unavailable" in task.error
    nodes = await _event_nodes(task_id)
    assert nodes[-1] == "failed"


async def test_run_research_missing_task(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_mod, "SessionFactory", session_factory)
    result = await worker_mod.run_research({"job_try": 1}, str(uuid.uuid4()))
    assert result == "missing"
