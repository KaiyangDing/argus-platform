"""检索层（P3.4 批2）：SQL 内混合检索——词法 ts_rank + 向量 HNSW，RRF 融合。

形态迁移：MinIO 双文件每请求全量载入、BM25 现建（jieba 分词全部块是
u40 压测定位的 chat 尾部元凶）→ chunks 表持久索引（GIN 倒排 + HNSW），
一条 SQL 出结果，每请求只剩 embed_query 一次 + 两路索引查询。

口径（ADR-012）：
- 查询侧 OR 组词不用 plainto——plainto 是 AND 语义，多词查询下词法路
  整路打灭；OR 对齐 BM25「任一词命中即计分」；
- ts_rank 非严格 BM25（无 IDF/长度归一）——RRF 只吃名次不吃分值，
  可接受；检索质量以引擎仓评测口径为裁判；
- 入库列与查询两侧同过 jieba + 'simple'，粒度一致即可对撞；
- section 直接在行里，旧「检索层回贴」补丁整体退役。
- embedding 不可用（熔断 open/端点故障）时降级纯词法路（P3.5），与 qts=NULL 词法自空互为镜像。
SearchFn 契约不变：(query, company, k) → list[Document]，图代码零感知。
"""

import json
import uuid
from collections.abc import Callable

import structlog
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from pybreaker import CircuitBreakerError
from sqlalchemy import text

from app.core.breakers import ENDPOINT_FAILURES
from app.core.db import sync_engine
from app.engine.ingest import tokenize_for_search

log = structlog.get_logger()

SearchFn = Callable[[str, str, int], list[Document]]

HYBRID_POOL = 200  # 深池再融合：小池会稀释单路命中（研究仓 S3 结论）
RRF_K = 60  # RRF 平滑常数，业界默认（与旧 EnsembleRetriever 同值）
# 降级判据与熔断计数同源：熔断 open 的秒拒 + 闸未开时的端点异常，
# 都意味着「向量路暂时拿不到」——退纯词法。密钥错/BadRequest 不在内：
# 配置事故要显式失败浮出，静默降级会把它埋掉。
_DEGRADE_ERRORS = (CircuitBreakerError, *ENDPOINT_FAILURES)

# 两路 CTE 各取深池名次，FULL OUTER JOIN 后 1/(K+rank) 相加再切 k。
# 向量参数走 pgvector 文本格式 CAST，免每连接注册类型适配器；
# :qts 为 NULL 时 to_tsquery 返回 NULL、@@ 判 NULL——词法路自然为空。
# 降级（embedding 不可用）时向量路靠 :has_vec 布尔开关剪空——NULL 距离
# 不过滤行，靠 :qvec IS NOT NULL 又会让参数类型推断歧义（psycopg 服务器
# 端绑定：同参数出现在 CAST 与裸判空两种上下文，PG 拒绝推断）。
_RRF_SQL = text("""
WITH vec AS (
    SELECT chunk_id, source_id, page, section, text,
           row_number() OVER (ORDER BY embedding <=> CAST(:qvec AS vector)) AS rank
    FROM chunks
    WHERE owner_id = :owner_id AND company_id = :company_id AND :has_vec
    ORDER BY embedding <=> CAST(:qvec AS vector)
    LIMIT :pool
),
lex AS (
    SELECT chunk_id, source_id, page, section, text,
           row_number() OVER (
               ORDER BY ts_rank(text_tokens, to_tsquery('simple', :qts)) DESC
           ) AS rank
    FROM chunks
    WHERE owner_id = :owner_id AND company_id = :company_id
      AND text_tokens @@ to_tsquery('simple', :qts)
    ORDER BY ts_rank(text_tokens, to_tsquery('simple', :qts)) DESC
    LIMIT :pool
)
SELECT chunk_id,
       COALESCE(v.source_id, l.source_id) AS source_id,
       COALESCE(v.page, l.page) AS page,
       COALESCE(v.section, l.section) AS section,
       COALESCE(v.text, l.text) AS text,
       COALESCE(1.0 / (:rrf_k + v.rank), 0)
           + COALESCE(1.0 / (:rrf_k + l.rank), 0) AS score
FROM vec v FULL OUTER JOIN lex l USING (chunk_id)
ORDER BY score DESC, chunk_id
LIMIT :k
""")


def to_or_tsquery(query: str) -> str | None:
    """query 切词 → OR 组 tsquery 串；词加引号防 tsquery 特殊字符。

    无有效词返回 None（纯标点查询），SQL 侧 NULL 让词法路自然为空、
    只走向量——与 P3.5 计划的「embedding 熔断 → 纯词法兜底」互为镜像。
    """
    terms = [t for t in tokenize_for_search(query).split() if t]
    if not terms:
        return None
    return " | ".join("'" + t.replace("'", "''") + "'" for t in terms)


def make_company_search(
    owner_id: str,
    company_id: str,
    embeddings: Embeddings,
    pool: int = HYBRID_POOL,
) -> SearchFn:
    """签名与 MinIO 版一致，调用方零改动——「检索实现可整体替换而图
    不知情」契约的兑现现场。空语料 EXISTS 短路：研究图空语料路径每方面
    3 查询，不短路会白打十几次 embed_query。"""
    owner_uuid, company_uuid = uuid.UUID(owner_id), uuid.UUID(company_id)
    with sync_engine.connect() as conn:
        has_chunks = conn.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM chunks"
                " WHERE owner_id = :o AND company_id = :c)"
            ),
            {"o": owner_uuid, "c": company_uuid},
        ).scalar()
    if not has_chunks:

        def empty(_query: str, _slug: str, _k: int) -> list[Document]:
            return []

        return empty

    def search(query: str, _slug: str, k: int) -> list[Document]:
        try:
            qvec: list[float] | None = embeddings.embed_query(query)
        except _DEGRADE_ERRORS as exc:
            # 可用性 > 检索质量；log 出声——静默降级 = 质量事故没人知道
            log.warning("retrieval_degraded", reason=str(exc)[:80])
            qvec = None
        with sync_engine.connect() as conn:
            rows = (
                conn.execute(
                    _RRF_SQL,
                    {
                        "qvec": json.dumps(qvec) if qvec is not None else None,
                        "has_vec": qvec is not None,
                        "qts": to_or_tsquery(query),
                        "owner_id": owner_uuid,
                        "company_id": company_uuid,
                        "pool": pool,
                        "rrf_k": RRF_K,
                        "k": k,
                    },
                )
                .mappings()
                .all()
            )
        return [
            Document(
                id=str(r["chunk_id"]),
                page_content=r["text"],
                metadata={
                    "chunk_id": r["chunk_id"],
                    "source_id": r["source_id"],
                    "company": company_id,
                    "page": r["page"],
                    "section": r["section"],
                },
            )
            for r in rows
        ]

    return search
