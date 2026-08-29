"""prompts 套件冒烟：变量名钉死。

手敲 prompt 里 {变量} 拼错，不在这里拦截就要到图运行时才炸；
结构化模型的默认值语义（Reflection/ReviewVerdict 可留空字段）一并锁定。
"""

from app.prompts import (
    BOUNDARY_PROMPT,
    CHAT_ANSWER_PROMPT,
    CONDENSE_PROMPT,
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
    Reflection,
    ReviewVerdict,
)

_EXPECTED = [
    (SUPERVISOR_PROMPT, {"company", "corpus_profile"}),
    (QUERIES_PROMPT, {"company", "name", "focus", "corpus_profile", "key_questions"}),
    (DIGEST_PROMPT, {"name", "focus", "key_questions", "memo", "evidence"}),
    (
        REFLECT_PROMPT,
        {"name", "focus", "corpus_profile", "key_questions", "past_queries", "memo"},
    ),
    (
        SECTION_PROMPT,
        {"company", "name", "focus", "key_questions", "conflicts", "memo", "evidence"},
    ),
    (EXEC_SUMMARY_PROMPT, {"company", "body"}),
    (SYNTHESIS_PROMPT, {"company", "body"}),
    (RISK_PROMPT, {"company", "body"}),
    (CONSISTENCY_PROMPT, {"company", "memos"}),
    (BOUNDARY_PROMPT, {"company", "body"}),
    (REVIEW_PROMPT, {"company", "memos", "questions"}),
    (CONDENSE_PROMPT, {"company", "history", "question"}),
    (CHAT_ANSWER_PROMPT, {"company", "history", "question", "evidence"}),
]


def test_prompt_input_variables() -> None:
    for prompt, variables in _EXPECTED:
        assert set(prompt.input_variables) == variables


def test_reflection_and_verdict_defaults() -> None:
    refl = Reflection(done=True, core_chunk_ids=["c:0"])
    assert refl.gaps == [] and refl.queries == []
    verdict = ReviewVerdict(need_more=False)
    assert verdict.followups == []
