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

Seven dimensions:

    source_quality     — cited domains are reputable, none are press-release/wire
    press_release_path — no cited URL is a press release or paid post by its path
    source_recency     — sources are dated, to the day, inside the week covered
    groundedness       — every quantitative claim and direct quote carries an
                         inline [n] citation
    citation_integrity — every [n] resolves 1:1 to a contiguous Sources list
    dedup              — no topic tagged NEW was already covered in a prior week
    voice              — measured tone, not marketing hype (hype-word density)

Each returns `(score, comment)`: a 0-1 score, and a comment saying what it found,
which is what makes a low score actionable rather than merely alarming.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import NamedTuple
from urllib.parse import urlsplit

from throughline.config import (
    PRESS_RELEASE_DOMAINS,
    PRESS_RELEASE_PATHS,
    REPUTABLE_DOMAINS,
)

_URL = re.compile(r"https?://[^\s)\]]+")
_HEADING = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_CITE = re.compile(r"\[\d+\]")
_CITE_N = re.compile(r"\[(\d+)\]")
_SOURCE_LINE = re.compile(r"^\s*(\d+)[.)]\s+\S", re.MULTILINE)
_SOURCE_ENTRY = re.compile(r"\s*(\d+)[.)]\s+\S")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# A date in a Sources line: a full day ("2026-11-09") or a month only ("2026-11").
_SOURCE_DATE = re.compile(r"\b(\d{4})-(\d{2})(?:-(\d{2}))?\b")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_LAST_WORD = re.compile(r"([A-Za-z][A-Za-z.]*)\.$")
# A direct quote: text in double quotes, straight or curly.
_QUOTE = re.compile(r"[\"\u201c]([^\"\u201d]+)[\"\u201d]")
# Words that attribute a quote to someone. Without one, quotation marks usually
# wrap a term ("human in the loop") rather than words a person said, and a term
# needs no citation.
_ATTRIBUTION = re.compile(
    r"\b(said|says|told|wrote|writes|called|argued|argues|warned|warns|"
    r"according to|described|describes|framed|frames|attributing|attributed|"
    r"denied|denies|stated|claimed|added|noted|insisted|admitted)\b",
    re.IGNORECASE,
)
# A single capital or dotted capitals before the final stop: an initial, "S.",
# "H.R.", "U.S.". These sit mid-sentence far more often than at the end of one.
_INITIALS = re.compile(r"[A-Z](\.[A-Z])*")

# How many days before the report date a source may be dated and still count as
# this week's. Matches the search window (tools._search asks for days=7).
RECENCY_WINDOW_DAYS = 7

# Abbreviations that end in a full stop but rarely end a sentence. Splitting on
# them cut "Carbon outperforms Fable 5.2 (53.9% vs. 51.4%), ... [11][12]." in two
# and scored the first half as uncited. Rejoining only lengthens a sentence, so
# the usual worst case is a claim that borrows the next sentence's citation.
# "etc." is left out because it often does end one.
_ABBREVIATIONS = frozenset(
    {
        "vs", "v", "e.g", "i.e", "cf", "approx", "al", "fig", "no",
        "dr", "mr", "mrs", "ms", "prof", "sen", "rep", "gov", "st",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
        "oct", "nov", "dec",
    }
)  # fmt: skip

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


def _sentences(text: str) -> list[str]:
    """Split prose into sentences, rejoining any split made after an abbreviation."""
    sentences: list[str] = []
    for piece in _SENTENCE_BREAK.split(text):
        last = _LAST_WORD.search(sentences[-1].rstrip()) if sentences else None
        word = last.group(1) if last else ""
        if word and (word.lower() in _ABBREVIATIONS or _INITIALS.fullmatch(word)):
            sentences[-1] = f"{sentences[-1]} {piece}"
        else:
            sentences.append(piece)
    return sentences


def _has_quote(sentence: str) -> bool:
    """True if the sentence quotes someone: two or more words in double quotes,
    attributed to a speaker or writer ("said", "told", "denied", "frames")."""
    quoted = any(len(q.split()) >= 2 for q in _QUOTE.findall(sentence))
    return quoted and bool(_ATTRIBUTION.search(_QUOTE.sub("", sentence)))


def _report_date(report: str) -> date | None:
    """The date in the report's title line ("# Throughline — ... · 2026-11-16")."""
    title = next((ln for ln in report.splitlines() if ln.startswith("# ")), "")
    found = _ISO_DAY.search(title)
    try:
        return date.fromisoformat(found.group(0)) if found else None
    except ValueError:
        return None


def _is_press_release_path(url: str) -> bool:
    segments = urlsplit(url).path.lower().split("/")
    return any(seg in PRESS_RELEASE_PATHS for seg in segments)


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


def _cited_urls(report: str) -> list[str]:
    return sorted({u.rstrip(".,);]") for u in _URL.findall(report)})


def press_release_urls(report: str) -> list[str]:
    """Cited URLs whose path marks a press release or paid post."""
    return [u for u in _cited_urls(report) if _is_press_release_path(u)]


def source_lines(report: str) -> dict[str, str]:
    """Each numbered Sources entry's text, keyed by its number, number stripped."""
    lines: dict[str, str] = {}
    for line in _sources_block(report).splitlines():
        entry = _SOURCE_ENTRY.match(line)
        if entry:
            lines[entry.group(1)] = line[entry.end() - 1 :].strip()
    return lines


class SourceDates(NamedTuple):
    """Where each source's date falls against the week the report covers."""

    start: date
    end: date
    total: int
    outside: list[tuple[str, str]]  # (source number, ISO date) before or after the week
    no_day: list[str]  # source numbers dated to the month only
    undated: list[str]  # source numbers with no readable date


def source_dates(report: str, run_date: date | None = None) -> SourceDates | None:
    """Check each Sources date against the week up to the report date.

    Returns None when there is no run date and the title carries none either.
    The date is read with the URL removed, because URLs often carry dates of
    their own, and the last date on the line wins because the date field
    follows the title.
    """
    run_date = run_date or _report_date(report)
    if run_date is None:
        return None
    start = run_date - timedelta(days=RECENCY_WINDOW_DAYS)
    total = 0
    outside: list[tuple[str, str]] = []
    no_day: list[str] = []
    undated: list[str] = []
    for line in _sources_block(report).splitlines():
        entry = _SOURCE_ENTRY.match(line)
        if not entry:
            continue
        total += 1
        n = entry.group(1)
        dates = _SOURCE_DATE.findall(_URL.sub("", line))
        if not dates:
            undated.append(n)
            continue
        year, month, day = dates[-1]
        if not day:
            no_day.append(n)
            continue
        try:
            when = date(int(year), int(month), int(day))
        except ValueError:
            undated.append(n)
            continue
        if not start <= when <= run_date:
            outside.append((n, when.isoformat()))
    return SourceDates(start, run_date, total, outside, no_day, undated)


def score_press_release_path(report: str) -> tuple[float, str]:
    """No cited URL is a press release or paid post, judged by its path.

    The domain rule in score_source_quality catches wire services. It cannot see
    a release syndicated onto a real outlet's site, such as
    kitsapsun.com/press-release/..., which carried claims about named
    legislators in the 16 Nov 2026 report. Binary, like the domain rule: one
    release is enough to fail.
    """
    urls = _cited_urls(report)
    if not urls:
        return 1.0, "no cited URLs"
    hits = press_release_urls(report)
    if hits:
        return 0.0, f"press release or paid post cited: {', '.join(hits)}"
    return 1.0, f"none of {len(urls)} cited URLs is a press release by its path"


def score_source_recency(report: str, run_date: date | None = None) -> tuple[float, str]:
    """Share of sources dated, to the day, inside the week the report covers.

    Each Sources line reads "<outlet> — <title> — <date> — <url>". A source counts
    only when its date has a day and falls in the RECENCY_WINDOW_DAYS up to the
    report date. A month-only date ("2026-11") or a missing one cannot be checked,
    so it does not count, and nor does a source from before the window, which is
    older news presented as this week's.
    """
    dates = source_dates(report, run_date)
    if dates is None:
        return 1.0, "no report date in the title to check sources against"
    if dates.total == 0:
        return 1.0, "no sources to date"
    start, end = dates.start, dates.end
    in_window = dates.total - len(dates.outside) - len(dates.no_day) - len(dates.undated)
    if in_window == dates.total:
        return 1.0, f"all {dates.total} sources dated {start} to {end}"
    notes: list[str] = []
    if dates.outside:
        listed = ", ".join(f"[{n}] {iso}" for n, iso in dates.outside)
        notes.append(f"dated outside {start} to {end}: {listed}")
    if dates.no_day:
        notes.append(f"no day in the date: {', '.join(f'[{n}]' for n in dates.no_day)}")
    if dates.undated:
        notes.append(f"undated: {', '.join(f'[{n}]' for n in dates.undated)}")
    return round(in_window / dates.total, 3), "; ".join(notes)


def uncited_claims(report: str) -> tuple[int, list[str]]:
    """How many claims the report makes, and the uncited ones in full.

    A sentence is a claim if it holds a number or quotes someone. A quote is a
    claim about what a named person or organisation said, so it needs a source as
    much as a figure does. Quotation marks around a term, with no one said to
    have said it, are not a quote.
    """
    total = 0
    uncited: list[str] = []
    for sentence in _sentences(_body(report)):
        bare = _CITE.sub("", sentence)
        if re.search(r"\d", bare) or _has_quote(bare):
            total += 1
            if not _CITE.search(sentence):
                uncited.append(" ".join(sentence.split()))
    return total, uncited


def score_groundedness(report: str) -> tuple[float, str]:
    """Every quantitative claim and every direct quote carries an inline [n] citation."""
    total, uncited = uncited_claims(report)
    if total == 0:
        return 1.0, "no quantitative claims or quotes to ground"
    if not uncited:
        return 1.0, f"all {total} quantitative claims and quotes cited"
    score = (total - len(uncited)) / total
    return round(score, 3), f"uncited claim(s): {'; '.join(c[:70] for c in uncited)}"


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


def hype_terms_found(report: str) -> list[str]:
    """The hype terms that appear in the report body, each listed once."""
    lower = _body(report).lower()
    return [term for term in HYPE_TERMS if term in lower]


def score_voice(report: str) -> tuple[float, str]:
    body = _body(report)
    words = re.findall(r"\w+", body)
    lower = body.lower()
    hits = sum(lower.count(term) for term in HYPE_TERMS)
    density = hits / max(len(words), 1) * 100
    score = max(0.0, 1.0 - density / _HYPE_THRESHOLD)
    return round(score, 3), f"hype density {density:.2f} per 100 words ({hits} hits)"
