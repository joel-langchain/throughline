"""The final pass sees what the code checks see, before the report ships.

On 16 Nov 2026 the final-pass reviewer approved a report with an uncited
benchmark claim, a press release in its sources, and a source from the week
before. The scorers found all three, but only after the report was published.
These tests cover the findings the reviewer now gets on the draft.
"""

from __future__ import annotations

from throughline.agent import (
    REPORT_PATH,
    final_pass_findings_middleware,
    final_pass_reviewer,
    with_findings,
)
from throughline.prepublish import findings_block, report_findings

# A draft as the reviewer sees it: [[url]] markers, unnumbered Sources lines.
CLEAN_DRAFT = """# Throughline — This Week in AI · 2026-11-16

Since last week, two model launches and a policy shift.

## Model launches (developing)
What changed: the lab shipped two models at $8 per million input tokens \
[[https://www.cnbc.com/a]][[https://www.reuters.com/b]]. Its head of product said \
"we called for pacing, not pausing" [[https://www.cnbc.com/a]].

## Sources
CNBC — Lab ships models — 2026-11-09 — https://www.cnbc.com/a
Reuters — Lab unveils models — 2026-11-10 — https://www.reuters.com/b
"""

PRESS_URL = "https://www.kitsapsun.com/press-release/story/826397/ai-pause"

DIRTY_DRAFT = f"""# Throughline — This Week in AI · 2026-11-16

Since last week, two model launches and a policy shift.

## Model launches (developing)
What changed: the lab shipped two models at $8 per million input tokens \
[[https://www.cnbc.com/a]]. On the index the new model scores 65, ahead of its \
rival at 60. It is a revolutionary release.

## Policy (developing)
IEEE retracted about 1,400 papers [[https://casrai.org/c]]. A bill would pause \
development [[{PRESS_URL}]]. Analysts expect a vote [[https://www.cato.org/d]].

## Sources
CNBC — Lab ships models — 2026-11-09 — https://www.cnbc.com/a
CASRAI — IEEE retracts papers — 2026-11-04 — https://casrai.org/c
Kitsap Sun — Report urges controls — 2026-11-12 — {PRESS_URL}
Cato Institute — The new Congress — 2026-11 — https://www.cato.org/d
"""


def test_a_clean_draft_has_no_findings() -> None:
    found = report_findings(CLEAN_DRAFT)
    assert not found, (found.must_fix, found.if_revising)
    assert findings_block(found) == ""


def test_each_defect_in_the_draft_is_a_finding() -> None:
    found = report_findings(DIRTY_DRAFT)
    must = "\n".join(found.must_fix)
    assert "scores 65, ahead of its rival at 60" in must  # uncited figure
    assert PRESS_URL in must  # press release by path
    assert "revolutionary" in must  # hype
    assert "dated 2026-11-04" in must and "IEEE retracts papers" in must  # stale source
    later = "\n".join(found.if_revising)
    assert "The new Congress" in later  # month-only date is not a reason to revise


def test_findings_name_sources_by_line_not_by_number() -> None:
    # The reviewer sees unnumbered Sources lines, so "[2]" would mean nothing to it.
    block = findings_block(report_findings(DIRTY_DRAFT))
    assert "[2]" not in block
    assert "CASRAI — IEEE retracts papers — 2026-11-04" in block


def test_the_block_separates_must_fix_from_fix_if_revising() -> None:
    block = findings_block(report_findings(DIRTY_DRAFT))
    assert block.startswith("AUTOMATED CHECK FINDINGS")
    assert block.index("Must fix") < block.index("Fix if you are returning REVISE")


def test_the_draft_file_is_not_changed() -> None:
    draft = DIRTY_DRAFT
    report_findings(draft)
    assert "[[https://www.cnbc.com/a]]" in draft


# --- wiring into the reviewer ------------------------------------------------


def test_reviewer_instructions_carry_the_findings() -> None:
    files = {REPORT_PATH: {"content": DIRTY_DRAFT, "encoding": "utf-8"}}
    prompt = with_findings("Review the report.", files)
    assert prompt.startswith("Review the report.")
    assert "AUTOMATED CHECK FINDINGS" in prompt


def test_reviewer_instructions_are_unchanged_for_a_clean_or_missing_draft() -> None:
    clean = {REPORT_PATH: {"content": CLEAN_DRAFT, "encoding": "utf-8"}}
    assert with_findings("Review the report.", clean) == "Review the report."
    assert with_findings("Review the report.", None) == "Review the report."


def test_the_reviewer_runs_the_findings_middleware() -> None:
    assert final_pass_findings_middleware in final_pass_reviewer["middleware"]
