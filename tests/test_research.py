"""研究图单测：struct 桩 + FakeListChatModel + 假 search，全图零网络零花费。

覆盖：纯函数（钉号/并集/归并编号/子集渲染）、全链跑通（并行 reducer 汇入、
跨方面去重全局编号、报告结构）、零证据路径（不调写作 LLM、报告仍成文）。
"""

import pytest
from langchain_core.documents import Document
from langchain_core.language_models import FakeListChatModel
from pydantic import BaseModel

from app.research import (
    AspectPlan,
    AspectSpec,
    EvidenceRef,
    QueryList,
    assign_aspect_ids,
    build_graph,
    merge_findings,
    render_evidence_subset,
    union_evidence,
)


class _StructStub:
    def __init__(self, value: object) -> None:
        self.value = value

    def invoke(self, _msgs: object) -> object:
        return self.value


def _struct_factory(plan: AspectPlan) -> object:
    queries = QueryList(queries=["查询一", "查询二"])

    def factory(model_cls: type[BaseModel]) -> _StructStub:
        if model_cls is AspectPlan:
            return _StructStub(plan)
        if model_cls is QueryList:
            return _StructStub(queries)
        raise AssertionError(f"未预期的结构化请求：{model_cls}")

    return factory


def _doc(chunk_id: str, text: str) -> Document:
    source_id, _seq = chunk_id.split(":")
    return Document(
        id=chunk_id,
        page_content=text,
        metadata={
            "chunk_id": chunk_id,
            "source_id": source_id,
            "page": 1,
            "company": "c1",
        },
    )


_PLAN = AspectPlan(
    aspects=[
        AspectSpec(name="财务表现", focus="营收与利润如何"),
        AspectSpec(name="公司治理", focus="股权结构如何"),
    ]
)


def test_assign_aspect_ids_pins_and_caps() -> None:
    many = AspectPlan(
        aspects=[AspectSpec(name=f"方面{i}", focus="焦点") for i in range(7)]
    )
    aspects = assign_aspect_ids(many)
    assert [a["aspect_id"] for a in aspects] == ["r1", "r2", "r3", "r4", "r5"]

    too_few = AspectPlan(aspects=[AspectSpec(name="孤", focus="独")])
    with pytest.raises(ValueError, match="不足"):
        assign_aspect_ids(too_few)


def test_union_evidence_dedups_and_caps() -> None:
    a, b, c = _doc("s-1:0", "甲"), _doc("s-1:1", "乙"), _doc("s-2:0", "丙")
    out = union_evidence([[a, b], [b, c]], cap=2)
    assert [e["chunk_id"] for e in out] == ["s-1:0", "s-1:1"]


def test_merge_findings_global_numbering() -> None:
    shared: EvidenceRef = {
        "chunk_id": "s-1:0",
        "source_id": "s-1",
        "page": 1,
        "text": "共",
    }
    only_b: EvidenceRef = {
        "chunk_id": "s-2:0",
        "source_id": "s-2",
        "page": 2,
        "text": "独",
    }
    evidence, sections = merge_findings(
        [
            {
                "aspect_id": "r2",
                "name": "乙面",
                "summary": "s2",
                "evidence": [shared, only_b],
            },
            {"aspect_id": "r1", "name": "甲面", "summary": "s1", "evidence": [shared]},
        ]
    )
    # 按 aspect_id 排序后 r1 先编号：共享证据 = [1]，r2 引用同一编号再添新证据 [2]
    assert [s["aspect_id"] for s in sections] == ["r1", "r2"]
    assert sections[0]["refs"] == [1]
    assert sections[1]["refs"] == [1, 2]
    assert [e["chunk_id"] for e in evidence] == ["s-1:0", "s-2:0"]


def test_render_evidence_subset_keeps_global_numbers() -> None:
    evidence: list[EvidenceRef] = [
        {"chunk_id": "s-1:0", "source_id": "s-1", "page": 1, "text": "一"},
        {"chunk_id": "s-1:1", "source_id": "s-1", "page": 2, "text": "二"},
        {"chunk_id": "s-2:0", "source_id": "s-2", "page": 3, "text": "三"},
    ]
    rendered = render_evidence_subset(evidence, [3, 1, 3])
    assert rendered.splitlines() == ["[1] (s-1 p1) 一", "[3] (s-2 p3) 三"]


def test_full_graph_run() -> None:
    corpus = [_doc("s-1:0", "营业收入八百亿元。"), _doc("s-2:0", "股权结构集中。")]
    chat = FakeListChatModel(
        responses=[
            "小结甲",
            "小结乙",
            "正文引用 [1]。",
            "正文引用 [2]。",
            "边界与缺口说明",
        ]
    )
    graph = build_graph(
        chat=chat,
        search=lambda _q, _s, _k: corpus,
        struct_factory=_struct_factory(_PLAN),
    )
    state = graph.compile().invoke({"company": "测试公司", "slug": "c-1"})

    # 两方面共享同一批检索结果 → 跨方面去重后全局证据仍为 2 条
    assert [e["chunk_id"] for e in state["evidence"]] == ["s-1:0", "s-2:0"]
    assert len(state["findings"]) == 2
    assert [s["refs"] for s in state["sections"]] == [[1, 2], [1, 2]]

    report = state["report"]
    assert report.startswith("# 测试公司 尽调报告")
    assert "## 财务表现" in report
    assert "## 公司治理" in report
    assert report.rstrip().endswith("边界与缺口说明")
    assert "## 证据不足与边界" in report


def test_zero_evidence_path() -> None:
    chat = FakeListChatModel(responses=["边界：全域证据缺失"])
    graph = build_graph(
        chat=chat,
        search=lambda _q, _s, _k: [],
        struct_factory=_struct_factory(_PLAN),
    )
    state = graph.compile().invoke({"company": "测试公司", "slug": "c-1"})

    assert state["evidence"] == []
    report = state["report"]
    # 零证据：小结即占位文案，写作 LLM 未被调用（responses 只有 boundary 一条）
    assert report.count("证据不足：检索未命中相关语料。") == 2
    assert "## 证据不足与边界" in report
