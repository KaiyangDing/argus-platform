"""检索层：BM25(jieba) + 向量 + RRF 混合，深池检索再切 top-k。

设计源自研究仓 S3 曲线结论（产品化重写）：
- BM25Retriever 必挂 jieba preprocess_func——默认按空白切词，中文整句
  成一个 token，检索直接退化（社区件中文隐坑）
- 深池 HYBRID_POOL=200 先检索、融合后再切 k：RRF 在小池下会稀释单路命中
- 产品差异：per-company 索引天然单公司，SearchFn 的 company 参数仅保
  签名兼容（图代码不动），闭包已绑定公司
分层：build_hybrid_search 纯内存（单测直测），make_company_search 做 IO 组装。
"""

from collections.abc import Callable

import jieba
from langchain_classic.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore

from app.ingest import load_company_rows, load_company_store

SearchFn = Callable[[str, str, int], list[Document]]

HYBRID_POOL = 200


def jieba_tokenize(text: str) -> list[str]:
    return [tok for tok in jieba.lcut(text) if tok.strip()]


def rows_to_documents(rows: list[dict[str, object]]) -> list[Document]:
    """chunks.jsonl 行 → LC Document（id=chunk_id，metadata 取检索所需子集）。"""
    return [
        Document(
            id=str(row["chunk_id"]),
            page_content=str(row["text"]),
            metadata={k: row[k] for k in ("source_id", "company", "page", "chunk_id")},
        )
        for row in rows
    ]


def build_hybrid_search(
    docs: list[Document], store: InMemoryVectorStore, pool: int = HYBRID_POOL
) -> SearchFn:
    """BM25 建索引一次（jieba 分词是大头），向量与融合按查询现组（轻量）。"""
    bm25 = BM25Retriever.from_documents(docs, preprocess_func=jieba_tokenize)
    bm25.k = pool

    def search(query: str, _slug: str, k: int) -> list[Document]:
        vector = store.as_retriever(search_kwargs={"k": pool})
        hybrid = EnsembleRetriever(retrievers=[bm25, vector], weights=[0.5, 0.5])
        return hybrid.invoke(query)[:k]

    return search


def make_company_search(
    owner_id: str,
    company_id: str,
    embeddings: Embeddings,
    pool: int = HYBRID_POOL,
) -> SearchFn:
    """载入公司索引并构建 SearchFn；语料为空返回恒空函数（图侧走零证据分支）。"""
    rows = load_company_rows(owner_id, company_id)
    docs = rows_to_documents(rows)
    if not docs:

        def empty(_query: str, _slug: str, _k: int) -> list[Document]:
            return []

        return empty

    store = load_company_store(owner_id, company_id, embeddings)
    return build_hybrid_search(docs, store, pool)
