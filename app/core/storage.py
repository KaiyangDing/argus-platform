"""MinIO 对象存储封装：同步 SDK 一律经线程池调用，不堵事件循环。"""

import io

from anyio import to_thread
from minio import Minio

from app.core.config import get_settings


def _client() -> Minio:
    settings = get_settings()
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


async def ensure_bucket() -> None:
    settings = get_settings()
    client = _client()

    def _ensure() -> None:
        if not client.bucket_exists(settings.minio_bucket):
            client.make_bucket(settings.minio_bucket)

    await to_thread.run_sync(_ensure)


async def put_pdf(object_key: str, data: bytes) -> None:
    settings = get_settings()
    client = _client()

    def _put() -> None:
        client.put_object(
            settings.minio_bucket,
            object_key,
            io.BytesIO(data),
            length=len(data),
            content_type="application/pdf",
        )

    await to_thread.run_sync(_put)


def get_bytes(object_key: str) -> bytes:
    settings = get_settings()
    response = _client().get_object(settings.minio_bucket, object_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def put_bytes(object_key: str, data: bytes, content_type: str) -> None:
    settings = get_settings()
    _client().put_object(
        settings.minio_bucket,
        object_key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
