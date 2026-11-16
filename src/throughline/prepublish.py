"""Code-check findings for the final-pass reviewer, before the report ships.

The scorers in `throughline.scoring` grade the finished report after it has been
delivered and published. On 16 Nov 2026 they found an uncited benchmark claim
that the final-pass reviewer, told to check for exactly that, had approved. So
the same checks now run on the draft first and their findings go to the
reviewer, which lists them as issues for the editor to fix.

The draft still cites by [[url]] marker at review time. The scorers read
numbered [n] citations, so the draft is renumbered in memory here (the file is
not touched), and each finding names its source by the Sources line rather than
by a number the reviewer never sees.

No model calls: this is string work on the draft, run once per reviewer call.
"""

from __future__ import annotations

from throughline.citations import renumber
from throughline.scoring import (
    hype_terms_found,
    press_release_urls,
    source_dates,
    source_lines,
    uncited_claims,
)

# Long sentences are cut to this many characters in a finding; enough to find
# the sentence in the draft.
_QUOTE_CHARS = 240


class Findings:
    """Findings split by whether they alone justify a revision."""

    def __init__(self) -> None:
        self.must_fix: list[str] = []
        self.if_revising: list[str] = []

    def __bool__(self) -> bool:
        return bool(self.must_fix or self.if_revising)


def _clip(text: str) -> str:
    return text if len(text) <= _QUOTE_CHARS else text[: _QUOTE_CHARS - 3] + "..."


def report_findings(draft: str) -> Findings:
    """What the code checks find in a drafted report, worded for the reviewer."""
    found = Findings()
    numbered, warnings = renumber(draft)
    for warning in warnings:
        if warning.startswith("cited URL has no Sources entry"):
            found.must_fix.append(f"A cited URL is missing from Sources. {warning}.")

    _, uncited = uncited_claims(numbered)
    for sentence in uncited:
        found.must_fix.append(
            "Claim with no citation. Cite it from that topic's research, or cut it: "
            f'"{_clip(sentence)}"'
        )

    for url in press_release_urls(numbered):
        found.must_fix.append(
            f"Press release cited ({url}). Cut the claims that rest only on it."
        )

    terms = hype_terms_found(numbered)
    if terms:
        found.must_fix.append(
            f"Hype wording ({', '.join(terms)}). Replace it with plain wording."
        )

    dates = source_dates(numbered)
    if dates is not None:
        lines = source_lines(numbered)
        for n, iso in dates.outside:
            found.must_fix.append(
                f"Source dated {iso}, outside the week {dates.start} to {dates.end}: "
                f'"{_clip(lines.get(n, n))}". Say in the text when it happened, or '
                "cut it if it is not this week's news."
            )
        for n in dates.no_day:
            found.if_revising.append(
                f'Source date has no day: "{_clip(lines.get(n, n))}". Add the day '
                "if the research archive records it."
            )
        for n in dates.undated:
            found.if_revising.append(
                f'Source has no date: "{_clip(lines.get(n, n))}". Add it if the '
                "research archive records it."
            )
    return found


def findings_block(found: Findings) -> str:
    """The findings as a section appended to the reviewer's instructions, or ""."""
    if not found:
        return ""
    parts = [
        "AUTOMATED CHECK FINDINGS",
        "Code checks ran on this draft before you read it. They are exact, not "
        "opinions.",
    ]
    if found.must_fix:
        parts.append(
            "Must fix. Each of these means REVISE. List each one in your issues:\n"
            + "\n".join(f"- {f}" for f in found.must_fix)
        )
    if found.if_revising:
        parts.append(
            "Fix if you are returning REVISE anyway. Do not return REVISE for these "
            "alone:\n" + "\n".join(f"- {f}" for f in found.if_revising)
        )
    return "\n\n".join(parts)
