"""token 记账与业务层配额闸（P3.1）。

记账口径：
- 事实源是模型回传的 usage_metadata，不做本地估算——估算出来的数字没法拿去
  对账单，而验收要的是「真实数字」；
- 一次图执行落一行（研究任务一行 / 对话一轮一行），节点明细进 by_node：
  行数与业务事件一一对应，配额聚合就是一次 SUM；
- 流式与非流式共用一条提取路径：langchain_core 把流式 chunk 相加之后才回调
  on_llm_end（chat_models.py:843/976），拿到的 LLMResult 与 invoke 同型；
- ChatOpenAI 只在「默认 base_url」时自动开 stream_usage（base.py:1231-1250），
  我们指向 dashscope 兼容端点，必须在 make_chat 里显式开，否则流式拿不到 usage；
- 拿不到 usage 的调用计进 missing_calls，不静默当零——静默零 = 配额永不触发。

配额口径（ADR-008）：判定走 PG 不走 Redis；滚动 24 小时窗口（免时区、免跨日
界刷量）；预算闸 + 并发闸两道，研究两道都过、对话只过预算闸（对话是秒级
交互，并发归 P3.2 的 HTTP 限流）。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import structlog
from fastapi import HTTPException, status
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import ResearchTask, TokenUsage

log = structlog.get_logger()

WINDOW_HOURS = 24
RUNNING_STATUSES = ("queued", "running")
_MILLION = Decimal(1_000_000)
_PRECISION = Decimal("0.000001")


class UsageCollector(BaseCallbackHandler):
    """一次图执行的 token 汇总器，按 langgraph 节点分组。

    节点名只在 on_chat_model_start 的 metadata 里给、usage 只在 on_llm_end 的
    LLMResult 里给——用 run_id 把两头对上（与 LLMCallLogger 同型手法）。
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.input_tokens = 0
        self.output_tokens = 0
        self.missing_calls = 0
        self.by_node: dict[str, dict[str, int]] = {}
        self._node_of: dict[object, str] = {}

    def on_chat_model_start(
        self,
        _serialized: dict,
        _messages: list,
        *,
        run_id: object,
        metadata: dict | None = None,
        **_kw: object,
    ) -> None:
        self._node_of[run_id] = (metadata or {}).get("langgraph_node", "?")

    def on_llm_end(self, response: LLMResult, *, run_id: object, **_kw: object) -> None:
        node = self._node_of.pop(run_id, "?")
        usage = _extract_usage(response)
        if usage is None:
            self.missing_calls += 1
            log.warning("usage_missing", node=node, model=self.model)
            return
        ins, outs = usage
        self.input_tokens += ins
        self.output_tokens += outs
        bucket = self.by_node.setdefault(node, {"input": 0, "output": 0, "calls": 0})
        bucket["input"] += ins
        bucket["output"] += outs
        bucket["calls"] += 1

    def on_llm_error(
        self, _error: BaseException, *, run_id: object, **_kw: object
    ) -> None:
        self._node_of.pop(run_id, None)  # 失败调用无 usage 可记，只松开挂账


def _extract_usage(response: LLMResult) -> tuple[int, int] | None:
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if usage:
                return int(usage["input_tokens"]), int(usage["output_tokens"])
    return None


def cost_of(
    input_tokens: int, output_tokens: int, settings: Settings | None = None
) -> Decimal:
    """按百万 token 单价计价，量化到 6 位小数（分以下的零头也不丢）。"""
    s = settings or get_settings()
    cost = (
        Decimal(input_tokens) / _MILLION * s.price_in_per_mtok
        + Decimal(output_tokens) / _MILLION * s.price_out_per_mtok
    )
    return cost.quantize(_PRECISION, rounding=ROUND_HALF_UP)


async def record_usage(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    company_id: uuid.UUID,
    kind: str,
    ref_id: uuid.UUID | None,
    collector: UsageCollector,
) -> None:
    """把一次图执行的账落成一行；零调用（如零证据对话）不落空行。"""
    if not collector.by_node and not collector.missing_calls:
        return
    cost = cost_of(collector.input_tokens, collector.output_tokens)
    session.add(
        TokenUsage(
            owner_id=owner_id,
            company_id=company_id,
            kind=kind,
            ref_id=ref_id,
            model=collector.model,
            input_tokens=collector.input_tokens,
            output_tokens=collector.output_tokens,
            cost_cny=cost,
            missing_calls=collector.missing_calls,
            by_node=collector.by_node or None,
        )
    )
    await session.commit()
    log.info(
        "usage_recorded",
        kind=kind,
        input_tokens=collector.input_tokens,
        output_tokens=collector.output_tokens,
        cost_cny=str(cost),
        missing_calls=collector.missing_calls,
    )


@dataclass(frozen=True)
class QuotaStatus:
    spend_cny: Decimal
    budget_cny: Decimal
    input_tokens: int
    output_tokens: int
    running_tasks: int
    max_running: int

    @property
    def over_budget(self) -> bool:
        return self.spend_cny >= self.budget_cny

    @property
    def at_capacity(self) -> bool:
        return self.running_tasks >= self.max_running


async def quota_status(
    session: AsyncSession, owner_id: uuid.UUID, settings: Settings | None = None
) -> QuotaStatus:
    s = settings or get_settings()
    since = datetime.now(UTC) - timedelta(hours=WINDOW_HOURS)
    totals = await session.execute(
        select(
            func.coalesce(func.sum(TokenUsage.cost_cny), 0),
            func.coalesce(func.sum(TokenUsage.input_tokens), 0),
            func.coalesce(func.sum(TokenUsage.output_tokens), 0),
        ).where(TokenUsage.owner_id == owner_id, TokenUsage.created_at >= since)
    )
    spend, ins, outs = totals.one()
    running = await session.scalar(
        select(func.count())
        .select_from(ResearchTask)
        .where(
            ResearchTask.owner_id == owner_id,
            ResearchTask.status.in_(RUNNING_STATUSES),
        )
    )
    return QuotaStatus(
        spend_cny=Decimal(spend),
        budget_cny=s.budget_cny_24h,
        input_tokens=int(ins),
        output_tokens=int(outs),
        running_tasks=int(running or 0),
        max_running=s.max_running_research,
    )


async def enforce_quota(
    session: AsyncSession, owner_id: uuid.UUID, *, need_slot: bool
) -> QuotaStatus:
    """超限即 429；need_slot=True 时另过并发闸（研究占槽，对话不占）。"""
    st = await quota_status(session, owner_id)
    if st.over_budget:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"近 {WINDOW_HOURS} 小时预算已用尽"
                f"（¥{st.spend_cny:.2f} / ¥{st.budget_cny:.2f}）："
                "等窗口滚动，或调高 ARGUS_BUDGET_CNY_24H"
            ),
        )
    if need_slot and st.at_capacity:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"已有 {st.running_tasks} 个研究任务在排队或执行"
                f"（并发上限 {st.max_running}）：等在跑的任务结束再发起"
            ),
        )
    return st
