"""Deterministic citation numbering for the finished report.

Numbering citations is a mechanical, bijective task, and asking the model to do
it is unreliable: with five or six sections it drifts into per-section local
numbering (``[1][2]`` restarting each section, sometimes with ad-hoc ``[1_v]``
suffixes) while the Sources list stays global — so markers dangle and sources are
orphaned. This module takes numbering away from the model entirely.

The contract the editor writes to:
  * In prose, each citation is the source URL wrapped in double brackets —
    ``[[https://example.com/article]]`` — placed right after the claim. Stack
    them for multiple sources: ``[[url1]][[url2]]``.
  * The Sources section lists one source per line, each line carrying its URL
    (numbered or not — the number is ignored and rewritten).

``renumber`` then, purely deterministically:
  1. walks the body and assigns global numbers 1..N to URLs by first appearance
     (a repeated URL reuses its number),
  2. rewrites every ``[[url]]`` marker to ``[n]``,
  3. rebuilds the Sources list in 1..N order using each URL's metadata line,
  4. drops orphan sources (listed but never cited) and flags any cited URL that
     has no Sources line.

The result is a guaranteed 1:1 correspondence between inline markers and the
Sources list — the exact property the ``citation_integrity`` evaluator checks.
"""

from __future__ import annotations

import re

# A citation marker in the prose: the source URL wrapped in double brackets.
_MARKER = re.compile(r"\[\[\s*(https?://[^\]\s]+?)\s*\]\]")
# Any http(s) URL (used to pull the URL out of a Sources line).
_URL = re.compile(r"https?://[^\s)\]]+")
# A leading list marker on a Sources line: "1. ", "1) ", "- ", "* ".
_LEAD = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+")
_SOURCES_SPLIT = re.compile(r"^(##\s+Sources\b.*)$", re.MULTILINE)


def _normalise_url(url: str) -> str:
    """Canonical form for matching an inline URL to a Sources line.

    Trims surrounding whitespace, trailing sentence punctuation, and a single
    trailing slash so ``https://x/a/`` and ``https://x/a`` compare equal.
    """
    url = url.strip().rstrip(".,);]")
    return url[:-1] if url.endswith("/") else url


def _split_sources(report: str) -> tuple[str, str, str]:
    """Return (body, sources_heading, sources_block).

    ``body`` is everything before the ``## Sources`` heading; ``sources_block``
    is everything after it. If there is no Sources heading, both trailing parts
    are empty and the whole report is the body.
    """
    parts = _SOURCES_SPLIT.split(report, maxsplit=1)
    if len(parts) < 3:
        return report, "", ""
    return parts[0], parts[1], parts[2]


def _metadata_map(sources_block: str) -> dict[str, str]:
    """Map each source's normalised URL to its display text (URL kept, number/bullet stripped)."""
    meta: dict[str, str] = {}
    for line in sources_block.splitlines():
        m = _URL.search(line)
        if not m:
            continue
        text = _LEAD.sub("", line).strip()
        meta[_normalise_url(m.group(0))] = text
    return meta


def renumber(report: str) -> tuple[str, list[str]]:
    """Assign global 1..N citation numbers deterministically. Returns (report, warnings).

    If the report contains no ``[[url]]`` markers (the model did not follow the
    contract), it is returned unchanged with a single warning, so a report is
    never destroyed — the ``citation_integrity`` evaluator remains the backstop.
    """
    body, heading, sources_block = _split_sources(report)

    ordered: list[str] = []  # cited URLs in first-appearance order
    number_of: dict[str, int] = {}
    for m in _MARKER.finditer(body):
        url = _normalise_url(m.group(1))
        if url not in number_of:
            number_of[url] = len(ordered) + 1
            ordered.append(url)

    if not ordered:
        return report, ["no [[url]] citation markers found — report left unchanged"]

    warnings: list[str] = []

    # 2. Rewrite each [[url]] marker to its global [n].
    def _sub(m: re.Match[str]) -> str:
        return f"[{number_of[_normalise_url(m.group(1))]}]"

    new_body = _MARKER.sub(_sub, body)

    # 3. Rebuild the Sources list in 1..N order from each URL's metadata line.
    meta = _metadata_map(sources_block)
    listed = set(meta)
    lines = []
    for n, url in enumerate(ordered, start=1):
        text = meta.get(url)
        if text is None:
            text = url
            warnings.append(f"cited URL has no Sources entry: {url}")
        lines.append(f"{n}. {text}")

    orphans = sorted(listed - set(ordered))
    if orphans:
        warnings.append(f"dropped {len(orphans)} orphan source(s) never cited: {', '.join(orphans)}")

    if not heading:
        # No Sources section existed; append one so the report is self-contained.
        warnings.append("no '## Sources' section found — one was appended")
        heading = "## Sources"
        new_report = new_body.rstrip() + "\n\n" + heading + "\n" + "\n".join(lines) + "\n"
    else:
        new_report = new_body + heading + "\n\n" + "\n".join(lines) + "\n"

    return new_report, warnings
