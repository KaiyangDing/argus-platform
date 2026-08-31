"""追问对话图：纯函数面 + 全 Fake 端到端（检索式改写 / 零证据 / token 级流式）。

FakeListChatModel 响应序与图内调用一一对应：condense 消耗第 1 条（P3.4 起
**首问也改写**——泛问词面不与语料对撞的验收实证）、answer 消耗第 2 条；
零证据分支用「不应被调用」哨兵证明 answer 零消耗（FakeListChatModel
耗尽后循环回到首条而非报错，断言"哨兵未浮出"比断言队列长度可靠）。
token 级流式按 stream_mode=["messages", "updates"] 双模式消费：
messages 供 token（metadata.langgraph_node 过滤节点），updates 供终态。
"""

from langchain_core.documents import Document
from langchain_core.language_models import FakeListChatModel

from app.engine.chat import (
    CHAT_K,
    NO_EVIDENCE_ANSWER,
    build_chat_graph,
    render_history,
)


def _doc(cid: str, text: str) -> Document:
    return Document(
        page_content=text,
        metadata={"chunk_id": cid, "source_id": "src", "page": 3, "company": "t"},
    )


def _history() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "2024 年营收多少？"},
        {"role": "assistant", "content": "营业收入为 5 亿元 [1]。"},
    ]


def test_render_history_strips_citations() -> None:
    assert render_history([]) == "（无）"
    out = render_history(_history())
    assert out == "用户：2024 年营收多少？\n助手：营业收入为 5 亿元 。"
    assert "[1]" not in out  # 历史里的编号属各自轮次的证据表，留着会诱导错引


def test_first_question_condensed_to_search_query() -> None:
    """首问也过改写：泛问「财务表现如何」词面不与利润表对撞（三只松鼠实证），
    condense 负责翻译成语料里会出现的指标词，检索用改写结果而非原问。"""
    seen: list[str] = []

    def search(query: str, slug: str, k: int) -> list[Document]:
        assert k == CHAT_K
        seen.append(query)
        return [_doc("c1", "营收证据")]

    chat = FakeListChatModel(
        responses=["营业收入 净利润 毛利率", "营收为 5 亿元 [1]。"]
    )
    graph = build_chat_graph(chat, search).compile()
    out = graph.invoke({"company": "测试公司", "slug": "t", "question": "财务表现如何"})

    assert seen == ["营业收入 净利润 毛利率"]  # 检索用改写后的查询式，不用原问
    assert out["condensed"] == "营业收入 净利润 毛利率"
    assert out["answer"] == "营收为 5 亿元 [1]。"


def test_followup_searches_with_condensed_query() -> None:
    seen: list[str] = []

    def search(query: str, slug: str, k: int) -> list[Document]:
        seen.append(query)
        return [_doc("c1", "毛利率证据")]

    chat = FakeListChatModel(
        responses=["测试公司 2024 年毛利率", "毛利率为 12.3% [1]。"]
    )
    graph = build_chat_graph(chat, search).compile()
    out = graph.invoke(
        {
            "company": "测试公司",
            "slug": "t",
            "history": _history(),
            "question": "那毛利率呢",
        }
    )

    assert seen == ["测试公司 2024 年毛利率"]  # 检索用改写后的独立查询，不用原追问
    assert out["condensed"] == "测试公司 2024 年毛利率"
    assert out["answer"] == "毛利率为 12.3% [1]。"


def test_zero_evidence_returns_fixed_answer_without_llm() -> None:
    chat = FakeListChatModel(responses=["改写查询", "不应被调用"])
    graph = build_chat_graph(chat, lambda q, s, k: []).compile()
    out = graph.invoke(
        {"company": "测试公司", "slug": "t", "history": _history(), "question": "追问"}
    )

    assert out["evidence"] == []
    assert out["answer"] == NO_EVIDENCE_ANSWER  # 哨兵未浮出 = answer 没调 LLM


def test_retrieve_dedupes_by_chunk_id() -> None:
    def search(query: str, slug: str, k: int) -> list[Document]:
        return [_doc("c1", "一"), _doc("c2", "二"), _doc("c1", "一的重复")]

    chat = FakeListChatModel(responses=["改写查询", "回答 [1][2]。"])
    graph = build_chat_graph(chat, search).compile()
    out = graph.invoke({"company": "测试公司", "slug": "t", "question": "问"})

    assert [e["chunk_id"] for e in out["evidence"]] == ["c1", "c2"]


async def test_token_streaming_from_answer_node() -> None:
    """P2 的新流式层：token 级（messages），与 P1.5 节点级（updates）不同层。"""
    chat = FakeListChatModel(responses=["改写查询", "毛利率为 12.3% [1]。"])
    graph = build_chat_graph(chat, lambda q, s, k: [_doc("c1", "毛利率证据")]).compile()

    tokens: list[str] = []
    final: dict[str, object] = {}
    async for mode, payload in graph.astream(
        {
            "company": "测试公司",
            "slug": "t",
            "history": _history(),
            "question": "那毛利率呢",
        },
        stream_mode=["messages", "updates"],
    ):
        if mode == "messages":
            chunk, meta = payload
            if meta.get("langgraph_node") == "answer":  # condense 的 token 不外泄
                tokens.append(str(chunk.content))
        elif "answer" in payload:
            final = payload["answer"]

    assert len(tokens) > 1  # 真流式：多片 token 而非整段一次
    assert "".join(tokens) == "毛利率为 12.3% [1]。"
    assert final["answer"] == "毛利率为 12.3% [1]。"  # updates 终态与 token 拼接一致
