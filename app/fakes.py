"""压测线 B 的确定性替身（ADR-011）：FakeChat + fake 结构化输出。

ARGUS_FAKE_LLM=1 时 make_chat / make_embeddings 分别返回 FakeChat 与
DeterministicFakeEmbedding——零 API 成本、零外部依赖、响应确定，压测打的
全是自家壳层（HTTP / PG / Redis / MinIO / worker 编排 / SSE）。

FakeChat 自带 with_structured_output：按 schema 返回最小合法实例、永不
空返回，RetryingStruct 与图代码零感知——开关只存在于两个模型工厂里。
"""

import time
from collections.abc import Iterator
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    UsageMetadata,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel

from app.prompts import AspectPlan, AspectSpec, QueryList, Reflection, ReviewVerdict

FAKE_REPLY = (
    "根据语料，公司主营业务收入保持增长，毛利率总体稳定 [1]。"
    "现金流与负债结构未见异常，经营风险集中在原材料价格波动 [2]。"
    "以上结论基于本轮检索证据，覆盖范围以语料实况为准。"
)

# 非零 usage 是刻意的：记账落行、配额 SUM 聚合这些写路径也要被压到（数值任意）
FAKE_USAGE = UsageMetadata(input_tokens=800, output_tokens=260, total_tokens=1060)


def fake_struct_instance(schema: type[BaseModel]) -> BaseModel:
    """按 schema 给最小合法实例；覆盖研究图的四个结构化模型。"""
    if schema is AspectPlan:
        return AspectPlan(
            aspects=[
                AspectSpec(
                    name="财务表现",
                    focus="核心财务指标与盈利质量",
                    key_questions=["营业收入与毛利率变化如何？", "现金流是否健康？"],
                ),
                AspectSpec(
                    name="业务与风险",
                    focus="主营业务构成与经营风险",
                    key_questions=["主营业务由哪些部分构成？", "主要经营风险是什么？"],
                ),
            ]
        )
    if schema is QueryList:
        return QueryList(queries=["营业收入 毛利率", "主营业务 经营风险"])
    if schema is Reflection:
        # core_chunk_ids 是必填字段；给空列表走 researcher 的回退路径
        # （按入库序取前 CORE_CAP），一轮即收
        return Reflection(done=True, core_chunk_ids=[])
    if schema is ReviewVerdict:
        return ReviewVerdict(need_more=False)  # 不补派：图走最短完整路径
    raise ValueError(f"fake 未覆盖的结构化模型：{schema.__name__}")


class FakeChat(BaseChatModel):
    """确定性 chat 替身：invoke 整段回、流式按块回，带固定 usage。"""

    reply: str = FAKE_REPLY
    delay_s: float = 0.0
    chunk_chars: int = 8

    @property
    def _llm_type(self) -> str:
        return "argus-fake-chat"

    def _wait(self) -> None:
        if self.delay_s > 0:
            time.sleep(self.delay_s)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._wait()
        msg = AIMessage(content=self.reply, usage_metadata=FAKE_USAGE)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        self._wait()  # 首块前一次性延迟：真模型的时延大头在首 token 之前
        pieces = [
            self.reply[i : i + self.chunk_chars]
            for i in range(0, len(self.reply), self.chunk_chars)
        ]
        for i, piece in enumerate(pieces):
            usage = FAKE_USAGE if i == len(pieces) - 1 else None
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=piece, usage_metadata=usage)
            )

    def with_structured_output(
        self, schema: type[BaseModel], **kwargs: Any
    ) -> Runnable[Any, BaseModel]:
        """结构化输出直给合法实例；method 等参数照单全收并忽略。"""

        def _run(_input: Any) -> BaseModel:
            self._wait()
            return fake_struct_instance(schema)

        return RunnableLambda(_run)
