"""HTTP 频率限流与 SSE 并发闸（P3.2，ADR-009）。

三层限流各司其职，本模块管前两层：
- HTTP 频率（fastapi-limiter 0.1.6，Redis lua 原子计数）：登录后 per-user
  跨设备共享额度，未登录退化 per-IP；只限写路径与烧钱路径，读路径不设闸、
  承载力交给压测验证（限读路径会先把自己前端的轮询打死）；
- SSE 并发闸（Redis 计数器）：流式连接一挂几分钟，按请求数限不住它——
  按「同时在线的流」限，研究进度流与对话流共享一个池；
- 业务配额（预算/任务槽）在 app/usage.py（ADR-008），走 PG。

rate_limit 在未 init 时 fail-open（直接放行）：限流是防滥用的护栏，不是
核心功能的前置依赖，Redis 挂了不该连坐把登录拦死；这也让全部 API 测试
免 init 直跑（ASGITransport 不执行 lifespan），限流行为由专项测试覆盖。
"""

from collections.abc import Awaitable, Callable
from math import ceil

from fastapi import HTTPException, Request, Response, status
from fastapi_limiter import FastAPILimiter
from redis.asyncio import Redis
from redis.exceptions import NoScriptError

from app.config import get_settings
from app.security import decode_token

# 阈值推导见 ADR-009：auth 三条 per-IP（人手速之上、脚本之下）；
# research/chat 的真实约束在业务配额，这里只防连点；healthz 防压测放大器
REGISTER_PER_MIN = 5
LOGIN_PER_MIN = 10
REFRESH_PER_MIN = 30
UPLOAD_PER_MIN = 20
RESEARCH_PER_MIN = 10
CHAT_PER_MIN = 20
HEALTHZ_PER_MIN = 60

SSE_MAX_CONCURRENT = 5  # 前端峰值 2（研究进度 + 对话），2.5 倍余量
SSE_TTL_SECONDS = 1800  # 泄漏槽位的自愈上限：连接崩掉没走到 release 时


async def user_or_ip(request: Request) -> str:
    """已登录按 user 计（跨设备共享额度），未登录退化按 IP。

    只解码不查库：限流跑在鉴权之前，坏 token 按 IP 计、放行后自会被
    get_current_user 拒掉。库内 key 自带 route 序号，无需拼路径。
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        user_id = decode_token(auth.removeprefix("Bearer "), "access")
        if user_id is not None:
            return f"u:{user_id}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


async def rate_limit_callback(
    _request: Request, _response: Response, pexpire: int
) -> None:
    seconds = ceil(pexpire / 1000)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"请求过于频繁，请 {seconds} 秒后再试",
        headers={"Retry-After": str(seconds)},
    )


async def _check(key: str, times: int, milliseconds: int) -> int:
    return await FastAPILimiter.redis.evalsha(
        FastAPILimiter.lua_sha, 1, key, str(times), str(milliseconds)
    )


def rate_limit(
    scope: str, times: int, seconds: int = 60
) -> Callable[[Request, Response], Awaitable[None]]:
    """频率闸依赖：库的 lua 原子计数 + 显式 scope 键。

    不走库的 RateLimiter.__call__：它按 route_index 扫 app.routes 拼键，
    FastAPI 0.141 的 _IncludedRouter 无 .path 一碰即崩（0.2.0 同病，测试
    抓获）；且 route 序号键随路由增删漂移，部署一次计数器全体清零。
    显式 scope 键稳定、在 Redis 里可读，计数核心仍是库的 lua 脚本。
    未初始化（测试 / Redis 不可用）fail-open 放行。
    """
    milliseconds = seconds * 1000

    async def dependency(request: Request, response: Response) -> None:
        if FastAPILimiter.redis is None:
            return
        key = f"{FastAPILimiter.prefix}:{await user_or_ip(request)}:{scope}"
        try:
            pexpire = await _check(key, times, milliseconds)
        except NoScriptError:
            # Redis 重启 / SCRIPT FLUSH 后脚本缓存失效：重载再试（库版同款兜底）
            FastAPILimiter.lua_sha = await FastAPILimiter.redis.script_load(
                FastAPILimiter.lua_script
            )
            pexpire = await _check(key, times, milliseconds)
        if pexpire != 0:
            await rate_limit_callback(request, response, pexpire)

    return dependency


class SseGate:
    """per-user SSE 并发计数：INCR 抢槽、DECR 还槽、TTL 兜底泄漏。

    计的是「连接」不是「计算」：对话断线后图在后台跑完（ADR-010），但槽位
    随连接断开即还。负漂移（release 多于 acquire）删键归零——宁可短暂多放，
    不永久少放。每次开临时连接与现有 SSE 端点同型（跨事件循环安全）。
    """

    def __init__(
        self,
        max_concurrent: int = SSE_MAX_CONCURRENT,
        ttl_seconds: int = SSE_TTL_SECONDS,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(user_key: str) -> str:
        return f"sse:conc:{user_key}"

    async def acquire(self, user_key: str) -> bool:
        redis = Redis.from_url(get_settings().redis_url)
        try:
            key = self._key(user_key)
            count = await redis.incr(key)
            await redis.expire(key, self.ttl_seconds)
            if count > self.max_concurrent:
                await redis.decr(key)
                return False
            return True
        finally:
            await redis.aclose()

    async def release(self, user_key: str) -> None:
        redis = Redis.from_url(get_settings().redis_url)
        try:
            key = self._key(user_key)
            if await redis.decr(key) < 0:
                await redis.delete(key)
        finally:
            await redis.aclose()


sse_gate = SseGate()
