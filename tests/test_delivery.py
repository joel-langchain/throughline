"""Slack delivery is optional and best-effort: no webhook -> no-op, never raises."""

from __future__ import annotations

from throughline import delivery


def test_to_slack_converts_headings_and_bold() -> None:
    out = delivery._to_slack("# Title\n\n## Section\n\n**bold** and text")
    assert "*Title*" in out
    assert "*Section*" in out
    assert "#" not in out
    assert "**" not in out


def test_deliver_is_noop_without_webhook(monkeypatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    def _fail(*_a, **_k):
        raise AssertionError("must not post when unconfigured")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fail)
    assert delivery.deliver_report("# Report") is False


def test_deliver_posts_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    captured: dict[str, str] = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode()
        return _Resp()

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)
    assert delivery.deliver_report("# Throughline\n\nBody [1].") is True
    assert "hooks.slack.com" in captured["url"]
    assert "Throughline" in captured["body"]


def test_deliver_swallows_errors(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")

    def _boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _boom)
    # Never raises — a delivery failure must not break the run.
    assert delivery.deliver_report("# Report") is False
