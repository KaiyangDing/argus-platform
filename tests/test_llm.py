"""llm 层单元测试：RetryingStruct 重试语义与 make_chat 超时硬化（研究仓 v0.2 处方）。

零网络：chat 用桩替身，只验证 method=function_calling 强制、空返回催办重试、
耗尽快速失败与 make_chat 的 timeout/max_retries 钉死。
"""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import app.llm as llm_mod
from app.llm import RetryingStruct, make_chat


class _Plan(BaseModel):
    items: list[str]


class _RecordingRunnable:
    def __init__(self, outputs: list[object]) -> None:
        self._outputs = list(outputs)
        self.calls: list[object] = []

    def invoke(self, msgs: object) -> object:
        self.calls.append(msgs)
        return self._outputs.pop(0)


class _Chat:
    def __init__(self, outputs: list[object]) -> None:
        self.runnable = _RecordingRunnable(outputs)
        self.seen_method: str | None = None

    def with_structured_output(
        self, _model_cls: type[BaseModel], method: str | None = None
    ) -> _RecordingRunnable:
        self.seen_method = method
        return self.runnable


def test_retrying_struct_forces_function_calling_and_survives_none() -> None:
    chat = _Chat([None, None, _Plan(items=["a"])])
    out = RetryingStruct(chat, _Plan).invoke("原始消息")
    assert chat.seen_method == "function_calling"
    assert out.items == ["a"]
    assert len(chat.runnable.calls) == 3  # 前两次 None 被重试吃掉


def test_retrying_struct_nudges_from_original_input() -> None:
    """拒调是粘性的：重试须从原始输入重建并追加恰一条催办消息（不叠加）。"""
    chat = _Chat([None, None, _Plan(items=["a"])])
    RetryingStruct(chat, _Plan).invoke("原始消息")
    first, second, third = chat.runnable.calls
    assert first == "原始消息"
    for call in (second, third):
        assert isinstance(call, list)
        assert call[0].content == "原始消息"
        nudges = [m for m in call if "禁止输出普通文本" in str(m.content)]
        assert len(nudges) == 1
        assert call[-1] is nudges[0]


def test_retrying_struct_exhaustion_raises() -> None:
    chat = _Chat([None, None, None])
    with pytest.raises(ValueError, match="空返回"):
        RetryingStruct(chat, _Plan).invoke("原始消息")


def test_make_chat_pins_timeout_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """600s 默认超时会把服务端挂死请求放大成半小时黑洞——必须钉短。

    240 是产品分叉值（研究仓钉 120）：v0.2 深图长窗调用在抖动时段被 120
    错杀进重试循环（2026-08-19 首跑事故）；挂死可见性由 LLMCallLogger 补位。
    """
    monkeypatch.setattr(
        llm_mod, "get_settings", lambda: SimpleNamespace(dashscope_api_key="test-key")
    )
    chat = make_chat()
    assert chat.request_timeout == 240.0
    assert chat.max_retries == 2
