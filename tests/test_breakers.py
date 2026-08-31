"""熔断器（P3.5 批1）：状态机行为、异常分类、Redis 共享态、模型包装接线。

模块级 llm_breaker / emb_breaker 不在这里直接触发——它们的状态在 Redis
db1 里跨测试存活，打开了会污染后续测试。行为测试用 make_breaker 造
独立随机 namespace 的实例（测的是我们的工厂配置：exclude 谓词 +
RedisStorage + listener）；接线测试 monkeypatch 模块级名字换成进程内
存 breaker（共享态已由专项用例锁定，接线只锁「调用确实过 breaker」）。
"""

import uuid
from collections.abc import Iterator

import httpx
import pybreaker
import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from openai import APIConnectionError

from app.core.breakers import make_breaker
from app.core.config import get_settings
from app.engine.fakes import FakeChat
from app.engine.ingest import BreakerEmbeddings, make_embeddings
from app.engine.llm import BreakerChat, make_chat


def _conn_error() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", "https://dashscope.test"))


def _ns() -> str:
    """随机 namespace：Redis db1 的键跨测试存活，隔离靠不撞名。"""
    return f"test-{uuid.uuid4().hex[:8]}"


def test_trips_after_consecutive_endpoint_failures() -> None:
    br = make_breaker(_ns(), fail_max=3, reset_timeout=60)
    calls = 0

    def failing() -> None:
        nonlocal calls
        calls += 1
        raise _conn_error()

    for _ in range(2):
        with pytest.raises(APIConnectionError):
            br.call(failing)
    # 第 fail_max 次：跳闸，异常被替换为 CircuitBreakerError
    with pytest.raises(pybreaker.CircuitBreakerError):
        br.call(failing)
    assert calls == 3
    # open 态：秒败且不再打后端
    with pytest.raises(pybreaker.CircuitBreakerError):
        br.call(failing)
    assert calls == 3
    assert br.current_state == "open"


def test_excluded_errors_never_trip() -> None:
    """非端点故障（RetryingStruct 耗尽的 ValueError 等）不计入熔断。"""
    br = make_breaker(_ns(), fail_max=2, reset_timeout=60)

    def bad_input() -> None:
        raise ValueError("模型未调用工具")

    for _ in range(5):
        with pytest.raises(ValueError, match="模型未调用工具"):
            br.call(bad_input)
    assert br.current_state == "closed"
    assert br.call(lambda: "ok") == "ok"


def test_state_shared_across_instances() -> None:
    """同 namespace 两实例 = 模拟 API 进程与 worker 进程：一个跳闸全体秒拒。"""
    ns = _ns()
    br_worker = make_breaker(ns, fail_max=2, reset_timeout=60)
    br_api = make_breaker(ns, fail_max=2, reset_timeout=60)

    def failing() -> None:
        raise _conn_error()

    with pytest.raises(APIConnectionError):
        br_worker.call(failing)
    with pytest.raises(pybreaker.CircuitBreakerError):
        br_worker.call(failing)

    api_calls = 0

    def healthy() -> str:
        nonlocal api_calls
        api_calls += 1
        return "ok"

    with pytest.raises(pybreaker.CircuitBreakerError):
        br_api.call(healthy)
    assert api_calls == 0


def test_half_open_probe_recovers() -> None:
    """reset_timeout 过后半开：探针成功即闭合，恢复全靠自动无需人工。"""
    br = make_breaker(_ns(), fail_max=1, reset_timeout=0)

    def failing() -> None:
        raise _conn_error()

    with pytest.raises(pybreaker.CircuitBreakerError):
        br.call(failing)
    # reset_timeout=0：下一次调用即半开探针
    assert br.call(lambda: "ok") == "ok"
    assert br.current_state == "closed"
    assert br.call(lambda: "ok") == "ok"


def test_open_blocks_stream_before_any_chunk() -> None:
    """open 态下生成器函数在 call 入口即拒，流一个字节都不会开始建。"""
    br = make_breaker(_ns(), fail_max=1, reset_timeout=60)
    br.open()
    started = 0

    def gen() -> Iterator[str]:
        nonlocal started
        started += 1
        yield "chunk"

    with pytest.raises(pybreaker.CircuitBreakerError):
        br.call(gen)
    assert started == 0


def test_generator_midstream_failure_counts() -> None:
    """pybreaker 原生生成器支持：流中途的端点异常也计入失败。"""
    br = make_breaker(_ns(), fail_max=1, reset_timeout=60)

    def bad_stream() -> Iterator[str]:
        yield "a"
        raise _conn_error()

    wrapped = br.call(bad_stream)
    assert next(wrapped) == "a"
    with pytest.raises((APIConnectionError, pybreaker.CircuitBreakerError)):
        next(wrapped)
    assert br.current_state == "open"


def test_breaker_chat_generate_goes_through_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_br = pybreaker.CircuitBreaker(fail_max=1, reset_timeout=600)
    monkeypatch.setattr("app.engine.llm.llm_breaker", test_br)
    calls = 0

    def fake_generate(self: object, *args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise _conn_error()

    monkeypatch.setattr(ChatOpenAI, "_generate", fake_generate)
    chat = make_chat()
    assert isinstance(chat, BreakerChat)

    with pytest.raises(pybreaker.CircuitBreakerError):
        chat.invoke("hi")
    assert calls == 1
    # 熔断已开：第二次不再触及底层 _generate
    with pytest.raises(pybreaker.CircuitBreakerError):
        chat.invoke("hi")
    assert calls == 1


def test_breaker_embeddings_goes_through_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_br = pybreaker.CircuitBreaker(fail_max=1, reset_timeout=600)
    monkeypatch.setattr("app.engine.ingest.emb_breaker", test_br)
    calls = 0

    def fake_embed_query(self: object, text: str) -> None:
        nonlocal calls
        calls += 1
        raise _conn_error()

    monkeypatch.setattr(OpenAIEmbeddings, "embed_query", fake_embed_query)
    emb = make_embeddings()
    assert isinstance(emb, BreakerEmbeddings)

    with pytest.raises(pybreaker.CircuitBreakerError):
        emb.embed_query("营业收入")
    assert calls == 1
    with pytest.raises(pybreaker.CircuitBreakerError):
        emb.embed_query("营业收入")
    assert calls == 1


def test_factories_wrap_only_real_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """fake 分支不包熔断：fake 不打端点，包了反而让压测线 B 混入熔断行为。"""
    assert isinstance(make_chat(), BreakerChat)
    assert isinstance(make_embeddings(), BreakerEmbeddings)
    monkeypatch.setattr(get_settings(), "fake_llm", True)
    assert isinstance(make_chat(), FakeChat)
    assert isinstance(make_embeddings(), DeterministicFakeEmbedding)
