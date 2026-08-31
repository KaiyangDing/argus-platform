"""降级动作（P3.5 批2）：embedding 不可用时检索退纯词法；worker 熔断联动。

检索降级的断言设计是双重的：兜底 try/except 生效（不抛、词法路有结果）
+ SQL 剪枝生效（零词法命中的块绝不混入——若 vec CTE 未被 :has_vec
谓词剪空，NULL 距离不过滤行，全部块会以未定义名次进向量池、带融合分
污染结果，len 断言直接抓获）。
复用 test_retrieval 的建数据链与互斥用词语料（断言不假设 jieba 词典
粒度，只依赖「查询与另两块在字符层面零共词」这一硬事实）。
"""

import httpx
import pybreaker
import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding
from openai import APIConnectionError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.breakers import LLM_RESET_TIMEOUT
from app.retrieval import make_company_search
from app.worker import BREAKER_RETRY_DEFER, RETRY_DEFER, _error_detail, _retry_defer
from tests.test_retrieval import TEXTS, _seed_company

Factory = async_sessionmaker[AsyncSession]

EMBED_DIM = 1024


class _TrippedEmbeddings(DeterministicFakeEmbedding):
    """熔断 open 场景：embed_query 秒拒。"""

    def embed_query(self, text: str) -> list[float]:
        raise pybreaker.CircuitBreakerError(
            "Timeout not elapsed yet, circuit breaker still open"
        )


class _ConnErrorEmbeddings(DeterministicFakeEmbedding):
    """闭合态首败场景：端点异常直接从客户端抛出（同时会计入熔断）。"""

    def embed_query(self, text: str) -> list[float]:
        raise APIConnectionError(
            request=httpx.Request("POST", "https://dashscope.test")
        )


async def test_search_degrades_to_lexical_on_breaker_open(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """熔断 open：检索不抛错，返回纯词法命中集——不多不少。

    len==1 是剪枝的强断言：三块全部有向量，若 vec 路未被剪空，全部会
    以 NULL 距离进池拿融合分，结果就是 3。
    """
    owner, company = await _seed_company(session_factory, TEXTS, monkeypatch)
    search = make_company_search(owner, company, _TrippedEmbeddings(size=EMBED_DIM))
    out = search("甲烷传感器毛利率如何", company, 3)
    assert len(out) == 1
    assert out[0].page_content == TEXTS[0]


async def test_search_degrades_on_endpoint_error(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """闸未开时的端点故障（前几次连败）同样降级，不等熔断确认。"""
    owner, company = await _seed_company(session_factory, TEXTS, monkeypatch)
    search = make_company_search(owner, company, _ConnErrorEmbeddings(size=EMBED_DIM))
    out = search("经营活动现金流量净额", company, 3)
    assert len(out) == 1
    assert out[0].page_content == TEXTS[1]


async def test_degraded_with_zero_lexical_hits_returns_empty(
    session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """双路皆空：降级 + 词法零命中 = 优雅空结果，不炸不兜错行。"""
    owner, company = await _seed_company(session_factory, TEXTS, monkeypatch)
    search = make_company_search(owner, company, _TrippedEmbeddings(size=EMBED_DIM))
    assert search("零重叠词汇查询", company, 3) == []


def test_retry_defer_lengthens_on_breaker() -> None:
    """熔断 open 时 10s 重投必再撞闸（闸至少开 60s），defer 拉到冷却窗后。"""
    assert _retry_defer(pybreaker.CircuitBreakerError("open")) == BREAKER_RETRY_DEFER
    assert BREAKER_RETRY_DEFER == LLM_RESET_TIMEOUT + 30
    assert _retry_defer(RuntimeError("boom")) == RETRY_DEFER


def test_error_detail_hides_breaker_internals() -> None:
    """熔断器的英文内部话不穿到时间线与处置记录；其余异常保留原样。"""
    detail = _error_detail(pybreaker.CircuitBreakerError("Timeout not elapsed yet"))
    assert "熔断" in detail
    assert "Timeout" not in detail
    assert _error_detail(ValueError("坏输入")) == "ValueError: 坏输入"
