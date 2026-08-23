"""Deliver the finished report to Slack (optional, best-effort).

When ``SLACK_WEBHOOK_URL`` is set (a Slack incoming webhook), the finished weekly
report is posted to that channel so a scheduled run lands somewhere readable
without a laptop. Unset → no-op. Uses only the standard library, and never raises:
a delivery failure must not fail the run or lose the report from state.

Delivery logs a one-line, secret-free outcome (``[delivery] ...``) so a run's
logs/trace show WHY a post did or didn't happen — the webhook URL is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

logger = logging.getLogger("throughline.delivery")

# Slack accepts large text but recommends staying well under the hard cap.
_MAX_CHARS = 39000


def _to_slack(markdown: str) -> str:
    """Light markdown -> Slack mrkdwn: Slack renders neither `#` headings nor `**`."""
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", markdown, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS].rstrip() + "\n\n…(truncated — full report in LangSmith)"
    return text


def _log(message: str) -> None:
    """Emit a secret-free delivery line to both the logger and stdout.

    stdout too, so the line is visible in the deployment's run logs even if log
    levels aren't configured. Never includes the webhook URL.
    """
    logger.info(message)
    print(f"[delivery] {message}", flush=True)


def deliver_report(report_body: str) -> bool:
    """Post the report to Slack if configured. Returns True if a post was sent.

    Best-effort: returns False (never raises) when unconfigured or on any error,
    so delivery can never break the run or drop the report from agent state. The
    outcome is logged (without the URL) so failures are diagnosable from the trace.
    """
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        _log("skipped: SLACK_WEBHOOK_URL not set")
        return False
    if not report_body:
        _log("skipped: no report body to send")
        return False
    if not url.startswith("https://hooks.slack.com/"):
        # A common misconfig is pasting the whole `curl ...` command, not the URL.
        _log(
            "skipped: SLACK_WEBHOOK_URL is not a Slack webhook URL "
            "(expected it to start with https://hooks.slack.com/)"
        )
        return False

    payload = json.dumps({"text": _to_slack(report_body)}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            ok = 200 <= resp.status < 300
            _log(f"{'posted to Slack' if ok else 'unexpected status'}: HTTP {resp.status}")
            return ok
    except urllib.error.HTTPError as exc:
        # Slack returns a short text reason (e.g. 'no_service', 'invalid_payload').
        try:
            reason = exc.read().decode("utf-8", "replace")[:100]
        except Exception:
            reason = ""
        _log(f"failed: HTTP {exc.code} {reason}".rstrip())
        return False
    except Exception as exc:
        _log(f"failed: {type(exc).__name__}")
        return False
