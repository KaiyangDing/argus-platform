"""研究图 v0.2：纯函数面 + 全 Fake 端到端（多轮 researcher / 复审补派 / 分层写作）。

同步执行下并行任务按提交序依次运行，struct 队列消费顺序确定：
r1 全流程 → r2 全流程 → … → merge → review → (补派) → write。
FakeListChatModel 响应序与图内非结构化调用一一对应（结构化调用走 struct 桩不消耗）：
各方面 digest（每轮一次）→ 一致性核对 → 各节正文 → 要点 → 关联 → 风险 → 边界。
"""

from collections import deque

import pytest
from langchain_core.documents import Document
from langchain_core.language_models import FakeListChatModel
from pydantic import BaseModel

from app.prompts import AspectPlan, AspectSpec, QueryList, Reflection, ReviewVerdict
from app.research import (
    assign_aspect_ids,
    build_graph,
    map_memo_refs,
    merge_findings,
    render_by_chunk_id,
    render_evidence_subset,
)


def scripted_struct_factory(script: dict[type[BaseModel], list[BaseModel]]):
    """按模型类分队列的脚本桩：多轮/补派场景下同一模型类被多次请求，按序出队。"""
    queues = {cls: deque(items) for cls, items in script.items()}

    class Scripted:
        def __init__(self, cls: type[BaseModel]) -> None:
            self._cls = cls

        def invoke(self, _input: object) -> BaseModel:
            return queues[self._cls].popleft()

    Scripted.queues = queues  # type: ignore[attr-defined]  # 供测试断言队列耗尽
    return Scripted


def _spec(name: str) -> AspectSpec:
    return AspectSpec(
        name=name, focus=f"{name}目标", key_questions=[f"{name}问1", f"{name}问2"]
    )


def _doc(cid: str, text: str) -> Document:
    return Document(
        page_content=text,
        metadata={"chunk_id": cid, "source_id": "src", "page": 3, "company": "t"},
    )


def _search(query: str, slug: str, k: int) -> list[Document]:
    assert k == 12
    return [_doc(f"c-{query}", f"{query} 的证据")]


def test_assign_aspect_ids_clamps_numbers_and_prefixes() -> None:
    plan = AspectPlan(aspects=[_spec(f"a{i}") for i in range(7)])
    aspects = assign_aspect_ids(plan)
    assert [a["aspect_id"] for a in aspects] == ["r1", "r2", "r3", "r4", "r5"]
    assert aspects[0]["key_questions"] == ["a0问1", "a0问2"]

    follow = assign_aspect_ids(AspectPlan(aspects=[_spec("补")]), prefix="f")
    assert follow[0]["aspect_id"] == "f1"  # followup 前缀不受 MIN_ASPECTS 限制

    with pytest.raises(ValueError, match="不足"):
        assign_aspect_ids(AspectPlan(aspects=[_spec("唯一")]))


def test_map_memo_refs_maps_known_strips_unknown() -> None:
    memo = "营收下降〔ar-2024:15〕；净利未知〔ghost:1〕。"
    assert map_memo_refs(memo, {"ar-2024:15": 3}) == "营收下降[3]；净利未知。"


def test_merge_findings_orders_dedups_numbers() -> None:
    f_r2 = {
        "aspect_id": "r2",
        "name": "乙",
        "summary": "s2",
        "evidence": [
            {"chunk_id": "c9", "source_id": "s", "page": 1, "text": "九"},
            {"chunk_id": "c1", "source_id": "s", "page": 1, "text": "一"},
        ],
    }
    f_r1 = {
        "aspect_id": "r1",
        "name": "甲",
        "summary": "s1",
        "evidence": [{"chunk_id": "c1", "source_id": "s", "page": 1, "text": "一"}],
    }
    evidence, sections = merge_findings([f_r2, f_r1])
    assert [s["aspect_id"] for s in sections] == ["r1", "r2"]
    assert [e["chunk_id"] for e in evidence] == ["c1", "c9"]
    assert sections[0]["refs"] == [1]
    assert sections[1]["refs"] == [2, 1]


def test_merge_findings_same_name_folds_into_one_section() -> None:
    base = {
        "aspect_id": "r1",
        "name": "财务",
        "summary": "主备忘录",
        "evidence": [{"chunk_id": "c1", "source_id": "s", "page": 1, "text": "一"}],
    }
    followup = {
        "aspect_id": "f1",
        "name": "财务",  # 补研同名（研究仓 iteration-1 章节重复病）
        "summary": "补研备忘录",
        "evidence": [
            {"chunk_id": "c1", "source_id": "s", "page": 1, "text": "一"},
            {"chunk_id": "c2", "source_id": "s", "page": 2, "text": "二"},
        ],
    }
    evidence, sections = merge_findings([followup, base])
    assert len(sections) == 1  # 不再重复成章
    assert "【补充研究】" in sections[0]["summary"]
    assert "补研备忘录" in sections[0]["summary"]
    assert sorted(sections[0]["refs"]) == [1, 2]
    assert len(evidence) == 2


def test_render_helpers_with_and_without_section() -> None:
    ref = {"chunk_id": "a:0", "source_id": "ar-2024", "page": 8, "text": "营收"}
    assert render_by_chunk_id([ref]) == "〔a:0〕 (ar-2024 p8) 营收"
    assert render_evidence_subset([ref], [1, 1]) == "[1] (ar-2024 p8) 营收"

    with_sec = {**ref, "section": "母公司资产负债表"}
    assert (
        render_by_chunk_id([with_sec]) == "〔a:0〕 (ar-2024 p8 · 母公司资产负债表) 营收"
    )
    assert (
        render_evidence_subset([with_sec], [1])
        == "[1] (ar-2024 p8 · 母公司资产负债表) 营收"
    )


def test_graph_end_to_end_happy_path() -> None:
    factory = scripted_struct_factory(
        {
            AspectPlan: [AspectPlan(aspects=[_spec("财务"), _spec("事件")])],
            QueryList: [QueryList(queries=["查A"]), QueryList(queries=["查B"])],
            Reflection: [
                Reflection(done=True, core_chunk_ids=["c-查A"], queries=[], gaps=[]),
                Reflection(done=True, core_chunk_ids=["c-查B"], queries=[], gaps=[]),
            ],
            ReviewVerdict: [ReviewVerdict(need_more=False, followups=[])],
        }
    )
    # chat 序：digest r1、digest r2、一致性核对、节 r1、节 r2、要点、关联、风险、边界
    chat = FakeListChatModel(
        responses=[
            "备忘录甲：事实〔c-查A〕。",
            "备忘录乙：事实〔c-查B〕。",
            "冲突核对：无。",
            "节甲正文 [1]。",
            "节乙正文 [2]。",
            "要点。",
            "关联。",
            "风险。",
            "边界。",
        ]
    )
    graph = build_graph(chat, _search, struct_factory=factory).compile()
    out = graph.invoke({"company": "测试公司", "slug": "t"})

    assert len(out["findings"]) == 2
    assert len(out["evidence"]) == 2
    assert out["revised"] is True
    rpt = out["report"]
    assert rpt.startswith("# 测试公司 尽调报告")
    for heading in (
        "## 投资要点",
        "## 财务",
        "## 事件",
        "## 跨方面关联分析",
        "## 风险因素",
        "## 证据不足与边界",
    ):
        assert heading in rpt
    assert "节甲正文 [1]。" in rpt
    assert rpt.endswith("边界。")
    assert "〔c-查A〕" not in rpt  # 备忘录引用不得以原始形态漏进报告


def test_researcher_multi_round_accumulates_and_follows_gap_queries() -> None:
    seen_queries: list[str] = []

    def search(query: str, slug: str, k: int) -> list[Document]:
        seen_queries.append(query)
        return [_doc(f"c-{query}", f"{query} 的证据")]

    factory = scripted_struct_factory(
        {
            AspectPlan: [AspectPlan(aspects=[_spec("财务"), _spec("事件")])],
            QueryList: [QueryList(queries=["初查"]), QueryList(queries=["查B"])],
            Reflection: [
                # r1 第一轮：未完成，派补查
                Reflection(
                    done=False,
                    core_chunk_ids=["c-初查"],
                    queries=["补查"],
                    gaps=["缺 2024"],
                ),
                # r1 第二轮：完成，核心集含两轮证据
                Reflection(
                    done=True, core_chunk_ids=["c-初查", "c-补查"], queries=[], gaps=[]
                ),
                # r2 一轮完成
                Reflection(done=True, core_chunk_ids=["c-查B"], queries=[], gaps=[]),
            ],
            ReviewVerdict: [ReviewVerdict(need_more=False, followups=[])],
        }
    )
    # chat 序：r1 digest×2、r2 digest×1、一致性核对、节×2、要点、关联、风险、边界
    chat = FakeListChatModel(
        responses=[
            "备1〔c-初查〕",
            "备1v2〔c-初查〕〔c-补查〕",
            "备2〔c-查B〕",
            "冲突核对：无。",
            "节1 [1][2]。",
            "节2 [3]。",
            "要",
            "联",
            "险",
            "界",
        ]
    )
    graph = build_graph(chat, search, struct_factory=factory).compile()
    out = graph.invoke({"company": "测试公司", "slug": "t"})

    assert "补查" in seen_queries  # 缺口查询真的被执行
    r1 = next(f for f in out["findings"] if f["aspect_id"] == "r1")
    assert [e["chunk_id"] for e in r1["evidence"]] == ["c-初查", "c-补查"]  # 两轮累积
    assert len(out["evidence"]) == 3


def test_review_dispatches_followup_once() -> None:
    factory = scripted_struct_factory(
        {
            AspectPlan: [AspectPlan(aspects=[_spec("财务"), _spec("事件")])],
            QueryList: [
                QueryList(queries=["查A"]),
                QueryList(queries=["查B"]),
                QueryList(queries=["查补"]),  # 补研 researcher 的首轮查询
            ],
            Reflection: [
                Reflection(done=True, core_chunk_ids=["c-查A"], queries=[], gaps=[]),
                Reflection(done=True, core_chunk_ids=["c-查B"], queries=[], gaps=[]),
                Reflection(done=True, core_chunk_ids=["c-查补"], queries=[], gaps=[]),
            ],
            ReviewVerdict: [
                ReviewVerdict(need_more=True, followups=[_spec("上市进程")])
            ],
        }
    )
    # chat 序：digest×2 → (复审补派) digest×1 → 一致性核对 → 节×3 → 要点、关联、风险、边界
    chat = FakeListChatModel(
        responses=[
            "备A〔c-查A〕",
            "备B〔c-查B〕",
            "备补〔c-查补〕",
            "冲突核对：无。",
            "节A [1]。",
            "节B [2]。",
            "节补 [3]。",
            "要",
            "联",
            "险",
            "界",
        ]
    )
    graph = build_graph(chat, _search, struct_factory=factory).compile()
    out = graph.invoke({"company": "测试公司", "slug": "t"})

    assert len(out["findings"]) == 3
    assert {f["aspect_id"] for f in out["findings"]} == {"r1", "r2", "f1"}
    assert "## 上市进程" in out["report"]  # 补研独立成章
    assert not factory.queues[ReviewVerdict]  # 复审只做一轮：verdict 恰消费一次
    assert out["followup_aspects"] == []  # 第二次 review 被 revised 闩短路清空


def test_zero_evidence_skips_llm_calls() -> None:
    factory = scripted_struct_factory(
        {
            AspectPlan: [AspectPlan(aspects=[_spec("甲"), _spec("乙")])],
            QueryList: [QueryList(queries=["空1"]), QueryList(queries=["空2"])],
            Reflection: [],  # 零证据不许消费自评（提前 break，队列留空也能跑通）
            ReviewVerdict: [ReviewVerdict(need_more=False, followups=[])],
        }
    )
    # 一致性核对 + 四个终稿调用；节正文零调用（零证据节直接放备忘录占位）
    chat = FakeListChatModel(responses=["冲突核对：无。", "要", "联", "险", "界"])
    graph = build_graph(chat, lambda q, s, k: [], struct_factory=factory).compile()
    out = graph.invoke({"company": "测试公司", "slug": "t"})

    assert all("证据不足" in f["summary"] for f in out["findings"])
    assert out["evidence"] == []
    assert "证据不足：检索未命中相关语料。" in out["report"]
    assert out["report"].endswith("界")
