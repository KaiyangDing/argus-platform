"""checkpointer 断点续跑（P3.5 批3）：图层语义锁定，InMemorySaver 零外部依赖。

锁两个恢复粒度：
- superstep 内（pending writes）：并行 researcher 部分失败，成功分支的
  写入已存 checkpoint，续跑只重跑失败分支——「一个研究员出错重跑 =
  全部研究员白烧」的解药；
- superstep 间：write 崩溃续跑，supervisor/researcher/merge/review 全部
  从 checkpoint 恢复零调用——「跑到第 18 分钟被杀重烧前 18 分钟」的解药。

炸点全用 ValueError：langgraph RetryPolicy 默认谓词不重试编程错误类，
节点一次失败即穿出，struct/chat 队列脚本保持确定（真实端点故障是
CircuitBreakerError/连接错，会先被节点重试 3 次再穿出，续跑语义相同）。
队列即断言：任何已完成节点被重跑都会 popleft 空队列炸 IndexError；
findings 的 operator.add reducer 若重复应用，len 断言也会抓到。
"""

from collections import deque
from collections.abc import Sequence

import pytest
from langchain_core.documents import Document
from langchain_core.language_models import SimpleChatModel
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.memory import InMemorySaver

from app.engine.prompts import AspectPlan, QueryList, Reflection, ReviewVerdict
from app.engine.research import build_graph
from tests.test_research import (
    _doc,
    _search,
    _spec,
    make_queue_chat,
    scripted_struct_factory,
)


def make_bomb_chat(responses: Sequence[str]) -> BaseChatModel:
    """出队遇 <BOOM> 哨兵即抛错的 queue chat：在指定消费位注入崩溃。"""
    queue = deque(responses)

    class _BombChat(SimpleChatModel):
        @property
        def _llm_type(self) -> str:
            return "bomb-chat"

        def _call(
            self,
            messages: list,
            stop: list[str] | None = None,
            run_manager: object = None,
            **kwargs: object,
        ) -> str:
            value = queue.popleft()
            if value == "<BOOM>":
                raise ValueError("write 阶段崩溃（模拟）")
            return value

    return _BombChat()


def _base_script() -> dict:
    return {
        AspectPlan: [AspectPlan(aspects=[_spec("财务"), _spec("事件")])],
        Reflection: [
            Reflection(done=True, core_chunk_ids=["c-查A"], queries=[], gaps=[]),
            Reflection(done=True, core_chunk_ids=["c-查B"], queries=[], gaps=[]),
        ],
        ReviewVerdict: [ReviewVerdict(need_more=False, followups=[])],
    }


def test_resume_reruns_only_failed_parallel_branch() -> None:
    """r2 检索炸：r1 的产出进 pending writes，续跑只重跑 r2。

    QueryList 队列恰 3 份（r1×1 + r2 首跑×1 + r2 续跑×1）：r1 若被重跑，
    第 4 次 popleft 空队列即红；findings==2 锁 add reducer 不重复应用。
    """
    bomb = {"armed": True}

    def search(query: str, slug: str, k: int) -> list[Document]:
        if bomb["armed"] and query == "炸":
            raise ValueError("检索失败（模拟）")
        return [_doc(f"c-{query}", f"{query} 的证据")]

    script = _base_script()
    script[QueryList] = [
        QueryList(queries=["查A"]),  # r1 首跑
        QueryList(queries=["炸"]),  # r2 首跑（search 炸）
        QueryList(queries=["查B"]),  # r2 续跑
    ]
    factory = scripted_struct_factory(script)
    chat = make_queue_chat(
        [
            "备A〔c-查A〕",
            "备B〔c-查B〕",
            "冲突核对：无。",
            "节财务 [1]。",
            "节事件 [2]。",
            "要点",
            "关联",
            "风险",
            "边界",
        ]
    )
    graph = build_graph(chat, search, struct_factory=factory).compile(
        checkpointer=InMemorySaver()
    )
    config = {"configurable": {"thread_id": "resume-branch"}}

    with pytest.raises(ValueError, match="检索失败"):
        graph.invoke({"company": "测试公司", "slug": "t"}, config)

    bomb["armed"] = False
    out = graph.invoke(None, config)

    assert len(out["findings"]) == 2
    assert {f["aspect_id"] for f in out["findings"]} == {"r1", "r2"}
    for cls in (AspectPlan, QueryList, Reflection, ReviewVerdict):
        assert not factory.queues[cls]
    rpt = out["report"]
    assert "节财务 [1]。" in rpt
    assert "节事件 [2]。" in rpt
    assert "## 证据不足与边界" in rpt


def test_resume_skips_all_completed_supersteps() -> None:
    """write 首调（一致性核对）炸：续跑时 supervisor/researcher/merge/review
    全部从 checkpoint 恢复零调用，只重跑 write。

    struct 四类队列首跑已耗尽——review 若被重跑，ReviewVerdict 空队列即红；
    终态快照的 next 为空且 report 与返回值一致（worker 落库窗口直取分支
    依赖的语义锚）。
    """
    factory = scripted_struct_factory(
        {
            **_base_script(),
            QueryList: [QueryList(queries=["查A"]), QueryList(queries=["查B"])],
        }
    )
    chat = make_bomb_chat(
        [
            "备A〔c-查A〕",
            "备B〔c-查B〕",
            "<BOOM>",
            "冲突核对：无。",
            "节财务 [1]。",
            "节事件 [2]。",
            "要点",
            "关联",
            "风险",
            "边界",
        ]
    )
    graph = build_graph(chat, _search, struct_factory=factory).compile(
        checkpointer=InMemorySaver()
    )
    config = {"configurable": {"thread_id": "resume-write"}}

    with pytest.raises(ValueError, match="write 阶段崩溃"):
        graph.invoke({"company": "测试公司", "slug": "t"}, config)

    out = graph.invoke(None, config)

    assert len(out["findings"]) == 2
    assert out["revised"] is True
    for cls in (AspectPlan, QueryList, Reflection, ReviewVerdict):
        assert not factory.queues[cls]
    assert "冲突核对" not in out["report"]  # 一致性输出不进报告，仅进 prompt

    snapshot = graph.get_state(config)
    assert snapshot.next == ()
    assert snapshot.values["report"] == out["report"]
    assert snapshot.values["evidence"] == out["evidence"]
