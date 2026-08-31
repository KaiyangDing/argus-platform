"""追问对话图：condense → retrieve → answer 三节点线性图（P2 第二核心功能）。

独立轻量图，不复用研究图（PLAN P2 约束）：追问是秒级交互，研究是分钟级
批处理——前者在 API 进程内联执行、token 级流式（stream_mode="messages"），
后者走 arq worker、节点级进度事件（stream_mode="updates"）。
两图共用证据工具（doc_to_ref / render_evidence_subset）与 SearchFn 契约。

- condense：把提问（含首问）改写成检索式查询——消解指代、把宽泛评价性
  提问展开为语料词面（营业收入/净利润），研究图 QUERIES_PROMPT 的同款翻译层；
- retrieve：SearchFn 契约检索同公司语料（与研究图同一套索引）；
- answer：只依据本轮证据回答，句末 [n] 引用，证据不足必须明说；
  零证据固定文案直接回，不调 LLM。
历史渲染剥掉 [n] 标记：历史里的编号对应各自轮次的证据表，与本轮
证据表编号空间不同，留着会诱导错引。
"""

import re
from typing import TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.engine.prompts import CHAT_ANSWER_PROMPT, CONDENSE_PROMPT
from app.engine.research import EvidenceRef, doc_to_ref, render_evidence_subset
from app.engine.retrieval import SearchFn

CHAT_K = 8  # 单查询单窗：远小于研究图多轮预算，够答一问且引用纪律稳
HISTORY_LIMIT = 8  # 进 prompt 的历史消息条数上限（4 轮对话）

NO_EVIDENCE_ANSWER = (
    "证据不足：未在该公司语料中检索到与追问相关的内容。"
    "请换个问法，或先上传相关文档后再试。"
)

_CITE_RE = re.compile(r"\[\d+\]")


class ChatMsg(TypedDict):
    role: str  # user | assistant
    content: str


class ChatState(TypedDict, total=False):
    company: str  # 公司中文名，进 prompt
    slug: str  # 检索域标识，产品=str(company_id)
    history: list[ChatMsg]  # 已存对话历史（不含本次追问）
    question: str
    condensed: str  # 改写后的独立检索查询
    evidence: list[EvidenceRef]  # 本轮检索证据，按序编号 [1..n]
    answer: str


def render_history(history: list[ChatMsg]) -> str:
    """历史渲染进 prompt：截尾 HISTORY_LIMIT 条并剥掉 [n] 引用标记。"""
    recent = history[-HISTORY_LIMIT:]
    if not recent:
        return "（无）"
    roles = {"user": "用户", "assistant": "助手"}
    return "\n".join(
        f"{roles.get(m['role'], m['role'])}：{_CITE_RE.sub('', m['content'])}"
        for m in recent
    )


def build_chat_graph(chat: BaseChatModel, search: SearchFn) -> StateGraph:
    def condense(state: ChatState) -> dict[str, object]:
        # 首问也过改写（三只松鼠验收实证：「财务表现如何」词面不与利润表
        # 对撞，词法向量两路全空手）——泛问 → 检索式的翻译层，研究图靠
        # QUERIES_PROMPT 做同一件事，chat 此前缺这一环；代价 = 首问多一次
        # 秒级小调用
        msgs = CONDENSE_PROMPT.invoke(
            {
                "company": state["company"],
                "history": render_history(state.get("history") or []),
                "question": state["question"],
            }
        )
        return {"condensed": str(chat.invoke(msgs).content)}

    def retrieve(state: ChatState) -> dict[str, object]:
        evidence: list[EvidenceRef] = []
        seen: set[str] = set()
        for doc in search(state["condensed"], state["slug"], CHAT_K):
            ref = doc_to_ref(doc)
            if ref["chunk_id"] not in seen:
                seen.add(ref["chunk_id"])
                evidence.append(ref)
        return {"evidence": evidence}

    def answer(state: ChatState) -> dict[str, object]:
        evidence = state["evidence"]
        if not evidence:
            return {"answer": NO_EVIDENCE_ANSWER}  # 零证据不烧调用
        msgs = CHAT_ANSWER_PROMPT.invoke(
            {
                "company": state["company"],
                "history": render_history(state.get("history") or []),
                "question": state["question"],
                "evidence": render_evidence_subset(
                    evidence, list(range(1, len(evidence) + 1))
                ),
            }
        )
        return {"answer": str(chat.invoke(msgs).content)}

    # retrieve 含 embed_query 网络调用，与 LLM 节点同样吃瞬态错误，一并挂重试
    llm_retry = RetryPolicy(max_attempts=3)
    g = StateGraph(ChatState)
    g.add_node("condense", condense, retry_policy=llm_retry)
    g.add_node("retrieve", retrieve, retry_policy=llm_retry)
    g.add_node("answer", answer, retry_policy=llm_retry)
    g.add_edge(START, "condense")
    g.add_edge("condense", "retrieve")
    g.add_edge("retrieve", "answer")
    g.add_edge("answer", END)
    return g
