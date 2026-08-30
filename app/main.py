import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import structlog
from anyio import to_thread
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi_limiter import FastAPILimiter
from minio import Minio
from redis.asyncio import Redis

from app.config import DEV_JWT_SECRET, Settings, get_settings
from app.db import engine, sync_engine
from app.limits import HEALTHZ_PER_MIN, rate_limit
from app.logs import setup_logging
from app.routers.auth import router as auth_router
from app.routers.chat import router as chat_router
from app.routers.companies import router as companies_router
from app.routers.research import router as research_router
from app.routers.usage import router as usage_router
from app.storage import ensure_bucket

setup_logging()
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.environment != "dev" and settings.jwt_secret == DEV_JWT_SECRET:
        raise RuntimeError("非 dev 环境禁止使用默认 JWT 密钥：设置 ARGUS_JWT_SECRET")
    await ensure_bucket()
    limiter_redis = Redis.from_url(
        settings.redis_url, encoding="utf8", decode_responses=True
    )
    await FastAPILimiter.init(limiter_redis, prefix="argus-limit")
    app_.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield
    await app_.state.arq.aclose()
    # 不走 FastAPILimiter.close()：它调 redis-py 5 已弃用的 close()，自己收
    FastAPILimiter.redis = None
    await limiter_redis.aclose()
    await engine.dispose()
    sync_engine.dispose()


app = FastAPI(title="Argus Platform", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(companies_router)
app.include_router(research_router)
app.include_router(chat_router)
app.include_router(usage_router)


async def _check_postgres(settings: Settings) -> None:
    conn = await asyncpg.connect(settings.database_url, timeout=2.0)
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


async def _check_redis(settings: Settings) -> None:
    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.ping()
    finally:
        await redis.aclose()


async def _check_minio(settings: Settings) -> None:
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    await to_thread.run_sync(client.list_buckets)


_DEP_CHECKS = {
    "postgres": _check_postgres,
    "redis": _check_redis,
    "minio": _check_minio,
}


@app.get("/healthz", dependencies=[Depends(rate_limit("healthz", HEALTHZ_PER_MIN))])
async def healthz() -> JSONResponse:
    settings = get_settings()
    names = list(_DEP_CHECKS)
    results = await asyncio.gather(
        *(asyncio.wait_for(_DEP_CHECKS[name](settings), 3.0) for name in names),
        return_exceptions=True,
    )
    deps: dict[str, str] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, Exception):
            deps[name] = f"error: {result!r}"
            log.warning("healthz_dep_failed", dep=name, error=repr(result))
        else:
            deps[name] = "ok"
    healthy = all(v == "ok" for v in deps.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "deps": deps},
    )
