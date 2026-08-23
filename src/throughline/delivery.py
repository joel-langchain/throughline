"""Deliver the finished report to Slack (optional, best-effort).

When ``SLACK_WEBHOOK_URL`` is set (a Slack incoming webhook), the finished weekly
report is posted to that channel so a scheduled run lands somewhere readable
without a laptop. Unset → no-op. Uses only the standard library, and never raises:
a delivery failure must not fail the run or lose the report from state.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

# Slack accepts large text but recommends staying well under the hard cap.
_MAX_CHARS = 39000


def _to_slack(markdown: str) -> str:
    """Light markdown -> Slack mrkdwn: Slack renders neither `#` headings nor `**`."""
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", markdown, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS].rstrip() + "\n\n…(truncated — full report in LangSmith)"
    return text


def deliver_report(report_body: str) -> bool:
    """Post the report to Slack if configured. Returns True if a post was sent.

    Best-effort: returns False (never raises) when unconfigured or on any error,
    so delivery can never break the run or drop the report from agent state.
    """
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url or not report_body:
        return False
    payload = json.dumps({"text": _to_slack(report_body)}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
