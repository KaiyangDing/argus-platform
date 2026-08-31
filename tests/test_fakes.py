"""fake 线自洽性（P3.3，ADR-011）：压测前证明 ARGUS_FAKE_LLM=1 下全链能跑。

不碰 DB / Redis / MinIO：图的检索用内存桩。与压测环境的唯一差别是
索引来源——压测走 seed 进 MinIO 的真形态索引（fake 向量）。
"""

import time

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding

from app.core.config import get_settings
from app.engine.chat import build_chat_graph
from app.engine.fakes import FAKE_REPLY, FakeChat, fake_struct_instance
from app.engine.llm import RetryingStruct, make_chat
from app.engine.prompts import AspectPlan, QueryList, Reflection, ReviewVerdict
from app.engine.research import assign_aspect_ids, build_graph


def _doc(cid: str, text: str) -> Document:
    return Document(
        page_content=text,
        metadata={
            "chunk_id": cid,
            "source_id": "seed-00000000",
            "company": "c1",
            "page": 1,
            "seq": 0,
            "section": "",
        },
    )


def _search(query: str, slug: str, k: int) -> list[Document]:
    return [
        _doc("seed-00000000:0", "营业收入同比增长百分之十二，毛利率保持稳定。"),
        _doc("seed-00000000:1", "主营业务为软件服务，经营风险集中在应收账款。"),
    ]


def test_fake_chat_invoke_has_content_and_usage() -> None:
    msg = FakeChat().invoke("营业收入如何？")
    assert msg.content == FAKE_REPLY
    # 非零 usage 是刻意的：记账/配额的写路径在压测中也要被压到（ADR-011）
    assert msg.usage_metadata is not None
    assert msg.usage_metadata["total_tokens"] > 0


def test_fake_chat_stream_matches_invoke() -> None:
    chunks = list(FakeChat().stream("营业收入如何？"))
    assert len(chunks) > 1
    assert "".join(str(c.content) for c in chunks) == FAKE_REPLY


def test_fake_chat_delay_applies() -> None:
    chat = FakeChat(delay_s=0.05)
    t0 = time.perf_counter()
    chat.invoke("你好")
    assert time.perf_counter() - t0 >= 0.05


def test_fake_struct_instances_valid() -> None:
    plan = fake_struct_instance(AspectPlan)
    assert isinstance(plan, AspectPlan)
    # supervisor 的最小方面数校验（MIN_ASPECTS=2）必须能过
    assert len(assign_aspect_ids(plan)) >= 2
    assert isinstance(fake_struct_instance(QueryList), QueryList)
    refl = fake_struct_instance(Reflection)
    assert isinstance(refl, Reflection)
    assert refl.done  # 一轮即收：researcher 不空转多轮
    verdict = fake_struct_instance(ReviewVerdict)
    assert isinstance(verdict, ReviewVerdict)
    assert not verdict.need_more  # 不补派：图走最短完整路径


def test_fake_struct_unknown_schema_raises() -> None:
    with pytest.raises(ValueError, match="未覆盖"):
        fake_struct_instance(QueryList.__base__)  # BaseModel 本身不在覆盖表


def test_retrying_struct_over_fake_chat() -> None:
    # RetryingStruct 是生产默认分支（struct_factory=None）：fake 的
    # with_structured_output 必须接得住 method="function_calling" 且永不空返回
    out = RetryingStruct(FakeChat(), AspectPlan).invoke("任意输入")
    assert isinstance(out, AspectPlan)


def test_make_chat_and_embeddings_fake_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.ingest import make_embeddings

    monkeypatch.setattr(get_settings(), "fake_llm", True)
    monkeypatch.setattr(get_settings(), "fake_llm_delay_s", 0.0)
    chat = make_chat()
    assert isinstance(chat, FakeChat)
    assert chat.delay_s == 0.0
    assert isinstance(make_embeddings(), DeterministicFakeEmbedding)


def test_fake_research_graph_end_to_end() -> None:
    graph = build_graph(chat=FakeChat(), search=_search).compile()
    state = graph.invoke(
        {"company": "压测公司", "slug": "c1", "corpus_profile": "两份合成年报。"}
    )
    assert "尽调报告" in state["report"]
    assert state["evidence"]  # merge 产出全局证据表


def test_fake_chat_graph_end_to_end() -> None:
    graph = build_chat_graph(FakeChat(), _search).compile()
    state = graph.invoke(
        {
            "company": "压测公司",
            "slug": "c1",
            "history": [],
            "question": "营业收入如何？",
        }
    )
    assert state["answer"] == FAKE_REPLY
    assert state["evidence"]
