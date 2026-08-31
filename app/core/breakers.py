"""dashscope 两端点的熔断器（P3.5，ADR-013）：Redis 共享态，API 与 worker 同视野。

为什么还需要熔断：现有三层重试（openai max_retries → RetryPolicy → arq
Retry）全是「每个调用各自撞墙」，治瞬态抖动；端点持续故障时重试是放大
器——所有在跑任务与对话各自烧满全部退避才失败，还持续锤已挂的端点。
熔断器补的是跨调用的共享记忆：连续失败确认故障后拉闸，新调用秒败
（CircuitBreakerError），冷却期后半开放一个探针，成功即自动闭合。

为什么状态放 Redis（pybreaker CircuitRedisStorage）：调用方横跨 API 进程
（chat 内联图）与 worker 进程（研究图 + 入库 embedding），进程内熔断器
各数各的——worker 已确认端点挂死，API 还在放行用户请求白等超时。
共享态让第一个发现故障的进程为所有进程拉闸。

两个端点分开熔断：chat（qwen-flash）与 embedding（text-embedding-v4）
故障域独立，一个挂不连坐另一个——embedding 熔断期间对话仍可词法检索
并正常回答（降级动作在 app/engine/retrieval.py，批2）。

Redis client 不开 decode_responses：CircuitRedisStorage 自己 .decode()，
开了会对 str 二次 decode 崩。Redis 不可用时 storage 回落 fallback=closed
放行——与 rate_limit fail-open 同哲学：护栏挂了不连坐核心功能。
"""

import pybreaker
import redis
import structlog
from openai import APIConnectionError, InternalServerError, RateLimitError

from app.core.config import get_settings

log = structlog.get_logger()

# 阈值推导：
# - fail_max 计「外显连续失败」。LLM 每次外显失败背后 openai 客户端已内部
#   重试 2 次（make_chat max_retries=2），5 次外显 ≈ 15 连败，非端点故障
#   凑不出来；embedding 无内部业务重试且调用密度高（批量入库 + 每次检索
#   一次 embed_query），3 次即可确认。
# - reset_timeout = 半开探针间隔。实测故障窗是十几分钟级（2026-08-19
#   18:45~19:04 抖动事故），60s 一探在窗内至多空探 20 次、恢复后 1 分钟内
#   自动闭合；embedding 路有词法兜底、open 期间服务仍可用，30s 更快回到
#   混合检索。
LLM_FAIL_MAX = 5
LLM_RESET_TIMEOUT = 60
EMB_FAIL_MAX = 3
EMB_RESET_TIMEOUT = 30

# 只有端点层故障才计入熔断：连接失败/超时（APITimeoutError 是
# APIConnectionError 子类）、429 过载、5xx。认证错/BadRequest 是我们的
# 问题，RetryingStruct 耗尽的 ValueError 是模型行为问题——都不是「端点
# 挂了」，全部排除（worker 的 transient/permanent 分类学在端点层的对应物）。
ENDPOINT_FAILURES = (APIConnectionError, RateLimitError, InternalServerError)


def _not_endpoint_failure(exc: BaseException) -> bool:
    return not isinstance(exc, ENDPOINT_FAILURES)


class _LogListener(pybreaker.CircuitBreakerListener):
    """状态迁移进日志：降级必须可观测，静默降级=质量事故没人知道。"""

    def state_change(
        self, cb: pybreaker.CircuitBreaker, old_state: object, new_state: object
    ) -> None:
        log.warning(
            "breaker_state_change",
            breaker=cb.name,
            old=getattr(old_state, "name", str(old_state)),
            new=getattr(new_state, "name", str(new_state)),
        )


def make_breaker(
    name: str, fail_max: int, reset_timeout: float
) -> pybreaker.CircuitBreaker:
    """工厂公开：测试用独立 namespace 实例验证同一套配置。"""
    storage = pybreaker.CircuitRedisStorage(
        pybreaker.STATE_CLOSED,
        redis.Redis.from_url(get_settings().redis_url),
        namespace=f"breaker:{name}",
        fallback_circuit_state=pybreaker.STATE_CLOSED,
    )
    return pybreaker.CircuitBreaker(
        fail_max=fail_max,
        reset_timeout=reset_timeout,
        exclude=[_not_endpoint_failure],
        listeners=[_LogListener()],
        state_storage=storage,
        name=name,
    )


llm_breaker = make_breaker("llm", LLM_FAIL_MAX, LLM_RESET_TIMEOUT)
emb_breaker = make_breaker("emb", EMB_FAIL_MAX, EMB_RESET_TIMEOUT)
