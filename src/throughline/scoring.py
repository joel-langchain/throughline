"""Reference-free scorers for a Throughline report.

Each function scores a finished report on ONE dimension by reading only the
report itself (and, for dedup, the prior-coverage ledger) — never a golden label.
That reference-free design is what lets the SAME function grade the frozen golden
set in CI and a live production report online, so a score means the same thing in
both places.

These live in the library rather than in `evals/` because the deployed agent
scores its own runs (see `monitoring.py`), and `evals/` is a development harness
that is not installed with the package. `evals/evaluators.py` wraps these in
LangSmith adapters and adds the LLM judge.

Five dimensions:

    source_quality     — cited domains are reputable, none are press-release/wire
    groundedness       — every quantitative claim carries an inline [n] citation
    citation_integrity — every [n] resolves 1:1 to a contiguous Sources list
    dedup              — no topic tagged NEW was already covered in a prior week
    voice              — measured tone, not marketing hype (hype-word density)

Each returns `(score, comment)`: a 0-1 score, and a comment saying what it found,
which is what makes a low score actionable rather than merely alarming.
"""

from __future__ import annotations

import re

from throughline.config import PRESS_RELEASE_DOMAINS, REPUTABLE_DOMAINS

_URL = re.compile(r"https?://[^\s)\]]+")
_HEADING = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_CITE = re.compile(r"\[\d+\]")
_CITE_N = re.compile(r"\[(\d+)\]")
_SOURCE_LINE = re.compile(r"^\s*(\d+)[.)]\s+\S", re.MULTILINE)

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


def _sources_block(report: str) -> str:
    """The Sources section text (everything after the '## Sources' heading)."""
    parts = re.split(r"^##\s+Sources\b", report, flags=re.MULTILINE)
    return parts[1] if len(parts) > 1 else ""


def _coverage_topics(coverage: str) -> list[str]:
    topics: list[str] = []
    for line in coverage.splitlines():
        line = line.strip()
        if line.startswith("- "):
            name = re.split(r"[—–-]", line[2:], maxsplit=1)[0]
            if name.strip():
                topics.append(_normalise(name))
    return topics


# --- the scorers ------------------------------------------------------------


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


def score_citation_integrity(report: str) -> tuple[float, str]:
    """Every inline [n] resolves to a Sources entry, and vice versa, contiguously.

    Catches the failure mode where sections use local per-section numbering while
    the Sources list is global: dangling markers (an [n] with no source), orphan
    sources (a listed source nothing cites), and non-contiguous numbering.
    """
    inline = {int(n) for n in _CITE_N.findall(_body(report))}
    sources = {int(n) for n in _SOURCE_LINE.findall(_sources_block(report))}
    if not inline and not sources:
        return 0.0, "no citations or sources found"
    if not sources:
        return 0.0, "inline citations present but no Sources list"

    dangling = sorted(inline - sources)  # [n] markers pointing at nothing
    orphan = sorted(sources - inline)  # listed sources nothing cites
    contiguous = sources == set(range(1, max(sources) + 1))

    problems = set(dangling) | set(orphan)
    if not contiguous:
        problems |= set(range(1, max(sources) + 1)) - sources
    universe = inline | sources | problems
    score = max(0.0, 1.0 - len(problems) / max(len(universe), 1))

    if not dangling and not orphan and contiguous:
        return 1.0, f"all {len(sources)} citations resolve 1:1 and numbering is contiguous"
    notes: list[str] = []
    if dangling:
        notes.append(f"dangling marker(s) with no source: {dangling}")
    if orphan:
        notes.append(f"orphan source(s) never cited: {orphan}")
    if not contiguous:
        notes.append(
            f"non-contiguous source numbering (max {max(sources)}, {len(sources)} listed)"
        )
    return round(score, 3), "; ".join(notes)


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
