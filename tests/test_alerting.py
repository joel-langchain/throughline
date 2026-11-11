"""A run that produced nothing has to say so where someone will see it.

Three scheduled runs (28 Sep, 12 Oct, 19 Oct 2026) ended with no report. Each hit
Tavily's plan usage limit, the researchers retried as instructed (72, 63, and 39
failed searches), and the run finished "successfully". Each one scored itself
`report_produced = 0` on its trace, and nobody looked until the week had gone.

These tests pin the two halves of the fix, all offline (no Tavily, no Slack, no
model):

1. the first refusal of the plan or key stops search for the rest of the run;
2. a run whose key signals fail posts a short alert to the report's Slack channel.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from langchain_core.messages import ToolMessage
from tavily.errors import ForbiddenError, InvalidAPIKeyError, UsageLimitExceededError

from throughline import agent, delivery, tools
from throughline.monitoring import alert_text, health_signals, run_signals
from throughline.quarantine import FAILURES_DIR
from throughline.tools import internet_search, scan_ai_week

PLAN_LIMIT = (
    "This request exceeds your plan's set usage limit. Please upgrade your plan or "
    "contact support@tavily.com"
)


class _RefusingTavily:
    """Stand-in for TavilyClient that raises `error` on every call."""

    def __init__(self, error: Exception):
        self.error = error
        self.calls: list[str] = []

    def search(self, query: str, **_kwargs) -> dict:
        self.calls.append(query)
        raise self.error


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No real client, no outage left over, and no developer Slack config."""
    monkeypatch.setattr(tools, "_tavily", None)
    tools.reset_search_outage()
    for var in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "SLACK_WEBHOOK_URL",
                "SITE_GITHUB_TOKEN", "SITE_REPO"):
        monkeypatch.delenv(var, raising=False)
    yield
    tools.reset_search_outage()


# --- 1. a refused plan or key stops search for the run ----------------------


def test_a_plan_limit_stops_every_later_search(monkeypatch) -> None:
    fake = _RefusingTavily(ForbiddenError(PLAN_LIMIT))
    monkeypatch.setattr(tools, "_tavily", fake)

    first = internet_search.invoke({"query": "agent evals"})
    second = internet_search.invoke({"query": "agent evals, rephrased"})

    assert fake.calls == ["agent evals"], "the second search must not reach Tavily"
    for out in (first, second):
        assert out["results"] == []
        assert "unavailable for the rest of this run" in out["error"]
        assert "Do not retry" in out["error"]
    assert "usage limit" in tools.search_outage()


def test_an_invalid_key_stops_search_too(monkeypatch) -> None:
    monkeypatch.setattr(tools, "_tavily", _RefusingTavily(InvalidAPIKeyError("Unauthorized")))
    internet_search.invoke({"query": "anything"})
    assert tools.search_outage().startswith("InvalidAPIKeyError")


def test_a_rate_limit_is_not_treated_as_an_outage(monkeypatch) -> None:
    # 429 is transient; the transport already retries it, and the next search
    # may well work. Only refusals that cannot clear within a run stop search.
    monkeypatch.setattr(tools, "_tavily", _RefusingTavily(UsageLimitExceededError("slow down")))
    out = internet_search.invoke({"query": "anything"})
    assert "Retry" in out["error"]
    assert tools.search_outage() is None


def test_the_scan_stops_and_tells_the_editor_not_to_continue(monkeypatch) -> None:
    monkeypatch.setattr(tools, "_SCAN_SEEDS", ["seed one", "seed two", "seed three"])
    fake = _RefusingTavily(ForbiddenError(PLAN_LIMIT))
    monkeypatch.setattr(tools, "_tavily", fake)

    out = scan_ai_week.invoke({"extra_query": ""})

    assert fake.calls == ["seed one"], "later seeds would fail the same way"
    assert out.startswith("STOP")
    assert "do not delegate research" in out


def test_the_outage_text_never_carries_a_key(monkeypatch) -> None:
    leaky = ForbiddenError("refused for key tvly-dev-AbC123_xyz")
    monkeypatch.setattr(tools, "_tavily", _RefusingTavily(leaky))
    internet_search.invoke({"query": "anything"})
    assert "AbC123" not in tools.search_outage()


def test_each_run_starts_with_search_assumed_working(monkeypatch) -> None:
    monkeypatch.setattr(tools, "_tavily", _RefusingTavily(ForbiddenError(PLAN_LIMIT)))
    internet_search.invoke({"query": "anything"})
    assert tools.search_outage() is not None

    agent.reset_search_outage_middleware.before_agent({}, None)

    assert tools.search_outage() is None


# --- 2. the run says what happened ------------------------------------------


def _failed_search_files(count: int) -> dict:
    return {
        f"/research/topic/{FAILURES_DIR}/f{i}.md": {"content": "err", "encoding": "utf-8"}
        for i in range(count)
    }


def _skip_reply() -> ToolMessage:
    payload = '{"topic": "t", "verdict": "SKIP", "reason": "search unavailable"}'
    return ToolMessage(content=payload, tool_call_id="c1", name="task")


def test_the_outage_is_its_own_signal() -> None:
    down = {s["key"]: s for s in health_signals({}, [], None, search_outage="ForbiddenError: x")}
    assert down["search_available"]["score"] == 0.0
    assert "ForbiddenError" in down["search_available"]["comment"]
    up = {s["key"]: s for s in health_signals({}, [], None)}
    assert up["search_available"]["score"] == 1.0


def test_every_search_failing_is_not_blamed_on_the_archive() -> None:
    # The quarantine archived the failures, so it was working. Flagging the
    # archive here would send someone to debug the wrong layer.
    signals = {
        s["key"]: s for s in health_signals(_failed_search_files(3), [_skip_reply()], None)
    }
    assert signals["source_archive_ok"]["score"] == 1.0
    assert "every search failed" in signals["source_archive_ok"]["comment"]


def test_a_healthy_run_sends_no_alert() -> None:
    report = (
        "# Throughline — This Week in AI · 2026-11-09\n\nThe lede.\n\n"
        "## A topic (new)\nA claim with a number, 40% [1]. More text to make the report "
        "long enough to count as a real one, with a second sentence after it.\n\n"
        "## Sources\n\n1. Reuters — A story — 2026-11-09 — https://www.reuters.com/a\n"
    )
    files = {
        "/output/report.md": {"content": report, "encoding": "utf-8"},
        "/research/topic/sources/s0.md": {"content": "raw", "encoding": "utf-8"},
    }
    signals = run_signals({"files": files, "messages": []}, report, delivered=True)
    assert alert_text(signals) is None


def test_the_alert_for_a_lost_week_says_why() -> None:
    # The shape of the 28 Sep run: no report, the plan refused, 72 failed searches.
    state = {"files": _failed_search_files(72), "messages": [_skip_reply()]}
    signals = run_signals(state, "", search_outage=f"ForbiddenError: {PLAN_LIMIT}")

    text = alert_text(signals, today=date(2026, 9, 28))

    assert text.startswith("*Throughline run alert · 2026-09-28*")
    assert "No report this week." in text
    assert "usage limit" in text
    assert "72 search(es) failed" in text
    assert "every search failed" not in text, "a passing signal is not alert material"


def test_a_report_that_was_not_delivered_still_alerts() -> None:
    report = (
        "# Throughline — This Week in AI · 2026-11-09\n\nThe lede.\n\n"
        "## A topic (new)\nA claim [1]. Enough text here to make this a real report "
        "rather than a stub, with a second sentence for good measure.\n\n"
        "## Sources\n\n1. Reuters — A story — 2026-11-09 — https://www.reuters.com/a\n"
    )
    files = {"/output/report.md": {"content": report, "encoding": "utf-8"}}
    text = alert_text(run_signals({"files": files, "messages": []}, report, delivered=False))
    assert "This week's run needs a look." in text
    assert "NOT delivered" in text


def test_the_hook_posts_an_alert_when_no_report_was_written(monkeypatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(agent, "deliver_alert", lambda text: sent.append(text) or True)
    monkeypatch.setattr(agent, "search_outage", lambda: f"ForbiddenError: {PLAN_LIMIT}")

    update = agent.renumber_citations_middleware.after_agent(
        {"files": _failed_search_files(2), "messages": [_skip_reply()]}, None
    )

    assert update is None, "there is no report to renumber"
    assert len(sent) == 1
    assert "No report this week." in sent[0]


# --- the alert goes where the report goes -----------------------------------


class _Resp:
    def __init__(self, body: dict):
        self.status = 200
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_the_alert_is_one_channel_message_with_previews_off(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")
    payloads: list[dict] = []

    def _fake_urlopen(request, timeout=0):
        payloads.append(json.loads(request.data.decode()))
        return _Resp({"ok": True, "ts": "1.0"})

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)

    assert delivery.deliver_alert("*Throughline run alert*\nNo report this week.") is True
    assert len(payloads) == 1
    assert payloads[0]["channel"] == "C123"
    assert "thread_ts" not in payloads[0]
    assert payloads[0]["unfurl_links"] is False


def test_an_alert_that_cannot_be_sent_never_raises(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")

    def _boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _boom)
    assert delivery.deliver_alert("alert") is False
    assert delivery.deliver_alert("") is False
