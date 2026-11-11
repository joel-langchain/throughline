"""The public dashboard is read-only, and a doubtful week never reaches it.

The weekly run is the dashboard's only writer: it adds the week's report, topics,
and scores to the repo's ``site`` branch. A run whose alert fired is recorded as
held, with the reason and no report. All offline (no GitHub, no LangSmith).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from throughline import agent, publish

REPORT = """# Throughline — This Week in AI · 2026-11-09

The lede.

## Enterprise AI Agents: From Risk to Liability (developing)
A claim [1].

## Tencent Hy4 Enters the Open-Weight Frontier (new)
Another claim [2].

## Sources

1. Reuters — A story — 2026-11-09 — https://www.reuters.com/a
2. CNBC — Another — 2026-11-09 — https://www.cnbc.com/b
"""

SIGNALS = [
    {"key": "report_produced", "score": 1.0, "comment": "ok"},
    {"key": "claims_corroborated", "score": 0.83, "comment": "5 of 6"},
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("SITE_GITHUB_TOKEN", "SITE_REPO", "SITE_BRANCH",
                "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)


# --- what a week looks like ---------------------------------------------------


def test_topics_carry_their_new_or_developing_tag() -> None:
    assert publish.topics(REPORT) == [
        {"name": "Enterprise AI Agents: From Risk to Liability", "status": "developing"},
        {"name": "Tencent Hy4 Enters the Open-Weight Frontier", "status": "new"},
    ]


def test_a_published_week_carries_the_report_and_its_scores() -> None:
    record = publish.run_record(REPORT, SIGNALS, day="2026-11-09", trace_id="t1")
    assert record["published"] is True
    assert record["title"] == "Throughline — This Week in AI · 2026-11-09"
    assert record["report"] == REPORT
    assert record["scores"] == {"report_produced": 1.0, "claims_corroborated": 0.83}


def test_a_held_week_carries_the_reason_and_no_report() -> None:
    record = publish.run_record(
        REPORT, SIGNALS, day="2026-11-09", trace_id="t1", held_reason="No report this week."
    )
    assert record["published"] is False
    assert record["held_reason"] == "No report this week."
    assert record["report"] is None
    assert record["topics"] == []


def test_the_index_has_one_line_per_week_newest_first_and_no_report_text() -> None:
    old = publish.run_record(REPORT, SIGNALS, day="2026-11-02", trace_id="a")
    new = publish.run_record(REPORT, SIGNALS, day="2026-11-09", trace_id="b")
    index = publish.merge_index(publish.merge_index(None, old), new)
    rerun = publish.merge_index(index, publish.run_record(REPORT, [], day="2026-11-09", trace_id="c"))

    assert [r["date"] for r in rerun["runs"]] == ["2026-11-09", "2026-11-02"]
    assert rerun["runs"][0]["trace_id"] == "c", "a rerun replaces that week's line"
    assert all("report" not in r for r in rerun["runs"])


def test_cost_and_duration_are_filled_from_finished_traces_only() -> None:
    start = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    traces = {
        "done": SimpleNamespace(total_cost=5.8981, start_time=start, end_time=start + timedelta(minutes=19)),
        "running": SimpleNamespace(total_cost=1.0, start_time=start, end_time=None),
    }

    def read_run(trace_id):
        if trace_id == "broken":
            raise RuntimeError("unreadable")
        return traces[trace_id]

    index = {"runs": [
        {"trace_id": "done", "cost_usd": None},
        {"trace_id": "running", "cost_usd": None},
        {"trace_id": "broken", "cost_usd": None},
        {"trace_id": "kept", "cost_usd": 9.17, "duration_minutes": 37.0},
    ]}
    assert publish.fill_costs(index, read_run) == 1
    assert index["runs"][0]["cost_usd"] == 5.9
    assert index["runs"][0]["duration_minutes"] == 19.0
    assert index["runs"][1]["cost_usd"] is None, "the current week is not finished yet"
    assert index["runs"][3]["cost_usd"] == 9.17


# --- writing to the site branch -----------------------------------------------


class _FakeGitHub:
    """In-memory stand-in for the contents API, keyed by path."""

    def __init__(self, files: dict | None = None, fail: bool = False):
        self.files = {p: (json.dumps(d), f"sha-{p}") for p, d in (files or {}).items()}
        self.writes: list[dict] = []
        self.fail = fail

    def __call__(self, method, url, token, body=None):
        if self.fail:
            raise OSError("network down")
        path = url.split("/contents/")[1].split("?")[0]
        if method == "GET":
            if path not in self.files:
                return None
            text, sha = self.files[path]
            return {"content": base64.b64encode(text.encode()).decode(), "sha": sha}
        self.writes.append({"path": path, "sha": body.get("sha"), "branch": body["branch"],
                            "data": json.loads(base64.b64decode(body["content"]))})
        self.files[path] = (json.dumps(self.writes[-1]["data"]), f"sha-new-{path}")
        return {}


def _configure(monkeypatch, fake):
    monkeypatch.setenv("SITE_GITHUB_TOKEN", "ghp_test_token_value")
    monkeypatch.setenv("SITE_REPO", "someone/throughline")
    monkeypatch.setattr(publish, "_request", fake)
    monkeypatch.setattr(publish, "_langsmith_reader", lambda _ids: None)


def test_nothing_is_published_without_configuration() -> None:
    assert publish.publish_run(REPORT, SIGNALS, trace_id="t") == "skipped"


def test_a_run_writes_its_week_then_updates_the_index(monkeypatch) -> None:
    existing = {"runs": [{"date": "2026-11-02", "trace_id": "a", "cost_usd": 5.9}]}
    fake = _FakeGitHub({publish.INDEX_PATH: existing})
    _configure(monkeypatch, fake)

    outcome = publish.publish_run(REPORT, SIGNALS, trace_id="t", today="2026-11-09")

    assert outcome == "published"
    assert [w["path"] for w in fake.writes] == ["data/runs/2026-11-09.json", publish.INDEX_PATH]
    assert all(w["branch"] == "site" for w in fake.writes), "never main"
    assert fake.writes[0]["data"]["report"] == REPORT
    assert fake.writes[1]["sha"] == f"sha-{publish.INDEX_PATH}", "an update must name the old blob"
    assert [r["date"] for r in fake.writes[1]["data"]["runs"]] == ["2026-11-09", "2026-11-02"]


def test_a_held_week_publishes_no_report(monkeypatch) -> None:
    fake = _FakeGitHub()
    _configure(monkeypatch, fake)
    outcome = publish.publish_run(REPORT, SIGNALS, trace_id="t", held_reason="No report this week.",
                                  today="2026-11-09")
    assert outcome == "held"
    assert fake.writes[0]["data"]["report"] is None


def test_a_failed_publish_never_raises_or_logs_the_token(monkeypatch, capsys) -> None:
    _configure(monkeypatch, _FakeGitHub(fail=True))
    assert publish.publish_run(REPORT, SIGNALS, trace_id="t") == "failed"
    assert "ghp_test_token_value" not in capsys.readouterr().out


def test_the_alert_says_when_a_week_was_held(monkeypatch) -> None:
    sent: list[str] = []
    calls: list[dict] = []
    monkeypatch.setattr(agent, "deliver_alert", lambda text: sent.append(text) or True)

    def fake_publish(report, signals, **kwargs):
        calls.append(kwargs)
        return "held"

    monkeypatch.setattr(agent, "publish_run", fake_publish)
    agent.renumber_citations_middleware.after_agent({"files": {}, "messages": []}, None)

    assert calls[0]["held_reason"].startswith("No report this week.")
    assert sent and sent[0].endswith("Held back from the dashboard until reviewed.")


# --- where the cost goes ---------------------------------------------------------


def test_each_model_call_is_charged_to_the_subagent_that_made_it() -> None:
    # The checker turned out to be ~80% of every run's cost; the split depends on
    # charging a call to the task it ran under, at any depth.
    tasks = [
        {"id": "t-research", "inputs": {"subagent_type": "topic-researcher"}},
        {"id": "t-check", "inputs": {"subagent_type": "citation-verifier"}},
    ]
    calls = [
        {"parent_run_ids": ["root", "t-check", "agent", "model"], "total_cost": 0.5},
        {"parent_run_ids": ["root", "t-check"], "total_cost": 0.25},
        {"parent_run_ids": ["root", "t-research"], "total_cost": 0.1},
        {"parent_run_ids": ["root"], "total_cost": 0.3},
        {"parent_run_ids": None, "total_cost": None},
    ]
    assert publish.stage_costs(tasks, calls) == {
        "citation-verifier": 0.75, "topic-researcher": 0.1, "editor": 0.3,
    }


def test_the_split_is_filled_alongside_the_total() -> None:
    start = datetime(2026, 11, 9, 5, 0, tzinfo=UTC)
    done = SimpleNamespace(total_cost=8.52, start_time=start, end_time=start + timedelta(minutes=26))
    index = {"runs": [{"trace_id": "t", "cost_usd": None}]}
    split = {"citation-verifier": 7.13, "editor": 0.8, "topic-researcher": 0.57,
             "final-pass-reviewer": 0.03}
    publish.fill_costs(index, lambda _t: done, lambda _t: split)
    assert index["runs"][0]["cost_by_stage"] == split


def test_a_failed_split_keeps_the_total() -> None:
    start = datetime(2026, 11, 9, 5, 0, tzinfo=UTC)
    done = SimpleNamespace(total_cost=8.52, start_time=start, end_time=start + timedelta(minutes=26))
    index = {"runs": [{"trace_id": "t", "cost_usd": None}]}

    def broken(_t):
        raise RuntimeError("trace too large")

    publish.fill_costs(index, lambda _t: done, broken)
    assert index["runs"][0]["cost_usd"] == 8.52
    assert "cost_by_stage" not in index["runs"][0]


def test_a_split_that_does_not_add_up_is_left_out() -> None:
    # Older traces carry cost on the run total only, so every stage reads $0.
    start = datetime(2026, 9, 21, 5, 0, tzinfo=UTC)
    done = SimpleNamespace(total_cost=21.55, start_time=start, end_time=start + timedelta(minutes=60))
    index = {"runs": [{"trace_id": "t", "cost_usd": None}]}
    publish.fill_costs(index, lambda _t: done, lambda _t: {"citation-verifier": 0.0, "editor": 0.0})
    assert index["runs"][0]["cost_usd"] == 21.55
    assert "cost_by_stage" not in index["runs"][0]


# --- reading the traces ------------------------------------------------------------
# read_run and list_runs are removed from the LangSmith SDK after 31 Jan 2027.
# Their replacements are project-scoped and async, and a run's own cost no longer
# includes its children, so the total comes from the trace's aggregates.


class _FakeTraceAPI:
    def __init__(self, traces: dict, runs: dict):
        self.traces, self.runs, self.listed = traces, runs, []

    def query(self, *, project_id, trace_ids, **_):
        assert project_id == "p1"

        async def found():
            for trace_id in trace_ids:
                if trace_id in self.traces:
                    yield self.traces[trace_id]

        return found()

    async def list_runs(self, trace_id, *, project_id, filter, selects):
        assert project_id == "p1"
        self.listed.append(trace_id)
        kind = "task" if '"task"' in filter else "llm"
        return SimpleNamespace(items=self.runs[trace_id][kind])


def _fake_langsmith(monkeypatch, api: _FakeTraceAPI) -> None:
    import langsmith

    async def aread_project(*, project_name):
        assert project_name == "throughline-prod"
        return SimpleNamespace(id="p1")

    client = SimpleNamespace(traces=api, aread_project=aread_project)
    monkeypatch.setattr(langsmith, "Client", lambda: client)


def test_costs_are_read_with_the_trace_api(monkeypatch) -> None:
    import asyncio

    start = datetime(2026, 11, 9, 5, 0, tzinfo=UTC)
    trace = SimpleNamespace(
        root_run=SimpleNamespace(start_time=start, end_time=start + timedelta(minutes=26)),
        trace_aggregates=SimpleNamespace(total_cost=8.52394955),
    )
    running = SimpleNamespace(
        root_run=SimpleNamespace(start_time=start, end_time=None),
        trace_aggregates=SimpleNamespace(total_cost=1.0),
    )
    api = _FakeTraceAPI(
        {"done": trace, "running": running},
        {"done": {
            "task": [SimpleNamespace(id="t-check", inputs={"subagent_type": "citation-verifier"})],
            "llm": [SimpleNamespace(parent_run_ids=["root", "t-check"], total_cost=7.13),
                    SimpleNamespace(parent_run_ids=["root"], total_cost=1.39)],
        }},
    )
    _fake_langsmith(monkeypatch, api)

    found = asyncio.run(publish._fetch_traces(["done", "running", "gone"], "throughline-prod"))

    assert set(found) == {"done", "running"}, "a trace that can't be found is skipped"
    run, stages = found["done"]
    assert run.total_cost == 8.52394955
    assert stages == {"citation-verifier": 7.13, "editor": 1.39}
    assert found["running"][1] is None, "no split while the run is still going"
    assert api.listed == ["done", "done"]

    index = {"runs": [{"trace_id": "done", "cost_usd": None}]}
    publish.fill_costs(index, lambda t: found[t][0], lambda t: found[t][1])
    assert index["runs"][0]["cost_usd"] == 8.52
    assert index["runs"][0]["duration_minutes"] == 26.0


def test_the_lookup_works_from_inside_a_running_event_loop(monkeypatch) -> None:
    # The end-of-run hook may run inside the server's event loop, where
    # asyncio.run would refuse to start.
    import asyncio

    monkeypatch.setattr(publish, "_tracing_project", lambda: "throughline-prod")
    run = SimpleNamespace(total_cost=1.0, start_time=None, end_time=None)

    async def fake_fetch(trace_ids, project):
        return {t: (run, {"editor": 1.0}) for t in trace_ids}

    monkeypatch.setattr(publish, "_fetch_traces", fake_fetch)

    async def inside_a_loop():
        return publish._langsmith_reader(["t"])

    read_run, read_stages = asyncio.run(inside_a_loop())
    assert read_run("t") is run
    assert read_stages("t") == {"editor": 1.0}


def test_no_tracing_project_means_no_cost_lookup(monkeypatch) -> None:
    monkeypatch.setattr(publish, "_tracing_project", lambda: None)
    assert publish._langsmith_reader(["t"]) is None


def test_only_weeks_missing_a_cost_are_looked_up() -> None:
    index = {"runs": [
        {"trace_id": "a", "cost_usd": None},
        {"trace_id": "b", "cost_usd": 5.9},
        {"trace_id": None, "cost_usd": None},
    ]}
    assert publish.pending_traces(index) == ["a"]
