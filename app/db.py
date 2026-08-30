from collections.abc import AsyncIterator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


engine = create_async_engine(async_url(get_settings().database_url))
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


def sync_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


# 同步引擎：SearchFn 在图的同步节点/线程池里执行，async engine 进不去
# （异步化整张图是大动作，不为检索做）；QueuePool 线程安全，懒连接零启动成本
sync_engine = create_engine(sync_url(get_settings().database_url))
