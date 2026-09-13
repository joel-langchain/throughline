"""Slack delivery is optional and best-effort: nothing configured -> no-op, never raises.

Two transports are covered: the bot token (summary in the channel, full report in
its thread) and the legacy incoming webhook (one truncated post).
"""

from __future__ import annotations

import json

import pytest

from throughline import delivery

REPORT = """# Throughline — This Week in AI · 2026-08-31

Since last week, two storylines converged in a single incident.

## OpenAI's Rogue Agents Hack Hugging Face (new)

Body text [1].

## Anthropic's AI Improves Itself (new)

More body text [2].

## Sources

1. Reuters — A story — 2026-08-26 — https://www.reuters.com/a
2. The Verge — Another story — 2026-08-26 — https://www.theverge.com/b
"""


@pytest.fixture(autouse=True)
def _clear_slack_env(monkeypatch):
    """Never let a developer's real Slack config leak into a test."""
    for var in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)


class _Resp:
    """Minimal urlopen context manager."""

    def __init__(self, body: dict | None = None, status: int = 200):
        self.status = status
        self._body = json.dumps(body or {}).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _capture_api(monkeypatch, responses: list[dict]):
    """Fake chat.postMessage, returning `responses` in order. Records each call."""
    calls: list[dict] = []
    queue = list(responses)

    def _fake_urlopen(request, timeout=0):
        calls.append(
            {
                "url": request.full_url,
                "auth": request.headers.get("Authorization"),
                "payload": json.loads(request.data.decode()),
            }
        )
        return _Resp(queue.pop(0) if queue else {"ok": True, "ts": "1.0"})

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)
    return calls


# --- Formatting ------------------------------------------------------------


def test_to_slack_converts_headings_and_bold() -> None:
    out = delivery._to_slack("# Title\n\n## Section\n\n**bold** and text")
    assert "*Title*" in out
    assert "*Section*" in out
    assert "#" not in out
    assert "**" not in out


def test_summary_has_title_lede_headlines_and_source_count() -> None:
    summary = delivery.summarize_report(REPORT)
    assert "Throughline — This Week in AI · 2026-08-31" in summary
    assert "Since last week, two storylines converged" in summary
    assert "1. OpenAI's Rogue Agents Hack Hugging Face (new)" in summary
    assert "2. Anthropic's AI Improves Itself (new)" in summary
    # Sources is not a topic; its count goes in the footer instead.
    assert "3. Sources" not in summary
    assert "2 sources" in summary


def test_summary_length_is_independent_of_body_length() -> None:
    # The whole point of the change: the channel message stays skimmable however
    # long the week's report runs, because only the lede and headlines go in it.
    fat = REPORT.replace("Body text [1].", "Body text [1]. " + "padding " * 2000)
    assert len(fat) > len(REPORT) * 10
    assert delivery.summarize_report(fat) == delivery.summarize_report(REPORT)


def test_summary_survives_a_report_with_no_headings() -> None:
    summary = delivery.summarize_report("Just prose, no headings at all.")
    assert "Throughline" in summary
    assert "Just prose" in summary


def test_split_keeps_whole_report_when_it_fits() -> None:
    assert delivery._split_for_thread("short") == ["short"]


def test_split_chunks_on_paragraph_boundaries_without_losing_text() -> None:
    paragraphs = [f"paragraph {i} " + "x" * 5000 for i in range(20)]
    text = "\n\n".join(paragraphs)
    chunks = delivery._split_for_thread(text)
    assert len(chunks) > 1
    assert all(len(c) <= delivery._MAX_CHARS for c in chunks)
    # Nothing is dropped: every paragraph still appears exactly once.
    rejoined = "\n\n".join(chunks)
    for i in range(20):
        assert rejoined.count(f"paragraph {i} ") == 1


# --- Bot-token transport ---------------------------------------------------


def test_bot_posts_summary_then_report_in_thread(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C0BS57BKQ3B")
    calls = _capture_api(monkeypatch, [{"ok": True, "ts": "1756636485.1"}, {"ok": True}])

    assert delivery.deliver_report(REPORT) is True
    assert len(calls) == 2

    parent, reply = calls
    assert parent["url"] == delivery._POST_MESSAGE_URL
    assert parent["auth"] == "Bearer xoxb-test"
    assert parent["payload"]["channel"] == "C0BS57BKQ3B"
    assert "thread_ts" not in parent["payload"]
    # The channel message is the summary, not the body.
    assert "This week" in parent["payload"]["text"]
    assert "More body text" not in parent["payload"]["text"]
    # The body lands in the parent's thread.
    assert reply["payload"]["thread_ts"] == "1756636485.1"
    assert "More body text" in reply["payload"]["text"]


def test_bot_disables_unfurling_on_every_message(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")
    calls = _capture_api(monkeypatch, [{"ok": True, "ts": "1.0"}, {"ok": True}])

    delivery.deliver_report(REPORT)
    for call in calls:
        assert call["payload"]["unfurl_links"] is False
        assert call["payload"]["unfurl_media"] is False


def test_bot_reports_failure_when_slack_refuses_the_summary(monkeypatch) -> None:
    # Slack answers HTTP 200 with ok:false — the status code alone would lie.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")
    calls = _capture_api(monkeypatch, [{"ok": False, "error": "channel_not_found"}])

    assert delivery.deliver_report(REPORT) is False
    assert len(calls) == 1  # no thread reply attempted


def test_bot_keeps_the_summary_when_the_thread_reply_fails(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")
    _capture_api(monkeypatch, [{"ok": True, "ts": "1.0"}, {"ok": False, "error": "msg_too_long"}])

    # The week reached the channel, so this is a partial success, not a failure.
    assert delivery.deliver_report(REPORT) is True


def test_bot_swallows_transport_errors(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")

    def _boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _boom)
    assert delivery.deliver_report(REPORT) is False


def test_bot_rejects_a_value_that_is_not_a_token(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "my-slack-app")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")

    def _fail(*_a, **_k):
        raise AssertionError("must not POST when the value isn't a Slack token")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fail)
    assert delivery.deliver_report(REPORT) is False


def test_bot_token_without_a_channel_is_a_noop(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    def _fail(*_a, **_k):
        raise AssertionError("must not POST without a channel")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fail)
    assert delivery.deliver_report(REPORT) is False


def test_bot_token_is_preferred_over_the_webhook(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    calls = _capture_api(monkeypatch, [{"ok": True, "ts": "1.0"}, {"ok": True}])

    assert delivery.deliver_report(REPORT) is True
    assert all("hooks.slack.com" not in call["url"] for call in calls)


# --- Webhook transport (legacy) --------------------------------------------


def test_deliver_is_noop_when_nothing_is_configured(monkeypatch) -> None:
    def _fail(*_a, **_k):
        raise AssertionError("must not post when unconfigured")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fail)
    assert delivery.deliver_report("# Report") is False


def test_deliver_posts_when_webhook_configured(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    captured: dict[str, str] = {}

    def _fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode()
        return _Resp()

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)
    assert delivery.deliver_report("# Throughline\n\nBody [1].") is True
    assert "hooks.slack.com" in captured["url"]
    assert "Throughline" in captured["body"]


def test_webhook_truncates_an_oversized_report(monkeypatch) -> None:
    # A webhook cannot thread, so an over-long week is still capped on this path.
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    captured: dict[str, str] = {}

    def _fake_urlopen(request, timeout=0):
        captured["body"] = request.data.decode()
        return _Resp()

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)
    assert delivery.deliver_report("x" * (delivery._MAX_CHARS + 5000)) is True
    assert "truncated" in captured["body"]


def test_deliver_swallows_errors(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")

    def _boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _boom)
    # Never raises — a delivery failure must not break the run.
    assert delivery.deliver_report("# Report") is False


def test_deliver_rejects_non_webhook_value(monkeypatch) -> None:
    # The exact misconfig we hit: the whole `curl ...` command pasted as the value.
    monkeypatch.setenv(
        "SLACK_WEBHOOK_URL",
        "curl -X POST -H 'Content-type: application/json' --data '{\"text\":\"hi\"}' "
        "https://hooks.slack.com/services/T/B/x",
    )

    def _fail(*_a, **_k):
        raise AssertionError("must not POST when the value isn't a bare webhook URL")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fail)
    assert delivery.deliver_report("# Report") is False
