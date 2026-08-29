from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-secret-change-me-not-for-production!"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARGUS_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = "postgresql://argus:argus@localhost:5432/argus"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "argus"
    minio_secret_key: str = "argusminio"
    minio_secure: bool = False
    environment: str = "dev"  # dev 之外的值会启用启动时安全自检（main.lifespan）
    jwt_secret: str = DEV_JWT_SECRET
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 7
    minio_bucket: str = "argus-documents"
    max_upload_mb: int = 50
    dashscope_api_key: str = ""
    # P3.1 计价与配额：单价按「每百万 token」计（dashscope 定价页口径）。
    # 默认值只是初值，首跑后按真实账单校准——记账要能拿去对账，
    # 不能是拍脑袋的常数；校准只改 .env，不改代码。
    price_in_per_mtok: Decimal = Decimal("0.15")
    price_out_per_mtok: Decimal = Decimal("1.5")
    budget_cny_24h: Decimal = Decimal(20)
    max_running_research: int = 2
    # P3.3 压测线 B（ADR-011）：LLM 与 embedding 全换确定性 fake（零 API
    # 成本零外部依赖），只压自家壳层；delay 模拟单次调用时延，让任务时长
    # 与队列形态接近真实，而不是瞬间完成什么都压不出来
    fake_llm: bool = False
    fake_llm_delay_s: float = 0.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
