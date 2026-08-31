"""模型接入的单一事实源：ChatOpenAI + dashscope OpenAI 兼容端点。

设计源自研究仓撞墙记录①：ChatTongyi 无标准 usage_metadata 且
community 集成整包日落，官方兼容端点 + langchain-openai 是正路。

v0.2 同步（研究仓工程问题账处方）：
- timeout 钉短 240s（研究仓钉 120；产品放宽实测值：v0.2 深图长窗调用在
  抖动时段会被 120 错杀进重试循环——2026-08-19 事故；挂死可见性由
  LLMCallLogger 调用级日志补位，任务级 60 分钟预算兜底）；
- RetryingStruct：json_schema 约束解码在三层嵌套 schema 上服务端挂死
  （确定性）→ 图内结构化输出一律 method="function_calling"；
  function_calling 偶发拒调且对特定输入粘性（同输入三连拒）→
  空返回重试 ≤3，第二次起注入催办消息扰动输入，耗尽快速失败。
"""

from collections.abc import Iterator

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.core.breakers import llm_breaker
from app.core.config import get_settings
from app.engine.fakes import FakeChat

DASHSCOPE_COMPAT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
CHAT_MODEL = "qwen-flash"
EMBED_MODEL = "text-embedding-v4"


class BreakerChat(ChatOpenAI):
    """chat 端点熔断包装：同步 _generate / _stream 全走 llm_breaker。

    覆盖这两个即覆盖图内全部调用形态：图节点都是同步函数（langgraph 丢
    线程池执行），invoke 在 token 流式消费下由 langchain 自动切到 _stream
    （_should_stream 检测到 token 回调），writer.batch 也是线程池里的
    invoke。异步 _agenerate 只有 async 节点会走，本仓没有。
    breaker.call 原生支持生成器：open 态在建流前秒败，流中途异常也计数。
    """

    def _generate(self, *args: object, **kwargs: object) -> ChatResult:
        return llm_breaker.call(super()._generate, *args, **kwargs)

    def _stream(self, *args: object, **kwargs: object) -> Iterator[ChatGenerationChunk]:
        yield from llm_breaker.call(super()._stream, *args, **kwargs)


def make_chat(
    model: str = CHAT_MODEL, temperature: float = 0.0, timeout: float = 240.0
) -> BaseChatModel:
    settings = get_settings()
    if settings.fake_llm:
        # 压测线 B（ADR-011）：开关下沉在工厂里，deps / chat 路由 / worker
        # 所有注入点零改动统一生效
        return FakeChat(delay_s=settings.fake_llm_delay_s)
    return BreakerChat(
        model=model,
        base_url=DASHSCOPE_COMPAT_BASE,
        api_key=settings.dashscope_api_key,
        temperature=temperature,
        timeout=timeout,
        max_retries=2,
        # dashscope 是非默认 base_url，ChatOpenAI 的自动开关不生效
        # （base.py:1231-1250 只对默认端点自动开）——不显式开则流式无 usage
        stream_usage=True,
    )


class RetryingStruct:
    """结构化输出双坑的工程解：挂死走 function_calling，粘性拒调靠催办扰动重试。"""

    _NUDGE = "你上一次的回复没有调用工具。必须调用给定的工具/函数返回结构化结果，禁止输出普通文本。"

    def __init__(
        self, chat: BaseChatModel, model_cls: type[BaseModel], attempts: int = 3
    ) -> None:
        self._model_cls = model_cls
        self._attempts = attempts
        self._runnable = chat.with_structured_output(
            model_cls, method="function_calling"
        )

    def invoke(self, msgs: object) -> BaseModel:
        current = msgs
        for _ in range(self._attempts):
            out = self._runnable.invoke(current)
            if out is not None:
                return out
            # 拒调是粘性的（研究仓 szss 实测同输入三连拒）：每次从原始输入重建，
            # 末尾追加一条催办消息打破同输入循环（不叠加多条）
            if hasattr(msgs, "to_messages"):
                base = msgs.to_messages()
            elif isinstance(msgs, list):
                base = list(msgs)
            else:
                base = [HumanMessage(str(msgs))]
            current = [*base, HumanMessage(self._NUDGE)]
        raise ValueError(
            f"结构化输出 {self._attempts} 次空返回：{self._model_cls.__name__}（模型未调用工具）"
        )
