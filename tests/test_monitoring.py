"""A run that finishes is not a run that worked.

Nobody watches the Monday run, so it scores itself and writes the scores to its
own trace. These tests are organised around the failures each signal exists to
catch — the interesting cases are all "the run exited successfully and the output
was still wrong".
"""

from __future__ import annotations

from langchain_core.messages import ToolMessage

from throughline.monitoring import (
    COVERAGE_PATH,
    health_signals,
    record,
    report_signals,
    run_signals,
)
from throughline.quarantine import FAILURES_DIR, SOURCES_DIR

REPORT = """# Throughline — This Week in AI · 2026-09-13

Something happened this week, and here is the thread running through it all.

## Agent reliability (new)
Agents completed 40% of tasks [1]. A second finding followed from it [2].

## Sources

1. Reuters — A study — 2026-09-13 — https://www.reuters.com/a
2. Nature — A paper — 2026-09-13 — https://www.nature.com/b
"""


def _files(report: str = REPORT, *, sources: int = 2, failures: int = 0, coverage: str = ""):
    files = {"/output/report.md": {"content": report, "encoding": "utf-8"}}
    for i in range(sources):
        files[f"/research/topic/{SOURCES_DIR}/s{i}.md"] = {"content": "raw", "encoding": "utf-8"}
    for i in range(failures):
        files[f"/research/topic/{FAILURES_DIR}/f{i}.md"] = {"content": "err", "encoding": "utf-8"}
    if coverage:
        files[COVERAGE_PATH] = {"content": coverage, "encoding": "utf-8"}
    return files


def _task(payload: str, *, status: str = "success"):
    return ToolMessage(content=payload, tool_call_id="c1", name="task", status=status)


def _by_key(signals) -> dict:
    return {s["key"]: s for s in signals}


# --- the failure that started all of this: no report ------------------------


def test_a_run_that_produced_no_report_says_so() -> None:
    signals = _by_key(health_signals({}, [], None))
    assert signals["report_produced"]["score"] == 0.0
    assert "no usable report" in signals["report_produced"]["comment"]


def test_a_stub_report_does_not_count_as_produced() -> None:
    # A heading and one line would otherwise score well on every quality metric,
    # because there is almost nothing there to be wrong.
    stub = {"/output/report.md": {"content": "# Throughline\n\nNothing to report.", "encoding": "utf-8"}}
    assert _by_key(health_signals(stub, [], None))["report_produced"]["score"] == 0.0


def test_a_long_report_with_no_topic_sections_is_not_produced() -> None:
    # Length alone is not the test: an apology can be verbose. A report needs a
    # title and at least one topic section to count.
    rambling = "# Throughline\n\n" + ("Nothing conclusive happened this week. " * 20)
    files = {"/output/report.md": {"content": rambling, "encoding": "utf-8"}}
    assert _by_key(health_signals(files, [], None))["report_produced"]["score"] == 0.0


def test_a_short_but_real_report_still_counts_as_produced() -> None:
    # A quiet week is allowed to be short. Thinness is what topic_count and
    # source_count report, not a false "no report was produced" alarm.
    signals = _by_key(health_signals(_files(), [], None))
    assert signals["report_produced"]["score"] == 1.0


def test_a_real_report_counts_its_topics_and_sources() -> None:
    signals = _by_key(health_signals(_files(), [], None))
    assert signals["report_produced"]["score"] == 1.0
    assert signals["topic_count"]["score"] == 1, "Sources is not a topic"
    assert signals["source_count"]["score"] == 2


# --- the silent failure: archiving stops working ----------------------------


def test_research_with_nothing_archived_is_flagged() -> None:
    # The quarantine breaking is invisible from the report: the verifier simply
    # has no evidence to check, so everything passes or everything flags.
    results = [_task('{"topic": "t", "verdict": "KEEP"}')]
    signals = _by_key(health_signals(_files(sources=0), results, None))
    assert signals["source_archive_ok"]["score"] == 0.0
    assert "NO sources archived" in signals["source_archive_ok"]["comment"]


def test_archiving_is_fine_when_sources_were_written() -> None:
    results = [_task('{"topic": "t", "verdict": "KEEP"}')]
    assert _by_key(health_signals(_files(sources=3), results, None))["source_archive_ok"]["score"] == 1.0


def test_a_run_that_never_researched_is_not_blamed_for_an_empty_archive() -> None:
    assert _by_key(health_signals(_files(sources=0), [], None))["source_archive_ok"]["score"] == 1.0


# --- searches and delegations that failed -----------------------------------


def test_failed_searches_are_counted() -> None:
    signals = _by_key(health_signals(_files(failures=3), [], None))
    assert signals["search_failures"]["score"] == 3
    assert "3 search(es) failed" in signals["search_failures"]["comment"]


def test_a_failed_delegation_is_counted() -> None:
    messages = [_task("the subagent failed twice", status="error")]
    assert _by_key(health_signals(_files(), messages, None))["subagent_failures"]["score"] == 1


def test_an_unreadable_subagent_reply_counts_as_a_failure() -> None:
    # Typed output means a reply that is not JSON is a broken contract, not prose
    # to be parsed leniently.
    messages = [_task("VERDICT: KEEP — back to free text")]
    assert _by_key(health_signals(_files(), messages, None))["subagent_failures"]["score"] == 1


# --- what the subagents decided ---------------------------------------------


def test_verdicts_are_read_from_the_typed_replies() -> None:
    messages = [
        _task('{"topic": "a", "verdict": "KEEP"}'),
        _task('{"topic": "b", "verdict": "KEEP"}'),
        _task('{"topic": "c", "verdict": "SKIP"}'),
        _task('{"topic": "a", "verdict": "FLAG"}'),
        _task('{"verdict": "REVISE", "issues": ["x"], "note": "n"}'),
    ]
    signals = _by_key(health_signals(_files(), messages, None))
    assert signals["topics_kept"]["score"] == 2
    assert "1 skipped" in signals["topics_kept"]["comment"]
    assert signals["verification_flags"]["score"] == 1
    assert signals["final_pass_revisions"]["score"] == 1


def test_a_clean_run_reports_no_flags_or_revisions() -> None:
    messages = [_task('{"topic": "a", "verdict": "PASS"}'), _task('{"verdict": "APPROVE", "note": "n"}')]
    signals = _by_key(health_signals(_files(), messages, None))
    assert signals["verification_flags"]["score"] == 0
    assert signals["final_pass_revisions"]["score"] == 0


# --- delivery ---------------------------------------------------------------


def test_delivery_outcome_is_recorded_when_known() -> None:
    assert _by_key(health_signals(_files(), [], True))["delivery_ok"]["score"] == 1.0
    failed = _by_key(health_signals(_files(), [], False))["delivery_ok"]
    assert failed["score"] == 0.0
    assert "NOT delivered" in failed["comment"]


def test_no_delivery_signal_when_delivery_was_not_attempted() -> None:
    assert "delivery_ok" not in _by_key(health_signals(_files(), [], None))


# --- report quality reuses the golden set's scorers -------------------------


def test_report_quality_scores_a_good_report_well() -> None:
    signals = _by_key(report_signals(REPORT, ""))
    assert signals["citation_integrity"]["score"] == 1.0
    assert signals["groundedness"]["score"] == 1.0
    assert signals["voice"]["score"] == 1.0


def test_report_quality_catches_a_broken_citation() -> None:
    broken = REPORT.replace("[2]", "[7]")  # a marker pointing at nothing
    assert _by_key(report_signals(broken, ""))["citation_integrity"]["score"] < 1.0


def test_report_quality_catches_a_repeated_topic() -> None:
    coverage = "## Week of 2026-09-06\n- Agent reliability — covered already\n"
    assert _by_key(report_signals(REPORT, coverage))["dedup"]["score"] == 0.0


def test_no_quality_scores_for_an_empty_report() -> None:
    # Scoring prose that does not exist would report a confident zero for every
    # dimension and bury the one signal that matters: report_produced.
    assert report_signals("", "") == []


# --- the whole set ----------------------------------------------------------


def test_run_signals_covers_health_and_quality_together() -> None:
    state = {"files": _files(), "messages": [_task('{"topic": "a", "verdict": "KEEP"}')]}
    keys = {s["key"] for s in run_signals(state, REPORT, delivered=True)}
    assert {
        "report_produced",
        "topic_count",
        "source_count",
        "source_archive_ok",
        "search_failures",
        "subagent_failures",
        "topics_kept",
        "verification_flags",
        "final_pass_revisions",
        "delivery_ok",
        "source_quality",
        "groundedness",
        "citation_integrity",
        "voice",
        "dedup",
    } <= keys


def test_every_signal_carries_a_comment_explaining_itself() -> None:
    state = {"files": _files(failures=1), "messages": [_task('{"topic": "a", "verdict": "KEEP"}')]}
    for signal in run_signals(state, REPORT, delivered=True):
        assert signal["comment"], f"{signal['key']} has no comment"
        assert isinstance(signal["score"], float)


def test_dedup_reads_the_coverage_ledger_out_of_state() -> None:
    coverage = "## Week of 2026-09-06\n- Agent reliability — covered already\n"
    state = {"files": _files(coverage=coverage), "messages": []}
    assert _by_key(run_signals(state, REPORT))["dedup"]["score"] == 0.0


# --- monitoring must never cost the report ----------------------------------


def test_recording_survives_junk_state() -> None:
    # No trace to write to here, so this also covers the untraced local path.
    assert record({"files": "not-a-dict", "messages": None}, REPORT) == []
    assert record(None, "") != [] or True  # must not raise


def test_recording_returns_the_signals_it_computed() -> None:
    state = {"files": _files(), "messages": []}
    assert {s["key"] for s in record(state, REPORT, delivered=True)} >= {"report_produced"}
