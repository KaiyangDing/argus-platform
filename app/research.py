"""研究图 v0.2：supervisor → Send 并行多轮 researcher → merge → review 补派 → 分层 write。

设计源自研究仓 argus-lg v0.2（产品内重写，v02_devlog 工程问题账处方集成）：
- researcher 从单轮流水线升级为多轮 map-reduce agent：每轮 检索→备忘录消化→缺口自评，
  总证据无上限（全语料可及），单次调用证据窗有界（撞墙⑤实测边界：单窗过大引用纪律崩塌）；
- supervisor 产出带 key_questions 的研究框架（五维尽调基线）；语料概况数据驱动注入
  （profile_fn 缝——产品语料是用户任意上传，年份等先验不可预设，只能来自语料实况）；
- merge 同名归并；review 复审补派一轮（≤2 任务，revised 闩防循环）；
- write 三层化：投资要点 / 各节三段式（事实→分析→风险）/ 跨方面关联 / 风险汇总 / 边界；
  写作前一致性核对 pass；备忘录〔chunk_id〕由代码映射全局 [n]——编号翻译不交给 LLM；
- LLM 节点挂 RetryPolicy(3)：瞬态 API 错误重跑节点不杀整跑；编程错误（含 RetryingStruct
  耗尽的 ValueError）默认谓词不重试，快速失败（见 app/llm.py 三层防线分工）。
防幻觉闸不动：零证据不调 LLM、只依据给定证据、数字逐字一致。
产品映射：state.company=公司中文名（进 prompt），state.slug=str(company_id)（进检索，
per-company 索引下闭包忽略该值，仅保签名兼容）。
"""

import operator
import re
from collections.abc import Callable, Sequence
from typing import Annotated, TypedDict

from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send
from pydantic import BaseModel

from app.llm import RetryingStruct
from app.prompts import (
    BOUNDARY_PROMPT,
    CONSISTENCY_PROMPT,
    DIGEST_PROMPT,
    EXEC_SUMMARY_PROMPT,
    QUERIES_PROMPT,
    REFLECT_PROMPT,
    REVIEW_PROMPT,
    RISK_PROMPT,
    SECTION_PROMPT,
    SUPERVISOR_PROMPT,
    SYNTHESIS_PROMPT,
    AspectPlan,
    QueryList,
    Reflection,
    ReviewVerdict,
)
from app.retrieval import SearchFn

K_PER_QUERY = 12
MAX_ROUNDS = 3  # 每方面 检索→消化→自评 轮数上限
ROUND_INTAKE = 18  # 每轮进备忘录的新证据上限（单窗有界）
CORE_CAP = 24  # 进写作证据表的核心证据上限（单窗有界）
MIN_ASPECTS = 2
MAX_ASPECTS = 5
MAX_FOLLOWUPS = 2
WRITE_CONCURRENCY = 4  # 分节/终稿 batch 并发上限；与 worker max_jobs=4 相乘，
# 调用侧峰值并发 ≈16，qwen-flash 默认配额可容

_MEMO_REF_RE = re.compile(r"〔([^〔〕]+)〕")


class Aspect(TypedDict):
    aspect_id: str
    name: str
    focus: str
    key_questions: list[str]


class EvidenceRef(TypedDict, total=False):
    chunk_id: str
    source_id: str
    page: int
    section: str  # 跨页运行章节头（面包屑），空串=未知
    text: str


class Finding(TypedDict):
    aspect_id: str
    name: str
    focus: str
    key_questions: list[str]
    summary: str  # 方面研究备忘录（〔chunk_id〕引用形态）
    evidence: list[EvidenceRef]  # 核心证据（researcher 缺口自评精选，≤CORE_CAP）


class Section(TypedDict):
    aspect_id: str
    name: str
    summary: str
    refs: list[int]


class ResearchState(TypedDict, total=False):
    company: str  # 公司中文名，进 prompt
    slug: str  # 检索域标识，产品=str(company_id)
    corpus_profile: str  # 语料概况（数据驱动生成，时间锚等先验的唯一来源）
    aspects: list[Aspect]
    findings: Annotated[list[Finding], operator.add]
    evidence: list[EvidenceRef]
    sections: list[Section]
    revised: bool  # 复审补派只做一轮的闩
    followup_aspects: list[Aspect]
    report: str


class ResearcherInput(TypedDict):
    company: str
    slug: str
    corpus_profile: str
    aspect: Aspect


def assign_aspect_ids(plan: AspectPlan, prefix: str = "r") -> list[Aspect]:
    """aspect_id 由代码按序钉 r1..rN / f1..fN（不信 LLM 编号）；补派前缀免检最小数。"""
    specs = plan.aspects[:MAX_ASPECTS]
    if prefix == "r" and len(specs) < MIN_ASPECTS:
        raise ValueError(f"supervisor 拆解不足 {MIN_ASPECTS} 个方面：{len(specs)}")
    return [
        {
            "aspect_id": f"{prefix}{i}",
            "name": s.name,
            "focus": s.focus,
            "key_questions": list(s.key_questions),
        }
        for i, s in enumerate(specs, start=1)
    ]


def doc_to_ref(doc: Document) -> EvidenceRef:
    return {
        "chunk_id": str(doc.metadata["chunk_id"]),
        "source_id": str(doc.metadata["source_id"]),
        "page": int(doc.metadata["page"]),
        "section": str(doc.metadata.get("section", "")),
        "text": doc.page_content,
    }


def _loc(ev: EvidenceRef) -> str:
    """证据定位串：来源 p页 [· 章节头]——面包屑随证据渲染进所有 LLM 窗口。"""
    sec = ev.get("section", "")
    return f"{ev['source_id']} p{ev['page']}" + (f" · {sec}" if sec else "")


def render_questions(questions: Sequence[str]) -> str:
    return "\n".join(f"- {q}" for q in questions)


def render_by_chunk_id(refs: Sequence[EvidenceRef]) -> str:
    return "\n".join(f"〔{ev['chunk_id']}〕 ({_loc(ev)}) {ev['text']}" for ev in refs)


def map_memo_refs(memo: str, numbers: dict[str, int]) -> str:
    """备忘录〔chunk_id〕→ 全局 [n]；未入全局表的引用剥除（编号翻译不交给 LLM）。"""

    def _sub(m: re.Match[str]) -> str:
        n = numbers.get(m.group(1))
        return f"[{n}]" if n else ""

    return _MEMO_REF_RE.sub(_sub, memo)


def merge_findings(findings: list[Finding]) -> tuple[list[EvidenceRef], list[Section]]:
    """按 aspect_id 排序（并行到达序不定），跨方面按 chunk_id 去重并全局编号。

    同名方面归并为一节（研究仓 iteration-1 病：复审补研与原方面同名 → 章节重复且
    互相矛盾）：补研备忘录以【补充研究】拼入原节，证据并集。
    """
    ordered = sorted(findings, key=lambda f: f["aspect_id"])
    numbers: dict[str, int] = {}
    evidence: list[EvidenceRef] = []
    sections: list[Section] = []
    by_name: dict[str, Section] = {}
    for f in ordered:
        refs: list[int] = []
        for ev in f["evidence"]:
            cid = ev["chunk_id"]
            if cid not in numbers:
                evidence.append(ev)
                numbers[cid] = len(evidence)
            refs.append(numbers[cid])
        existing = by_name.get(f["name"])
        if existing is not None:
            existing["summary"] += "\n\n【补充研究】\n" + f["summary"]
            existing["refs"].extend(r for r in refs if r not in existing["refs"])
        else:
            section: Section = {
                "aspect_id": f["aspect_id"],
                "name": f["name"],
                "summary": f["summary"],
                "refs": refs,
            }
            by_name[f["name"]] = section
            sections.append(section)
    return evidence, sections


def render_evidence_subset(evidence: Sequence[EvidenceRef], refs: Sequence[int]) -> str:
    """按全局编号渲染本节证据子集（编号保留全局值，跨节引用语义一致）。"""
    return "\n".join(
        f"[{n}] ({_loc(evidence[n - 1])}) {evidence[n - 1]['text']}"
        for n in sorted(set(refs))
    )


def build_graph(
    chat: BaseChatModel,
    search: SearchFn,
    struct_factory: Callable[[type[BaseModel]], object] | None = None,
    write_chat: BaseChatModel | None = None,
    profile_fn: Callable[[str], str] | None = None,
) -> StateGraph:
    """write_chat：写作层可单独换更强模型（A/B 用）。profile_fn：slug→语料概况
    （产品侧从 Document 表生成同款概况，图零改动——研究仓留的产品对接缝）。"""
    writer = write_chat or chat
    default_profile = (
        "（未提供语料概况：年份与文档类型未知，先以宽泛查询探明时间范围。）"
    )

    def _struct(model_cls: type[BaseModel]) -> object:
        if struct_factory is not None:
            return struct_factory(model_cls)
        return RetryingStruct(chat, model_cls)

    def supervisor(state: ResearchState) -> dict[str, object]:
        profile = state.get("corpus_profile") or (
            profile_fn(state["slug"]) if profile_fn else default_profile
        )
        msgs = SUPERVISOR_PROMPT.invoke(
            {"company": state["company"], "corpus_profile": profile}
        )
        plan = _struct(AspectPlan).invoke(msgs)
        return {"aspects": assign_aspect_ids(plan), "corpus_profile": profile}

    def _send(state: ResearchState, aspect: Aspect) -> Send:
        return Send(
            "researcher",
            {
                "company": state["company"],
                "slug": state["slug"],
                "corpus_profile": state["corpus_profile"],
                "aspect": aspect,
            },
        )

    def fan_out(state: ResearchState) -> list[Send]:
        return [_send(state, a) for a in state["aspects"]]

    def researcher(state: ResearcherInput) -> dict[str, object]:
        aspect = state["aspect"]
        questions = render_questions(aspect["key_questions"])
        msgs = QUERIES_PROMPT.invoke(
            {
                "company": state["company"],
                "name": aspect["name"],
                "focus": aspect["focus"],
                "corpus_profile": state["corpus_profile"],
                "key_questions": questions,
            }
        )
        queries = _struct(QueryList).invoke(msgs).queries[:4]

        evidence_map: dict[str, EvidenceRef] = {}
        past_queries: list[str] = []
        memo = "（空）"
        core_ids: list[str] = []
        for rnd in range(MAX_ROUNDS):
            new_refs: list[EvidenceRef] = []
            for q in queries:
                past_queries.append(q)
                for doc in search(q, state["slug"], K_PER_QUERY):
                    ref = doc_to_ref(doc)
                    if ref["chunk_id"] not in evidence_map:
                        evidence_map[ref["chunk_id"]] = ref
                        new_refs.append(ref)
            if not evidence_map:
                break  # 全空：证据不足路径，不烧后续调用
            new_refs = new_refs[:ROUND_INTAKE]
            if new_refs:
                msgs = DIGEST_PROMPT.invoke(
                    {
                        "name": aspect["name"],
                        "focus": aspect["focus"],
                        "key_questions": questions,
                        "memo": memo,
                        "evidence": render_by_chunk_id(new_refs),
                    }
                )
                memo = str(chat.invoke(msgs).content)
            msgs = REFLECT_PROMPT.invoke(
                {
                    "name": aspect["name"],
                    "focus": aspect["focus"],
                    "corpus_profile": state["corpus_profile"],
                    "key_questions": questions,
                    "past_queries": "\n".join(f"- {q}" for q in past_queries),
                    "memo": memo,
                }
            )
            refl = _struct(Reflection).invoke(msgs)
            core_ids = [cid for cid in refl.core_chunk_ids if cid in evidence_map][
                :CORE_CAP
            ]
            if refl.done or not refl.queries or rnd == MAX_ROUNDS - 1:
                break
            queries = refl.queries[:3]

        if not evidence_map:
            memo = "证据不足：检索未命中相关语料。"
            core: list[EvidenceRef] = []
        else:
            # 自评未给出可用核心集时回退：按入库序（融合排名序）取前 CORE_CAP
            picked = core_ids or list(evidence_map)[:CORE_CAP]
            core = [evidence_map[cid] for cid in picked]
        finding: Finding = {
            "aspect_id": aspect["aspect_id"],
            "name": aspect["name"],
            "focus": aspect["focus"],
            "key_questions": aspect["key_questions"],
            "summary": memo,
            "evidence": core,
        }
        return {"findings": [finding]}

    def merge(state: ResearchState) -> dict[str, object]:
        evidence, sections = merge_findings(state["findings"])
        return {"evidence": evidence, "sections": sections}

    def review(state: ResearchState) -> dict[str, object]:
        if state.get("revised"):
            return {"followup_aspects": []}
        memos = "\n\n".join(
            f"### {f['name']}（{f['aspect_id']}）\n{f['summary']}"
            for f in state["findings"]
        )
        questions = "\n\n".join(
            f"### {a['name']}\n{render_questions(a['key_questions'])}"
            for a in state["aspects"]
        )
        msgs = REVIEW_PROMPT.invoke(
            {"company": state["company"], "memos": memos, "questions": questions}
        )
        verdict = _struct(ReviewVerdict).invoke(msgs)
        followups: list[Aspect] = []
        if verdict.need_more and verdict.followups:
            plan = AspectPlan(aspects=verdict.followups[:MAX_FOLLOWUPS])
            followups = assign_aspect_ids(plan, prefix="f")
        return {"revised": True, "followup_aspects": followups}

    def route_after_review(state: ResearchState) -> list[Send] | str:
        followups = state.get("followup_aspects") or []
        if followups:
            return [_send(state, a) for a in followups]
        return "write"

    def write(state: ResearchState) -> dict[str, object]:
        evidence = state["evidence"]
        numbers = {ev["chunk_id"]: i + 1 for i, ev in enumerate(evidence)}
        # 一致性核对 pass（研究仓 iteration-1 病：并行研究员对同一指标矛盾数字无人调停）
        memos_mapped = "\n\n".join(
            f"### {s['name']}\n{map_memo_refs(s['summary'], numbers)}"
            for s in state["sections"]
        )
        conflicts = str(
            writer.invoke(
                CONSISTENCY_PROMPT.invoke(
                    {"company": state["company"], "memos": memos_mapped}
                )
            ).content
        )
        # 分节生成并发化（P3.4）：各节只依赖共享的 conflicts 与本节证据子集，
        # 互不依赖——batch 并发；零证据节仍是备忘录占位，不进 batch 不烧调用
        rendered: list[str | None] = []
        batch_inputs = []
        for section in state["sections"]:
            memo_mapped = map_memo_refs(section["summary"], numbers)
            if not section["refs"]:
                rendered.append(memo_mapped)
                continue
            aspect_qs = next(
                (
                    render_questions(f["key_questions"])
                    for f in state["findings"]
                    if f["aspect_id"] == section["aspect_id"]
                ),
                "",
            )
            focus = next(
                (
                    f["focus"]
                    for f in state["findings"]
                    if f["aspect_id"] == section["aspect_id"]
                ),
                "",
            )
            batch_inputs.append(
                SECTION_PROMPT.invoke(
                    {
                        "company": state["company"],
                        "name": section["name"],
                        "focus": focus,
                        "key_questions": aspect_qs,
                        "conflicts": conflicts,
                        "memo": memo_mapped,
                        "evidence": render_evidence_subset(evidence, section["refs"]),
                    }
                )
            )
            rendered.append(None)
        if batch_inputs:
            # batch 返回序 = 输入序（langchain 保序），按占位序回填
            outs = iter(
                writer.batch(
                    batch_inputs, config={"max_concurrency": WRITE_CONCURRENCY}
                )
            )
            rendered = [str(next(outs).content) if r is None else r for r in rendered]
        section_parts = [
            f"## {s['name']}\n{body}"
            for s, body in zip(state["sections"], rendered, strict=True)
        ]

        body_md = "\n\n".join(section_parts)
        extras = {"company": state["company"], "body": body_md}
        # 四终稿都只吃 body_md、互不依赖——第二个 batch（依赖节批完成，两批有先后）
        finals = writer.batch(
            [
                EXEC_SUMMARY_PROMPT.invoke(extras),
                SYNTHESIS_PROMPT.invoke(extras),
                RISK_PROMPT.invoke(extras),
                BOUNDARY_PROMPT.invoke(extras),
            ],
            config={"max_concurrency": WRITE_CONCURRENCY},
        )
        exec_summary, synthesis, risks, boundary = (str(m.content) for m in finals)
        report = (
            f"# {state['company']} 尽调报告\n\n## 投资要点\n{exec_summary}\n\n{body_md}\n\n"
            f"## 跨方面关联分析\n{synthesis}\n\n## 风险因素\n{risks}\n\n"
            f"## 证据不足与边界\n{boundary}"
        )
        return {"report": report}

    # 节点级重试（研究仓 iteration-3 事故：researcher 单次 API 超时三连杀死整跑）——
    # langgraph 内置 RetryPolicy，LLM 节点全挂；merge 纯代码不挂
    llm_retry = RetryPolicy(max_attempts=3)
    g = StateGraph(ResearchState)
    g.add_node("supervisor", supervisor, retry_policy=llm_retry)
    g.add_node("researcher", researcher, retry_policy=llm_retry)
    g.add_node("merge", merge)
    g.add_node("review", review, retry_policy=llm_retry)
    g.add_node("write", write, retry_policy=llm_retry)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", fan_out, ["researcher"])
    g.add_edge("researcher", "merge")
    g.add_edge("merge", "review")
    g.add_conditional_edges("review", route_after_review, ["researcher", "write"])
    g.add_edge("write", END)
    return g
