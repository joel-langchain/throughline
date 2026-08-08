"""Reference-free evaluators for Throughline reports.

Each evaluator scores a finished report on ONE dimension by reading only the
report itself (and, for dedup, the prior-coverage ledger) — never the golden
label. That reference-free design is deliberate: the exact same functions grade
the frozen golden set offline here AND live report traces online later.

Four deterministic scorers (fast, free, no flakiness) target the planted defects:

    source_quality — cited domains are reputable, none are press-release/wire
    groundedness   — every quantitative claim carries an inline [n] citation
    dedup          — no topic tagged NEW was already covered in a prior week
    voice          — measured tone, not marketing hype (hype-word density)

Plus one LLM-as-judge (voice_judge) for the prose quality the code can't grade.
The deterministic four are the CI backbone; the judge is the qualitative layer.

Each function returns a LangSmith feedback dict {"key", "score", "comment"} and
tolerates being called with a RunTree (local evaluate()) or a plain dict (online).
"""

from __future__ import annotations

import re

from throughline.config import PRESS_RELEASE_DOMAINS, REPUTABLE_DOMAINS

_URL = re.compile(r"https?://[^\s)\]]+")
_HEADING = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_CITE = re.compile(r"\[\d+\]")

# Marketing-hype phrases that clash with Throughline's measured, analytical voice.
HYPE_TERMS = (
    "game-changing",
    "game changing",
    "gamechanging",
    "revolutionary",
    "breakthrough",
    "mind-blowing",
    "mindblowing",
    "jaw-dropping",
    "world-changing",
    "unprecedented",
    "insane",
    "unbelievable",
    "incredible",
    "blow your mind",
    "transform everything",
    "you won't believe",
    "must-see",
    "skyrocket",
)
# Hype terms per 100 words tolerated before the voice score starts dropping.
_HYPE_THRESHOLD = 1.0


# --- shared helpers ---------------------------------------------------------


def _domain(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


def _domain_in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _body(report: str) -> str:
    """Report text minus the Sources section and all heading lines."""
    main = re.split(r"^##\s+Sources\b", report, flags=re.MULTILINE)[0]
    return "\n".join(ln for ln in main.splitlines() if not ln.lstrip().startswith("#"))


def _coverage_topics(coverage: str) -> list[str]:
    topics: list[str] = []
    for line in coverage.splitlines():
        line = line.strip()
        if line.startswith("- "):
            name = re.split(r"[—–-]", line[2:], maxsplit=1)[0]
            if name.strip():
                topics.append(_normalise(name))
    return topics


# --- deterministic scorers (pure; return (score, comment)) ------------------


def score_source_quality(report: str) -> tuple[float, str]:
    domains = {_domain(u.rstrip(".,);]")) for u in _URL.findall(report)}
    if not domains:
        return 0.0, "no cited sources"
    denied = sorted(d for d in domains if _domain_in(d, PRESS_RELEASE_DOMAINS))
    if denied:
        return 0.0, f"press-release/wire domain cited: {', '.join(denied)}"
    reputable = sorted(d for d in domains if _domain_in(d, REPUTABLE_DOMAINS))
    score = len(reputable) / len(domains)
    if score == 1.0:
        return 1.0, "all cited sources reputable"
    unknown = sorted(d for d in domains if d not in reputable)
    return round(score, 3), f"non-reputable sources: {', '.join(unknown)}"


def score_groundedness(report: str) -> tuple[float, str]:
    total = cited = 0
    uncited: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", _body(report)):
        if re.search(r"\d", _CITE.sub("", sentence)):  # a quantitative claim
            total += 1
            if _CITE.search(sentence):
                cited += 1
            else:
                uncited.append(" ".join(sentence.split())[:70])
    if total == 0:
        return 1.0, "no quantitative claims to ground"
    score = cited / total
    if not uncited:
        return 1.0, f"all {total} quantitative claims cited"
    return round(score, 3), f"uncited claim(s): {'; '.join(uncited)}"


def score_dedup(report: str, coverage: str) -> tuple[float, str]:
    covered = _coverage_topics(coverage)
    if not covered:
        return 1.0, "no prior coverage to dedup against"
    repeats: list[str] = []
    for heading in _HEADING.findall(report):
        low = heading.lower()
        if low.strip().startswith("sources"):
            continue
        is_new = "(new)" in low and "(developing)" not in low
        name = _normalise(re.sub(r"\(.*?\)", "", heading))
        if is_new and any(name == c or name in c or c in name for c in covered):
            repeats.append(name)
    if repeats:
        return 0.0, f"topic tagged NEW but already covered: {', '.join(repeats)}"
    return 1.0, "no repeated topics"


def score_voice(report: str) -> tuple[float, str]:
    body = _body(report)
    words = re.findall(r"\w+", body)
    lower = body.lower()
    hits = sum(lower.count(term) for term in HYPE_TERMS)
    density = hits / max(len(words), 1) * 100
    score = max(0.0, 1.0 - density / _HYPE_THRESHOLD)
    return round(score, 3), f"hype density {density:.2f} per 100 words ({hits} hits)"


# --- LangSmith adapters (reference-free) ------------------------------------


def _extract(run, example) -> tuple[str, str]:
    """Pull (report, coverage) from a RunTree or dict; fall back to example inputs."""
    outputs = getattr(run, "outputs", None)
    if outputs is None and isinstance(run, dict):
        outputs = run.get("outputs")
    outputs = outputs or {}
    report = outputs.get("report", "")
    coverage = outputs.get("coverage", "")
    if not coverage and example is not None:
        inputs = getattr(example, "inputs", None)
        if inputs is None and isinstance(example, dict):
            inputs = example.get("inputs")
        coverage = (inputs or {}).get("coverage", "")
    return report, coverage


def source_quality(run, example=None) -> dict:
    report, _ = _extract(run, example)
    score, comment = score_source_quality(report)
    return {"key": "source_quality", "score": score, "comment": comment}


def groundedness(run, example=None) -> dict:
    report, _ = _extract(run, example)
    score, comment = score_groundedness(report)
    return {"key": "groundedness", "score": score, "comment": comment}


def dedup(run, example=None) -> dict:
    report, coverage = _extract(run, example)
    score, comment = score_dedup(report, coverage)
    return {"key": "dedup", "score": score, "comment": comment}


def voice(run, example=None) -> dict:
    report, _ = _extract(run, example)
    score, comment = score_voice(report)
    return {"key": "voice", "score": score, "comment": comment}


def voice_judge(run, example=None) -> dict:
    """LLM-as-judge for prose the deterministic voice score can't grade.

    Makes one cheap model call to rate whether the report reads in Throughline's
    measured, analytical, anti-hype voice. Reference-free, so reusable online.
    """
    from pydantic import BaseModel, Field

    from throughline.models import model

    report, _ = _extract(run, example)

    class Verdict(BaseModel):
        on_voice: bool = Field(description="True if measured/analytical, not hypey marketing")
        reason: str = Field(description="One short sentence of justification")

    verdict = model.with_structured_output(Verdict).invoke(
        [
            (
                "system",
                "You judge whether a weekly AI-news report reads in a measured, "
                "analytical, anti-hype voice: plain language, specific and sourced, "
                "no marketing superlatives or breathless claims. Answer on_voice=false "
                "if any part reads like hype or a press release.",
            ),
            ("human", report),
        ]
    )
    return {
        "key": "voice_llm_judge",
        "score": 1.0 if verdict.on_voice else 0.0,
        "comment": verdict.reason,
    }
