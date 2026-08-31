"""P1.1 冒烟：healthz 两条路径 + settings 单例。

探针用桩替换（monkeypatch 注入 _DEP_CHECKS），不依赖 compose 起没起，
pytest 随时可绿；真实依赖连通性由 compose healthcheck 与页面验收覆盖。
"""

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_mod
from app.core.config import Settings, get_settings


async def _ok(_settings: Settings) -> None:
    """探针成功桩。"""


async def _boom(_settings: Settings) -> None:
    msg = "connection refused"
    raise RuntimeError(msg)


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=main_mod.app),
        base_url="http://test",
    )


async def test_healthz_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(main_mod._DEP_CHECKS):
        monkeypatch.setitem(main_mod._DEP_CHECKS, name, _ok)
    async with _client() as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["deps"] == {"postgres": "ok", "redis": "ok", "minio": "ok"}


async def test_healthz_reports_failed_dep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(main_mod._DEP_CHECKS, "postgres", _boom)
    monkeypatch.setitem(main_mod._DEP_CHECKS, "redis", _ok)
    monkeypatch.setitem(main_mod._DEP_CHECKS, "minio", _ok)
    async with _client() as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["deps"]["postgres"].startswith("error:")
    assert body["deps"]["redis"] == "ok"
    assert body["deps"]["minio"] == "ok"


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
