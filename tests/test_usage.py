"""记账与配额的纯单测：回调汇总、计价、配额判定（不碰 DB、不发网络）。

三件事在此钉死：
- usage 提取对「流式累加后的 chunk」与「invoke 的整条消息」同型——langchain_core
  把流式 chunk 相加之后才回调 on_llm_end（chat_models.py:843/976），两条路一份代码；
- 拿不到 usage 的调用计进 missing_calls，绝不静默当零：静默零 = 配额永不触发；
- 配额边界取「等于即拒」，预算闸与并发闸各自独立。
"""

from decimal import Decimal

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, LLMResult

from app.core.config import Settings
from app.domain.usage import QuotaStatus, UsageCollector, cost_of


def _result(inp: int, out: int, *, streaming: bool = False) -> LLMResult:
    usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
    gen: ChatGeneration = (
        ChatGenerationChunk(message=AIMessageChunk(content="x", usage_metadata=usage))
        if streaming
        else ChatGeneration(message=AIMessage(content="x", usage_metadata=usage))
    )
    return LLMResult(generations=[[gen]])


def _call(collector: UsageCollector, node: str, result: LLMResult, run_id: str) -> None:
    collector.on_chat_model_start(
        {}, [], run_id=run_id, metadata={"langgraph_node": node}
    )
    collector.on_llm_end(result, run_id=run_id)


def test_collector_sums_and_groups_by_node() -> None:
    c = UsageCollector("qwen-flash")
    _call(c, "researcher", _result(100, 20), "r1")
    _call(c, "researcher", _result(300, 40), "r2")
    _call(c, "write", _result(500, 900), "w1")
    assert (c.input_tokens, c.output_tokens) == (900, 960)
    assert c.by_node["researcher"] == {"input": 400, "output": 60, "calls": 2}
    assert c.by_node["write"] == {"input": 500, "output": 900, "calls": 1}
    assert c.missing_calls == 0


def test_streaming_chunk_usage_is_read_the_same_way() -> None:
    c = UsageCollector("qwen-flash")
    _call(c, "answer", _result(80, 12, streaming=True), "a1")
    assert (c.input_tokens, c.output_tokens) == (80, 12)
    assert c.by_node["answer"]["calls"] == 1


def test_missing_usage_counts_instead_of_silent_zero() -> None:
    c = UsageCollector("qwen-flash")
    bare = LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]])
    _call(c, "condense", bare, "c1")
    assert c.missing_calls == 1
    assert (c.input_tokens, c.output_tokens) == (0, 0)
    assert c.by_node == {}


def test_llm_error_releases_the_pending_run() -> None:
    c = UsageCollector("qwen-flash")
    c.on_chat_model_start({}, [], run_id="e1", metadata={"langgraph_node": "write"})
    c.on_llm_error(ValueError("boom"), run_id="e1")
    assert c.by_node == {}
    assert c.missing_calls == 0


def test_cost_uses_configured_prices_at_six_decimals() -> None:
    s = Settings(price_in_per_mtok=Decimal("0.15"), price_out_per_mtok=Decimal("1.5"))
    assert cost_of(1_000_000, 200_000, s) == Decimal("0.45")
    assert cost_of(1000, 0, s) == Decimal("0.000150")


def test_quota_boundaries_reject_on_equal() -> None:
    at_budget = QuotaStatus(Decimal(20), Decimal(20), 0, 0, 0, 2)
    assert at_budget.over_budget
    assert not at_budget.at_capacity
    at_slots = QuotaStatus(Decimal(1), Decimal(20), 0, 0, 2, 2)
    assert at_slots.at_capacity
    assert not at_slots.over_budget
