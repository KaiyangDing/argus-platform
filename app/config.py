from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    mineru_api_url: str = "http://127.0.0.1:8888"


@lru_cache
def get_settings() -> Settings:
    return Settings()
