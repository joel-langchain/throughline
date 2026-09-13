"""Deliver the finished report to Slack (optional, best-effort).

Two transports, chosen by what is configured:

* **Bot token** (``SLACK_BOT_TOKEN`` + ``SLACK_CHANNEL_ID``, preferred) — posts a
  short summary as the channel message and the full report as replies in its
  thread. In a channel other people read, a wall of text is unreadable and there
  is nowhere obvious to respond; a skimmable parent plus a threaded body gives
  each week one post and keeps the discussion attached to it.
* **Incoming webhook** (``SLACK_WEBHOOK_URL``, legacy) — one post of the whole
  report, truncated at Slack's limit. A webhook cannot thread: it returns no
  message ``ts``, so there is no parent to reply under. This path is kept
  unchanged so an existing webhook deployment keeps working untouched.

Neither set → no-op. Uses only the standard library, and never raises: a delivery
failure must not fail the run or lose the report from state.

Delivery logs a one-line, secret-free outcome (``[delivery] ...``) so a run's
logs/trace show WHY a post did or didn't happen. The token and the webhook URL
are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

from throughline.citations import source_count

logger = logging.getLogger("throughline.delivery")

# Slack accepts large text but recommends staying well under the hard cap.
_MAX_CHARS = 39000

_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
# Bot (xoxb-) and user (xoxp-) tokens both authorise chat.postMessage.
_TOKEN_PREFIXES = ("xoxb-", "xoxp-")

# The report's own structure, which the summary is built from: an H1 title, a
# lede paragraph, then one H2 per topic and a final "## Sources".
_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_H2 = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_SOURCES_HEADING = re.compile(r"^sources\b", re.IGNORECASE)


def _to_slack(markdown: str) -> str:
    """Light markdown -> Slack mrkdwn: Slack renders neither `#` headings nor `**`."""
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", markdown, flags=re.MULTILINE)
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def _truncate(text: str) -> str:
    """Cap a single message at Slack's limit (the webhook path, which cannot thread)."""
    if len(text) <= _MAX_CHARS:
        return text
    return text[:_MAX_CHARS].rstrip() + "\n\n…(truncated — full report in LangSmith)"


def _split_for_thread(text: str) -> list[str]:
    """Split text into <= _MAX_CHARS chunks on paragraph boundaries.

    The thread is the right home for the full report, so a long week costs an
    extra reply rather than losing its Sources list off the end. A single
    paragraph over the cap is hard-sliced; there is nothing else to be done.
    """
    if len(text) <= _MAX_CHARS:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        while len(paragraph) > _MAX_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:_MAX_CHARS])
            paragraph = paragraph[_MAX_CHARS:]
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > _MAX_CHARS:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def summarize_report(report: str) -> str:
    """Build the channel-level message: title, the week's lede, and its headlines.

    Pure and side-effect free, so it can be unit-tested without touching Slack.
    It reads the structure the editor already writes, so nothing new is asked of
    the prompt. A report missing its headings still yields a usable message: the
    title falls back to a generic one and the headline list is simply omitted.
    """
    title_match = _H1.search(report)
    title = title_match.group(1).strip() if title_match else "Throughline — This Week in AI"

    # The lede is everything between the title and the first topic heading.
    after_title = report[title_match.end() :] if title_match else report
    first_h2 = _H2.search(after_title)
    lede = (after_title[: first_h2.start()] if first_h2 else after_title).strip()

    headlines = [
        h.strip() for h in _H2.findall(report) if not _SOURCES_HEADING.match(h.strip())
    ]

    parts = [f"*{title}*"]
    if lede:
        parts.append(lede)
    if headlines:
        numbered = "\n".join(f"{n}. {h}" for n, h in enumerate(headlines, start=1))
        parts.append(f"*This week*\n{numbered}")

    sources = source_count(report)
    footer = "Full report in thread"
    if sources:
        footer += f" · {sources} sources"
    parts.append(f"_{footer}._")

    return "\n\n".join(parts)


def _log(message: str) -> None:
    """Emit a secret-free delivery line to both the logger and stdout.

    stdout too, so the line is visible in the deployment's run logs even if log
    levels aren't configured. Never includes the token or the webhook URL.
    """
    logger.info(message)
    print(f"[delivery] {message}", flush=True)


def _post_message(token: str, payload: dict) -> dict:
    """Call chat.postMessage and return the parsed response.

    Slack answers HTTP 200 even when it refuses the call, so the ``ok`` field —
    not the status code — is what decides success. Raises on transport errors;
    callers catch.
    """
    request = urllib.request.Request(
        _POST_MESSAGE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _message(channel: str, text: str, thread_ts: str | None = None) -> dict:
    """A chat.postMessage payload with link previews off.

    Unfurling is disabled deliberately. A report cites twenty-plus sources, and
    letting Slack expand them would bury the text and render remote content
    pulled from untrusted web search into a channel other people read.
    """
    payload = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return payload


def _deliver_via_bot(token: str, channel: str, report_body: str) -> bool:
    """Post the summary to the channel, then the full report into its thread."""
    try:
        parent = _post_message(token, _message(channel, _to_slack(summarize_report(report_body))))
    except Exception as exc:
        _log(f"failed: {type(exc).__name__} posting the summary")
        return False
    if not parent.get("ok"):
        _log(f"failed: Slack refused the summary ({parent.get('error', 'unknown')})")
        return False

    thread_ts = parent.get("ts")
    if not thread_ts:
        # The summary is in the channel, so the week is not lost; only the body is.
        _log("partial: summary posted but Slack returned no ts, so the report has no thread")
        return True

    chunks = _split_for_thread(_to_slack(report_body))
    posted = 0
    for index, chunk in enumerate(chunks, start=1):
        text = chunk if len(chunks) == 1 else f"{chunk}\n\n_(part {index} of {len(chunks)})_"
        try:
            reply = _post_message(token, _message(channel, text, thread_ts=thread_ts))
        except Exception as exc:
            _log(f"partial: {type(exc).__name__} posting report part {index}/{len(chunks)}")
            break
        if not reply.get("ok"):
            _log(
                f"partial: Slack refused report part {index}/{len(chunks)} "
                f"({reply.get('error', 'unknown')})"
            )
            break
        posted += 1

    _log(f"posted to Slack: summary + {posted}/{len(chunks)} thread part(s)")
    return True


def _deliver_via_webhook(url: str, report_body: str) -> bool:
    """Post the whole report as one message. Unchanged legacy path."""
    if not url.startswith("https://hooks.slack.com/"):
        # A common misconfig is pasting the whole `curl ...` command, not the URL.
        _log(
            "skipped: SLACK_WEBHOOK_URL is not a Slack webhook URL "
            "(expected it to start with https://hooks.slack.com/)"
        )
        return False

    payload = json.dumps({"text": _truncate(_to_slack(report_body))}).encode("utf-8")
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


def deliver_report(report_body: str) -> bool:
    """Post the report to Slack if configured. Returns True if a post was sent.

    Prefers the bot-token transport (summary + threaded report) and falls back to
    the webhook. Best-effort: returns False (never raises) when unconfigured or on
    any error, so delivery can never break the run or drop the report from agent
    state. The outcome is logged, without secrets, so failures are diagnosable
    from the trace.
    """
    if not report_body:
        _log("skipped: no report body to send")
        return False

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL_ID", "").strip()
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

    if token and channel:
        if not token.startswith(_TOKEN_PREFIXES):
            _log(
                "skipped: SLACK_BOT_TOKEN is not a Slack token "
                "(expected it to start with xoxb- or xoxp-)"
            )
            return False
        return _deliver_via_bot(token, channel, report_body)

    if token and not channel:
        _log("skipped: SLACK_BOT_TOKEN is set but SLACK_CHANNEL_ID is not")
        return False

    if webhook:
        return _deliver_via_webhook(webhook, report_body)

    _log(
        "skipped: no Slack delivery configured "
        "(set SLACK_BOT_TOKEN + SLACK_CHANNEL_ID, or SLACK_WEBHOOK_URL)"
    )
    return False
