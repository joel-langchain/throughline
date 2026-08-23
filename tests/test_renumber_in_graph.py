"""Tests for the in-graph citation renumbering (the headless output path).

The deployed agent has no local runner to post-process its output, so citation
renumbering runs INSIDE the graph via an ``after_agent`` middleware. These tests
cover the pure file-update helper, the middleware hook that wraps it, and that
the middleware is actually wired into the compiled agent graph — all without a
model call.
"""

from __future__ import annotations

from datetime import date

from throughline.agent import (
    REPORT_PATH,
    append_todays_date,
    build_agent,
    renumber_citations_middleware,
    renumber_report_in_files,
)

# A report written to the editor's [[url]] citation contract, not yet numbered.
UNNUMBERED = """# Throughline — This Week in AI

## Topic A (new)
A first claim [[https://www.reuters.com/a]] and a second [[https://www.nytimes.com/b]].

## Sources
1. Reuters — https://www.reuters.com/a
2. New York Times — https://www.nytimes.com/b
"""


def test_helper_renumbers_and_returns_only_the_report() -> None:
    update = renumber_report_in_files({REPORT_PATH: {"content": UNNUMBERED}})
    assert update is not None, "expected a files update for an unnumbered report"
    assert set(update["files"]) == {REPORT_PATH}, "only the report file should change"
    body = update["files"][REPORT_PATH]["content"]
    assert update["files"][REPORT_PATH]["encoding"] == "utf-8"
    # [[url]] markers are gone, replaced by contiguous global [n].
    assert "[[" not in body
    assert "[1]" in body and "[2]" in body
    assert "1. Reuters" in body and "2. New York Times" in body


def test_helper_is_a_noop_when_there_is_nothing_to_do() -> None:
    assert renumber_report_in_files(None) is None
    assert renumber_report_in_files({}) is None
    # An already-numbered report has no [[url]] markers, so nothing changes.
    already = renumber_report_in_files(
        {REPORT_PATH: {"content": "Body [1].\n\n## Sources\n1. Reuters — https://www.reuters.com/a\n"}}
    )
    assert already is None


def test_helper_accepts_legacy_list_content() -> None:
    update = renumber_report_in_files({REPORT_PATH: {"content": UNNUMBERED.splitlines()}})
    assert update is not None
    assert "[[" not in update["files"][REPORT_PATH]["content"]


def test_middleware_hook_delegates_to_the_helper() -> None:
    update = renumber_citations_middleware.after_agent(
        {"files": {REPORT_PATH: {"content": UNNUMBERED}}}, None
    )
    assert update is not None
    assert "[[" not in update["files"][REPORT_PATH]["content"]


def test_middleware_is_wired_into_the_compiled_graph() -> None:
    nodes = build_agent().get_graph().nodes
    assert any("renumber" in name.lower() for name in nodes), (
        f"renumber middleware node missing from graph; nodes={list(nodes)}"
    )


def test_todays_date_is_appended_to_the_system_prompt() -> None:
    # A plain string prompt is preserved and dated.
    out = append_todays_date("EDITOR PROMPT", today=date(2026, 8, 23))
    assert out.startswith("EDITOR PROMPT")
    assert "today's date is 2026-08-23" in out


def test_todays_date_handles_system_message_content_blocks() -> None:
    class _Msg:
        content = [{"type": "text", "text": "BASE"}]

    out = append_todays_date(_Msg(), today=date(2026, 1, 5))
    assert "BASE" in out
    assert "2026-01-05" in out
