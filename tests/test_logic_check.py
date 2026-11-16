"""The logic check and its place beside the final-pass reviewer, offline.

The judge is a fake that returns fixed working, so these tests pin what code
does with the working: which items become findings, how a finding's quote is
held to the report, how findings join the reviewer's result, and that the check
runs alongside the reviewer instead of after it.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from throughline import logic_check
from throughline.agent import REPORT_PATH, logic_check_middleware, merge_logic_findings
from throughline.logic_check import (
    DerivedFigure,
    DerivedFigures,
    HeadingCheck,
    Headings,
    LogicFinding,
    RepeatedSubject,
    RepeatedSubjects,
    Summaries,
    SummaryCheck,
)
from throughline.schemas import FinalPassResult

REPORT = """# Throughline — This Week in AI · 2026-08-08

Since last week, open-weight models fell further behind closed ones.

## Open-Weight Models Win (new)
A lab released a model that matches leading closed systems [[https://example.com/a]]. \
Rep. Smith said it sold 4 licences at 10 dollars each, a total of 50 dollars \
[[https://example.com/b]]. Analysts expect more labs to follow.

## Sources
1. Example — A — 2026-08-06 — https://example.com/a
2. Example — B — 2026-08-06 — https://example.com/b
"""

SUMMARY = "Since last week, open-weight models fell further behind closed ones."
CITED = "A lab released a model that matches leading closed systems [1]."
DERIVED = (
    "Rep. Smith said it sold 4 licences at 10 dollars each, a total of 50 dollars [2]."
)
HEADING = "Open-Weight Models Win"


def _working(call: str) -> object:
    """The fake judge's working for one call: one failing item each."""
    if call == "derived_figure":
        return DerivedFigures(
            items=[
                DerivedFigure(
                    sentence=DERIVED,
                    stated="50 dollars",
                    inputs="4 licences, 10 dollars each",
                    working="4 x 10 = 40",
                    verdict="does not follow",
                ),
                DerivedFigure(
                    sentence="A sentence that is not in the report at all, 9 x 9 = 80.",
                    stated="80",
                    inputs="9, 9",
                    working="81",
                    verdict="does not follow",
                ),
                DerivedFigure(
                    sentence=CITED,
                    stated="matches",
                    inputs="none stated",
                    working="no arithmetic",
                    verdict="inputs not stated",
                ),
            ]
        )
    if call == "summary":
        return Summaries(
            items=[
                SummaryCheck(
                    number=1,
                    rates="open-weight models",
                    cited=CITED,
                    why="behind vs matches",
                    conflict=True,
                ),
                SummaryCheck(
                    number=2, rates="", cited="", why="no rating", conflict=False
                ),
                SummaryCheck(
                    number=9,
                    rates="x",
                    cited=CITED,
                    why="no such sentence",
                    conflict=True,
                ),
            ]
        )
    if call == "repeated":
        return RepeatedSubjects(
            items=[
                RepeatedSubject(
                    subject="the model",
                    first=CITED,
                    second=DERIVED,
                    why="fine",
                    conflict=False,
                )
            ]
        )
    return Headings(
        items=[
            HeadingCheck(
                heading=f"{HEADING} (new)",
                asserts="open-weight models win",
                body_shows="one model matches closed systems; nothing shows a win",
                against="Analysts expect more labs to follow.",
                supported=False,
            ),
            HeadingCheck(
                heading="A Heading The Report Does Not Have",
                asserts="x",
                body_shows="y",
                against=CITED,
                supported=False,
            ),
            HeadingCheck(
                heading=HEADING,
                asserts="open-weight models win",
                body_shows="no sentence quoted, so not a finding",
                against="",
                supported=False,
            ),
        ]
    )


class FakeJudge:
    def __init__(self, call: str, delay: float = 0.0, fail: bool = False):
        self.call, self.delay, self.fail = call, delay, fail

    def invoke(self, messages):
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("model unavailable")
        return _working(self.call)


@pytest.fixture
def fake_judge(monkeypatch):
    """Swap the model for FakeJudge and give each test its own pending checks."""
    settings = {"delay": 0.0, "fail": False}
    monkeypatch.setattr(
        logic_check, "_judge_factory", lambda call: FakeJudge(call, **settings)
    )
    monkeypatch.setattr(logic_check, "_pending", {})
    return settings


# --- what the judge reads ----------------------------------------------------


def test_judge_text_numbers_markers_and_drops_sources() -> None:
    text = logic_check.judge_text(REPORT)
    assert "## Sources" not in text
    assert "https://" not in text
    assert CITED in text
    assert "a total of 50 dollars [2]." in text


def test_uncited_sentences_are_found_in_code() -> None:
    assert logic_check.uncited_sentences(REPORT) == [
        SUMMARY,
        "Analysts expect more labs to follow.",
    ]


def test_summary_call_lists_the_uncited_sentences() -> None:
    content = logic_check.judge_input(REPORT, "summary")
    assert content.endswith(
        f"UNCITED SENTENCES\n1. {SUMMARY}\n2. Analysts expect more labs to follow."
    )
    assert logic_check.judge_input(REPORT, "heading") == logic_check.judge_text(REPORT)


# --- from working to findings ------------------------------------------------


def test_only_failing_items_with_real_quotes_become_findings() -> None:
    found = [
        f
        for call in logic_check.CALLS
        for f in logic_check.findings_from(_working(call), REPORT)
    ]
    assert [f.kind for f in found] == ["derived_figure", "contradiction", "heading"]
    derived, contradiction, heading = found
    # The quote is shown without its marker, and the made-up sentence is dropped.
    assert derived.quote == DERIVED.replace(" [2].", ".")
    assert derived.detail == "4 x 10 = 40"
    # The summary finding quotes the sentence code found, by its number.
    assert contradiction.quote == f'"{SUMMARY}" and "{CITED.replace(" [1].", ".")}"'
    assert heading.quote == "Open-Weight Models Win (new)"
    assert heading.detail.endswith(
        'The section says "Analysts expect more labs to follow."'
    )


def test_a_heading_finding_must_quote_its_own_section() -> None:
    def heading(against: str, asserts: str = "they win") -> Headings:
        return Headings(
            items=[
                HeadingCheck(
                    heading=HEADING,
                    asserts=asserts,
                    body_shows="they do not",
                    against=against,
                    supported=False,
                )
            ]
        )

    assert logic_check.findings_from(heading(CITED), REPORT)
    # The opening sentence is in the report but not in this section.
    assert not logic_check.findings_from(heading(SUMMARY), REPORT)
    assert not logic_check.findings_from(heading(""), REPORT)
    # A heading that only names a topic cannot claim too much.
    assert not logic_check.findings_from(heading(CITED, "Names a topic."), REPORT)


def test_a_quote_without_its_markers_or_with_curly_quotes_still_matches() -> None:
    working = DerivedFigures(
        items=[
            DerivedFigure(
                sentence="Rep. Smith said it sold 4 licences at 10 dollars each, a total of 50 dollars.",
                stated="50",
                inputs="4, 10",
                working="40",
                verdict="does not follow",
            )
        ]
    )
    assert len(logic_check.findings_from(working, REPORT)) == 1


def test_score_is_per_kind() -> None:
    found = [LogicFinding("heading", "X", "y")]
    assert logic_check.score(found, "heading")[0] == 0.0
    assert logic_check.score(found, "derived_figure") == (
        1.0,
        "no derived figure errors found",
    )


def test_check_runs_the_calls_for_the_asked_kinds(fake_judge) -> None:
    assert {f.kind for f in logic_check.check(REPORT, ("heading",))} == {"heading"}
    assert len(logic_check.check(REPORT)) == 3


# --- joining the reviewer's result -------------------------------------------


def _review_result(verdict: str = "APPROVE") -> Command:
    result = FinalPassResult(verdict=verdict, issues=[], note="Reads well.")
    return Command(
        update={"messages": [ToolMessage(result.model_dump_json(), tool_call_id="r1")]}
    )


def _parsed(command: Command) -> FinalPassResult:
    return FinalPassResult.model_validate_json(command.update["messages"][0].content)


def test_findings_turn_approve_into_revise_with_the_quotes() -> None:
    finding = LogicFinding("derived_figure", DERIVED, "4 x 10 = 40")
    merged = _parsed(merge_logic_findings(_review_result(), [finding]))
    assert merged.verdict == "REVISE"
    assert merged.issues == [finding.issue()]
    assert DERIVED in merged.issues[0] and "4 x 10 = 40" in merged.issues[0]
    assert "logic check found 1 problem" in merged.note


def test_no_findings_or_an_unreadable_result_is_left_alone() -> None:
    result = _review_result()
    assert merge_logic_findings(result, []) is result
    finding = [LogicFinding("heading", "X", "y")]
    error = ToolMessage("failed twice", tool_call_id="r1", status="error")
    assert merge_logic_findings(error, finding) is error
    prose = ToolMessage("not json", tool_call_id="r1")
    assert merge_logic_findings(prose, finding) is prose


# --- the middleware ----------------------------------------------------------


def _write_request() -> SimpleNamespace:
    args = {"file_path": REPORT_PATH, "content": REPORT}
    return SimpleNamespace(
        tool=None, state={}, tool_call={"name": "write_file", "args": args, "id": "w1"}
    )


def _review_request(files: dict | None = None) -> SimpleNamespace:
    args = {"subagent_type": "final-pass-reviewer", "description": "Review the report."}
    state = {
        "files": files if files is not None else {REPORT_PATH: {"content": REPORT}}
    }
    return SimpleNamespace(
        tool=None, state=state, tool_call={"name": "task", "args": args, "id": "r1"}
    )


def _written(_request):
    return ToolMessage("Updated file /output/report.md", tool_call_id="w1")


def test_the_write_starts_the_check_and_the_review_collects_it(fake_judge) -> None:
    logic_check_middleware.wrap_tool_call(_write_request(), _written)
    assert len(logic_check._pending) == 1
    result = logic_check_middleware.wrap_tool_call(
        _review_request(), lambda _r: _review_result()
    )
    assert len(logic_check._pending) == 1, (
        "the review joined the check, not a second one"
    )
    merged = _parsed(result)
    assert merged.verdict == "REVISE"
    assert [i.split(".")[0] for i in merged.issues] == [
        "Logic check, derived figure"
    ] + [
        "Logic check, contradiction",
        "Logic check, heading",
    ]


def test_the_check_runs_alongside_the_reviewer_not_after_it(fake_judge) -> None:
    fake_judge["delay"] = 0.5

    def slow_reviewer(_request):
        time.sleep(0.5)
        return _review_result()

    began = time.monotonic()
    logic_check_middleware.wrap_tool_call(_write_request(), _written)
    result = logic_check_middleware.wrap_tool_call(_review_request(), slow_reviewer)
    elapsed = time.monotonic() - began
    assert _parsed(result).verdict == "REVISE"
    assert elapsed < 0.9, f"check and reviewer ran one after the other ({elapsed:.2f}s)"


def test_a_failed_check_leaves_the_review_as_it_was(fake_judge) -> None:
    fake_judge["fail"] = True
    approved = _review_result()
    result = logic_check_middleware.wrap_tool_call(
        _review_request(), lambda _r: approved
    )
    assert result is approved


def test_a_slow_check_is_skipped_after_the_wait(fake_judge) -> None:
    fake_judge["delay"] = 1.0
    pending = logic_check.start(REPORT)
    began = time.monotonic()
    assert logic_check.collect(pending, timeout=0.1) == []
    assert time.monotonic() - began < 0.5


def test_other_tools_and_subagents_pass_through(fake_judge) -> None:
    other = SimpleNamespace(
        tool=None,
        state={},
        tool_call={
            "name": "task",
            "args": {"subagent_type": "topic-researcher"},
            "id": "t1",
        },
    )
    sentinel = ToolMessage("researched", tool_call_id="t1")
    assert logic_check_middleware.wrap_tool_call(other, lambda _r: sentinel) is sentinel
    assert logic_check._pending == {}


def test_the_async_path_merges_too(fake_judge) -> None:
    async def written(_request):
        return _written(_request)

    async def reviewer(_request):
        return _review_result()

    async def run():
        await logic_check_middleware.awrap_tool_call(_write_request(), written)
        return await logic_check_middleware.awrap_tool_call(_review_request(), reviewer)

    assert _parsed(asyncio.run(run())).verdict == "REVISE"


def test_the_confirming_re_review_is_not_checked_again(fake_judge) -> None:
    from langchain_core.messages import AIMessage

    def review_call(call_id: str) -> AIMessage:
        args = {"subagent_type": "final-pass-reviewer", "description": "Review."}
        return AIMessage("", tool_calls=[{"name": "task", "args": args, "id": call_id}])

    request = _review_request()
    request.state["messages"] = [review_call("r0"), review_call("r1")]
    approved = _review_result()
    assert (
        logic_check_middleware.wrap_tool_call(request, lambda _r: approved) is approved
    )
    assert logic_check._pending == {}
