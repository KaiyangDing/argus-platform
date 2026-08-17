"""集成测试基座（P1.2 起）。

- 在任何 app 模块被导入前把数据库指到 argus_test：pytest 最先加载
  conftest，测试模块随后才 import app，settings 读到的即测试库
- 建库建表一次性用 asyncio.run 在独立事件循环跑完，与测试各自的循环无关
- 测试引擎用 NullPool：pytest-asyncio 每个测试一个新事件循环，池化连接
  跨循环复用会炸，每次新建连接绕开
- 经 app.dependency_overrides 覆盖 get_session：端点全走测试引擎，
  app.db 的模块级 engine 在测试中从不建立连接
- client fixture 每次先 TRUNCATE users，测试间零残留
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

TEST_DB_URL = "postgresql://argus:argus@localhost:5432/argus_test"
ADMIN_DB_URL = "postgresql://argus:argus@localhost:5432/postgres"

os.environ["ARGUS_DATABASE_URL"] = TEST_DB_URL
os.environ["ARGUS_MINIO_BUCKET"] = "argus-test-documents"


async def _create_db_and_tables() -> None:
    admin = await asyncpg.connect(ADMIN_DB_URL)
    try:
        with contextlib.suppress(asyncpg.DuplicateDatabaseError):
            await admin.execute("CREATE DATABASE argus_test")
    finally:
        await admin.close()

    import app.models  # noqa: F401  导入即注册表元数据
    from app.db import Base, async_url
    from app.storage import ensure_bucket

    engine = create_async_engine(async_url(TEST_DB_URL), poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    # ASGITransport 不执行 lifespan，测试 bucket 在此确保
    await ensure_bucket()


@pytest.fixture(scope="session")
def _test_db() -> None:
    asyncio.run(_create_db_and_tables())


class FakeArq:
    """记录 enqueue 调用的队列桩；端点测试用它断言任务投递。"""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[object, ...]]] = []

    async def enqueue_job(self, name: str, *args: object, **kwargs: object) -> None:
        self.jobs.append((name, args))


@pytest.fixture
def arq_stub() -> FakeArq:
    return FakeArq()


@pytest.fixture
async def session_factory(
    _test_db: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """NullPool 会话工厂：worker/DB 直连测试用（绕过端点依赖注入）。"""
    from app.db import async_url

    engine = create_async_engine(async_url(TEST_DB_URL), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def client(_test_db: None, arq_stub: FakeArq) -> AsyncIterator[AsyncClient]:
    from app.db import async_url, get_session
    from app.deps import get_arq
    from app.main import app

    engine = create_async_engine(async_url(TEST_DB_URL), poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_arq] = lambda: arq_stub
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()
