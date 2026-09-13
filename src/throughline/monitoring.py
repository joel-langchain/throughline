"""Score every production run and attach the scores to its trace.

Nobody watches the Monday run. Whatever it produces has already been delivered by
the time anyone looks, so the run has to say how it went — not just that it
finished. A run that finishes is not the same as a run that worked: it can lose
half its searches, skip every topic, publish an uncited report, or quietly stop
archiving sources, and still exit "successfully".

So each finished run scores itself and writes the scores to its own LangSmith
trace as feedback. Two families:

* **Report quality** — the reference-free scorers the golden set already uses
  offline (`throughline.scoring`). The same functions grade the frozen golden set
  in CI and the live report here, so a CI regression and a production regression
  are directly comparable.
* **Run health** — what the report alone cannot show: whether a report was
  produced at all, how many topics survived, whether searches failed, whether
  sources were archived, what the verifier flagged, and whether delivery landed.

Everything is derived from the finished run's state, so there is no extra model
call beyond the optional voice judge. Scoring is best-effort and never raises:
a monitoring failure must not cost the report.

Reading the scores: in LangSmith, the tracing project's runs carry these as
feedback keys, chartable over time and alertable with a threshold. The ones worth
alerting on are `report_produced`, `source_archive_ok`, `citation_integrity`,
and `delivery_ok`; the counts (`search_failures`, `subagent_failures`,
`verification_flags`) are worth watching for anything above zero.
"""

from __future__ import annotations

import json
import logging
import re

from throughline.quarantine import FAILURES_DIR, RESEARCH_ROOT, SOURCES_DIR
from throughline.scoring import (
    score_citation_integrity,
    score_dedup,
    score_groundedness,
    score_source_quality,
    score_voice,
)

logger = logging.getLogger("throughline.monitoring")

COVERAGE_PATH = "/memories/coverage.md"
# "Produced" means a report exists and has the shape of one: a title and at least
# one topic section. It deliberately does NOT mean "substantial" — a genuinely
# quiet week is allowed to be short, and thinness is what topic_count and
# source_count are for. The length floor only rules out a stub, e.g. a heading
# above an apology, which would otherwise score well on every quality dimension
# precisely because there is nothing there to be wrong.
_MIN_REPORT_CHARS = 200
_TITLE = re.compile(r"^#\s+\S", re.MULTILINE)
_TOPIC_HEADING = re.compile(r"^##\s+(?!Sources\b)(.+)$", re.MULTILINE)
_URL = re.compile(r"https?://[^\s)\]]+")


class Signal(dict):
    """One feedback entry: {"key", "score", "comment"}."""

    def __init__(self, key: str, score: float, comment: str):
        super().__init__(key=key, score=float(score), comment=comment)


# --- reading the finished run ----------------------------------------------


def _file_text(files: dict | None, path: str) -> str:
    entry = (files or {}).get(path)
    if not entry:
        return ""
    body = entry.get("content", "") if isinstance(entry, dict) else entry
    return "\n".join(body) if isinstance(body, list) else (body or "")


def _count_archive(files: dict | None, directory: str) -> int:
    """How many archived search files sit under any topic's `directory`."""
    marker = f"/{directory}/"
    return sum(
        1
        for path in (files or {})
        if path.startswith(f"{RESEARCH_ROOT}/") and marker in path
    )


def _task_results(messages: list | None) -> tuple[list[dict], int]:
    """Parsed subagent replies, plus the number of delegations that failed.

    Subagents reply with typed JSON now (see `schemas.py`), so a verdict can be
    read rather than inferred from prose. Anything that does not parse is counted
    as a failed delegation rather than silently ignored.
    """
    parsed: list[dict] = []
    failures = 0
    for message in messages or []:
        name = message.get("name") if isinstance(message, dict) else getattr(message, "name", None)
        kind = message.get("type") if isinstance(message, dict) else getattr(message, "type", None)
        if kind != "tool" or name != "task":
            continue
        status = (
            message.get("status") if isinstance(message, dict) else getattr(message, "status", None)
        )
        content = (
            message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
        )
        if not isinstance(content, str):
            content = str(content)
        if status == "error":
            failures += 1
            continue
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            failures += 1
            continue
        if isinstance(obj, dict):
            parsed.append(obj)
        else:
            failures += 1
    return parsed, failures


# --- the signals ------------------------------------------------------------


def report_signals(report: str, coverage: str) -> list[Signal]:
    """Quality of the finished report, via the golden set's own evaluators."""
    if not report.strip():
        return []
    checks = (
        ("source_quality", score_source_quality(report)),
        ("groundedness", score_groundedness(report)),
        ("citation_integrity", score_citation_integrity(report)),
        ("voice", score_voice(report)),
        ("dedup", score_dedup(report, coverage)),
    )
    return [Signal(key, score, comment) for key, (score, comment) in checks]


def health_signals(files: dict | None, messages: list | None, delivered: bool | None) -> list[Signal]:
    """What the report cannot tell you: did the machinery actually work?"""
    report = _file_text(files, "/output/report.md")
    archived = _count_archive(files, SOURCES_DIR)
    failed_searches = _count_archive(files, FAILURES_DIR)
    results, subagent_failures = _task_results(messages)

    topics = len(_TOPIC_HEADING.findall(report))
    sources = len({u.rstrip(".,);]") for u in _URL.findall(report)})
    produced = (
        len(report.strip()) >= _MIN_REPORT_CHARS
        and bool(_TITLE.search(report))
        and topics >= 1
    )

    verdicts = [str(r.get("verdict", "")).upper() for r in results]
    kept = verdicts.count("KEEP")
    skipped = verdicts.count("SKIP")
    flags = verdicts.count("FLAG")
    revise = verdicts.count("REVISE")

    signals = [
        Signal(
            "report_produced",
            1.0 if produced else 0.0,
            f"{len(report)} chars, {topics} topic section(s)"
            if produced
            else f"no usable report ({len(report)} chars, {topics} topic section(s))",
        ),
        Signal("topic_count", topics, f"{topics} topic section(s) in the report"),
        Signal("source_count", sources, f"{sources} distinct source(s) cited"),
        # Zero archived sources WHILE research happened means the quarantine
        # stopped working and the verifier has been checking nothing. Zero with no
        # research at all is just a run that did none, so the comment has to
        # distinguish them: an alarming line on a healthy run sends someone
        # chasing a problem that isn't there.
        Signal(
            "source_archive_ok",
            0.0 if (results and archived == 0) else 1.0,
            f"{archived} search result(s) archived"
            if archived
            else (
                "NO sources archived — the verifier had no evidence to check"
                if results
                else "no research in this run, so nothing to archive"
            ),
        ),
        Signal(
            "search_failures",
            failed_searches,
            f"{failed_searches} search(es) failed and were not archived as evidence"
            if failed_searches
            else "no failed searches",
        ),
        Signal(
            "subagent_failures",
            subagent_failures,
            f"{subagent_failures} delegation(s) failed or returned unreadable output"
            if subagent_failures
            else "every delegation returned a readable result",
        ),
        Signal(
            "topics_kept",
            kept,
            f"{kept} kept, {skipped} skipped by the quality gate",
        ),
        Signal(
            "verification_flags",
            flags,
            f"{flags} topic(s) flagged for unsupported citations"
            if flags
            else "no citation flags",
        ),
        Signal(
            "final_pass_revisions",
            revise,
            f"final-pass reviewer asked for revision {revise} time(s)"
            if revise
            else "final pass approved without revision",
        ),
    ]
    if delivered is not None:
        signals.append(
            Signal(
                "delivery_ok",
                1.0 if delivered else 0.0,
                "report delivered" if delivered else "report NOT delivered (see [delivery] log)",
            )
        )
    return signals


def run_signals(
    state: dict | None, report: str, *, delivered: bool | None = None
) -> list[Signal]:
    """Every signal for one finished run."""
    state = state or {}
    files = state.get("files")
    coverage = _file_text(files, COVERAGE_PATH)
    return [
        *health_signals(files, state.get("messages"), delivered),
        *report_signals(report, coverage),
    ]


# --- attaching them to the trace -------------------------------------------


def _current_run_id() -> str | None:
    """The trace's root run id, or None when the run is not being traced."""
    try:
        from langsmith.run_helpers import get_current_run_tree

        tree = get_current_run_tree()
    except Exception:  # noqa: BLE001 - tracing is optional, never fatal
        return None
    if tree is None:
        return None
    return str(getattr(tree, "trace_id", None) or getattr(tree, "id", "")) or None


def record(state: dict | None, report: str, *, delivered: bool | None = None) -> list[Signal]:
    """Score the run and attach the scores to its trace. Never raises.

    Returns the signals so a caller (or a test) can see them even when there is
    no trace to write to, and logs a one-line summary either way so a local run
    and a deployed run are both readable from their logs.
    """
    try:
        signals = run_signals(state, report, delivered=delivered)
    except Exception as exc:  # noqa: BLE001 - monitoring must not cost the report
        logger.warning("scoring failed: %s", exc)
        print(f"[monitoring] scoring failed: {type(exc).__name__}: {exc}", flush=True)
        return []

    summary = ", ".join(f"{s['key']}={s['score']:g}" for s in signals)
    print(f"[monitoring] {summary}", flush=True)

    run_id = _current_run_id()
    if run_id is None:
        return signals
    try:
        from langsmith import Client

        client = Client()
        for signal in signals:
            client.create_feedback(
                run_id=run_id,
                key=signal["key"],
                score=signal["score"],
                comment=signal["comment"],
            )
    except Exception as exc:  # noqa: BLE001 - a trace write must not fail the run
        logger.warning("could not attach feedback: %s", exc)
        print(f"[monitoring] could not attach feedback: {type(exc).__name__}", flush=True)
    return signals
