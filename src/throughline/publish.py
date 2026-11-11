"""Publish each finished run to the public dashboard (optional, best-effort).

The dashboard is a static site on the repo's ``site`` branch, served by GitHub
Pages. It is read-only by construction: visitors read files the weekly run wrote,
and nothing on the page can start a run. This module is the only writer. After a
run it adds ``data/runs/<date>.json`` (the report, its topics, and the run's
scores) and updates ``data/index.json`` (one summary line per week).

A run whose alert fired is not published. It is recorded as held, with the reason
and no report, so a broken or doubtful week never goes public unreviewed and a
lost week still shows up as lost rather than silently missing.

Cost, cost by stage, and duration are not known until a run has ended, so each
run fills them in for earlier weeks from their LangSmith traces. The latest week
shows them a week late. Cost here is model calls only; search is billed by its
own plan and is not in the trace.

Configured by ``SITE_GITHUB_TOKEN`` (a token that can write this repo's contents)
and ``SITE_REPO`` (``owner/name``), with ``SITE_BRANCH`` defaulting to ``site``.
Neither set → no-op. Uses only the standard library for GitHub, never raises, and
logs a secret-free ``[publish] ...`` line.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime

logger = logging.getLogger("throughline.publish")

_API = "https://api.github.com"
INDEX_PATH = "data/index.json"
_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_TOPIC = re.compile(r"^##\s+(.+?)\s*(?:\((new|developing)\))?\s*$", re.MULTILINE | re.IGNORECASE)


def _log(message: str) -> None:
    logger.info(message)
    print(f"[publish] {message}", flush=True)


# --- what gets published ----------------------------------------------------


def topics(report: str) -> list[dict]:
    """The report's topic headings with their new/developing tag, Sources excluded."""
    found = []
    for name, status in _TOPIC.findall(report):
        if name.strip().lower().startswith("sources"):
            continue
        found.append({"name": name.strip(), "status": status.lower() or None})
    return found


def run_record(
    report: str,
    signals: list[dict],
    *,
    day: str,
    trace_id: str | None,
    held_reason: str | None = None,
) -> dict:
    """One week's entry. Pure, so the shape is testable without GitHub.

    A held week carries the reason instead of the report, so nothing doubtful is
    published, but the scores still show what happened.
    """
    title = _TITLE.search(report)
    return {
        "date": day,
        "title": title.group(1).strip() if title else None,
        "published": held_reason is None,
        "held_reason": held_reason,
        "topics": topics(report) if held_reason is None else [],
        "scores": {s["key"]: s["score"] for s in signals},
        "trace_id": trace_id,
        "cost_usd": None,
        "cost_by_stage": None,
        "duration_minutes": None,
        "report": report if held_reason is None else None,
    }


def summary(record: dict) -> dict:
    """The index line for a week: everything except the report text."""
    return {k: v for k, v in record.items() if k != "report"}


def merge_index(index: dict | None, record: dict) -> dict:
    """Add or replace this week's line, newest first."""
    runs = [r for r in (index or {}).get("runs", []) if r.get("date") != record["date"]]
    runs.append(summary(record))
    runs.sort(key=lambda r: r.get("date") or "", reverse=True)
    return {"updated": datetime.now(UTC).isoformat(timespec="seconds"), "runs": runs}


def stage_costs(task_runs: list[dict], llm_runs: list[dict]) -> dict[str, float]:
    """Model cost per stage of one run, from its trace.

    Each subagent runs inside an editor ``task`` call, so a model call belongs to
    the subagent whose task call is among its ancestors; anything else is the
    editor's own work (scanning, clustering, writing). Keys are the subagent
    names, plus ``editor``.
    """
    stage_of = {
        str(t["id"]): (t.get("inputs") or {}).get("subagent_type") or "other" for t in task_runs
    }
    totals: dict[str, float] = {}
    for call in llm_runs:
        ancestors = [str(p) for p in (call.get("parent_run_ids") or [])]
        stage = next((stage_of[p] for p in ancestors if p in stage_of), "editor")
        totals[stage] = totals.get(stage, 0.0) + float(call.get("total_cost") or 0)
    return {stage: round(cost, 2) for stage, cost in totals.items()}


def fill_costs(index: dict, read_run, read_stages=None) -> int:
    """Fill cost, cost by stage, and duration for earlier weeks from their traces.

    ``read_run(trace_id)`` returns an object with ``total_cost``, ``start_time``,
    and ``end_time``; ``read_stages(trace_id)`` returns ``stage_costs`` for it.
    Best-effort per week; returns how many weeks were filled.
    """
    filled = 0
    for entry in index.get("runs", []):
        if entry.get("cost_usd") is not None or not entry.get("trace_id"):
            continue
        try:
            run = read_run(entry["trace_id"])
        except Exception:  # noqa: BLE001 - one unreadable trace must not stop the rest
            continue
        if run is None or getattr(run, "end_time", None) is None:
            continue  # still running: this is the current week
        cost = getattr(run, "total_cost", None)
        entry["cost_usd"] = round(float(cost), 2) if cost is not None else None
        entry["duration_minutes"] = round(
            (run.end_time - run.start_time).total_seconds() / 60, 1
        )
        if read_stages is not None:
            try:
                stages = read_stages(entry["trace_id"])
            except Exception:  # noqa: BLE001 - the split is a nice-to-have
                stages = None
            # Some traces carry cost only on the run total, not on each model
            # call. A split that doesn't add up to the total would mislead, so
            # it is left out.
            if stages and entry["cost_usd"] and sum(stages.values()) >= 0.9 * entry["cost_usd"]:
                entry["cost_by_stage"] = stages
        filled += 1
    return filled


# --- GitHub contents API ------------------------------------------------------


def _request(method: str, url: str, token: str, body: dict | None = None) -> dict | None:
    """One GitHub API call. Returns the parsed body, or None on a 404."""
    request = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _read(repo: str, branch: str, path: str, token: str) -> tuple[dict | None, str | None]:
    """A JSON file on the branch, and its blob sha (needed to update it)."""
    found = _request("GET", f"{_API}/repos/{repo}/contents/{path}?ref={branch}", token)
    if not found:
        return None, None
    return json.loads(base64.b64decode(found["content"]).decode("utf-8")), found["sha"]


def _write(repo: str, branch: str, path: str, token: str, data: dict, sha: str | None, message: str) -> None:
    body = {
        "message": message,
        "branch": branch,
        "content": base64.b64encode(
            json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        ).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    _request("PUT", f"{_API}/repos/{repo}/contents/{path}", token, body)


def _langsmith_reader():
    """``(read_run, read_stages)`` against LangSmith, or None when not configured."""
    try:
        from langsmith import Client

        client = Client()
    except Exception:  # noqa: BLE001 - cost backfill is optional
        return None

    def read_stages(trace_id: str) -> dict[str, float]:
        tasks = client.list_runs(
            trace_id=trace_id, run_type="tool", filter='eq(name, "task")',
            select=["id", "inputs"],
        )
        calls = client.list_runs(
            trace_id=trace_id, run_type="llm", select=["parent_run_ids", "total_cost"],
        )
        return stage_costs(
            [{"id": t.id, "inputs": t.inputs} for t in tasks],
            [{"parent_run_ids": c.parent_run_ids, "total_cost": c.total_cost} for c in calls],
        )

    return client.read_run, read_stages


def publish_run(
    report: str,
    signals: list[dict],
    *,
    trace_id: str | None,
    held_reason: str | None = None,
    today: str | None = None,
) -> str:
    """Publish this run to the dashboard. Returns what happened; never raises.

    "published", "held" (the alert fired, so only the reason is recorded),
    "skipped" (not configured), or "failed" (see the [publish] log).
    """
    token = os.environ.get("SITE_GITHUB_TOKEN", "").strip()
    repo = os.environ.get("SITE_REPO", "").strip()
    branch = os.environ.get("SITE_BRANCH", "site").strip() or "site"
    if not token or not repo:
        _log("skipped: no dashboard configured (set SITE_GITHUB_TOKEN and SITE_REPO)")
        return "skipped"

    day = today or datetime.now(UTC).date().isoformat()
    record = run_record(report, signals, day=day, trace_id=trace_id, held_reason=held_reason)
    outcome = "held" if held_reason else "published"
    try:
        _, run_sha = _read(repo, branch, f"data/runs/{day}.json", token)
        _write(repo, branch, f"data/runs/{day}.json", token, record, run_sha,
               f"data: {outcome} run {day}")
        index, index_sha = _read(repo, branch, INDEX_PATH, token)
        index = merge_index(index, record)
        readers = _langsmith_reader()
        if readers is not None:
            fill_costs(index, *readers)
        _write(repo, branch, INDEX_PATH, token, index, index_sha, f"data: index for {day}")
    except Exception as exc:  # noqa: BLE001 - publishing must not cost the run
        _log(f"failed: {type(exc).__name__} writing to {repo}@{branch}")
        return "failed"
    _log(f"{outcome}: {day} to {repo}@{branch}")
    return outcome
