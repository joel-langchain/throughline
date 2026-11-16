"""Scorer edge cases taken from real reports.

The golden set checks that each scorer catches its planted defect. These tests
pin the cases that a planted defect does not cover: the sentence splits, dates,
and URLs that real Monday reports produced and the scorers once got wrong.
"""

from __future__ import annotations

from datetime import date

from throughline.scoring import (
    score_groundedness,
    score_press_release_path,
    score_source_recency,
)


def _report(body: str, sources: str = "") -> str:
    return f"# Throughline — This Week in AI · 2026-11-16\n\n## Topic (new)\n{body}\n\n## Sources\n{sources}"


# --- groundedness: sentence splitting ---------------------------------------


def test_vs_does_not_split_a_cited_sentence() -> None:
    # 16 Nov 2026: the split after "vs." left the first half without its citation.
    body = (
        "On AutomationBench, Carbon outperforms Fable 5.2 (53.9% vs. 51.4%), "
        "a lead it holds even though it trails Fable overall [1][2]."
    )
    score, comment = score_groundedness(_report(body))
    assert score == 1.0, comment


def test_bill_numbers_and_titles_do_not_split_a_cited_sentence() -> None:
    # 9 Nov 2026: "H.R. 10538 / S. 5493" and "Rep." produced fragments scored as
    # uncited claims.
    body = (
        "The bill (H.R. 10538 / S. 5493), introduced separately by Rep. Obernolte, "
        "would require incident reporting within 72 hours [1]."
    )
    score, comment = score_groundedness(_report(body))
    assert score == 1.0, comment


def test_an_uncited_number_is_still_caught_after_an_abbreviation() -> None:
    body = "The ExploitGym score gap, 48.5% vs. 32.1%, is the clearest signal yet."
    score, comment = score_groundedness(_report(body))
    assert score == 0.0
    assert "48.5% vs. 32.1%" in comment


# --- groundedness: quotes ---------------------------------------------------


def test_an_attributed_quote_needs_a_citation() -> None:
    # 9 Nov 2026: no number in the sentence, so the old rule never saw it.
    body = 'Accenture denied the errors resulted from AI, attributing them to "human error".'
    assert score_groundedness(_report(body))[0] == 0.0


def test_a_cited_quote_passes() -> None:
    body = 'Anthropic said: "We called for pacing, not pausing" [1].'
    assert score_groundedness(_report(body))[0] == 1.0


def test_a_term_in_quotation_marks_is_not_a_quote() -> None:
    # 2 Nov 2026: a term in quotes, with no speaker, is not a claim.
    body = 'Teams should treat "human in the loop" as a label that needs defining.'
    score, comment = score_groundedness(_report(body))
    assert score == 1.0, comment


# --- source_recency ---------------------------------------------------------


def test_sources_inside_the_week_pass() -> None:
    sources = (
        "1. CNBC — Release — 2026-11-09 — https://www.cnbc.com/a\n"
        "2. Reuters — IPO — 2026-11-16 — https://www.reuters.com/b\n"
    )
    score, comment = score_source_recency(_report("Text [1][2].", sources))
    assert score == 1.0, comment


def test_old_and_month_only_dates_count_against_recency() -> None:
    # 16 Nov 2026: IEEE (4 Nov) was last week's news; several dates had no day.
    sources = (
        "1. CNBC — Release — 2026-11-09 — https://www.cnbc.com/a\n"
        "2. CASRAI — IEEE retracts — 2026-11-04 — https://casrai.org/b\n"
        "3. Cato — New Congress — 2026-11 — https://www.cato.org/c\n"
        "4. Blog — No date — https://example.org/d\n"
    )
    score, comment = score_source_recency(_report("Text [1][2][3][4].", sources))
    assert score == 0.25
    assert "[2] 2026-11-04" in comment
    assert "no day in the date: [3]" in comment
    assert "undated: [4]" in comment


def test_a_date_inside_the_url_is_not_the_source_date() -> None:
    # Reuters URLs end in a date; the date field is what counts.
    sources = "1. Reuters — IPO — 2026-11 — https://www.reuters.com/ipo-2026-11-10\n"
    assert score_source_recency(_report("Text [1].", sources))[0] == 0.0


def test_a_future_date_is_outside_the_window() -> None:
    sources = "1. Blog — Post — 2026-11-20 — https://example.org/a\n"
    assert score_source_recency(_report("Text [1].", sources))[0] == 0.0


def test_an_explicit_run_date_overrides_the_title() -> None:
    sources = "1. Blog — Post — 2026-11-02 — https://example.org/a\n"
    report = _report("Text [1].", sources)
    assert score_source_recency(report)[0] == 0.0
    assert score_source_recency(report, run_date=date(2026, 11, 9))[0] == 1.0


# --- press_release_path -----------------------------------------------------


def test_a_press_release_on_a_newspaper_site_is_caught() -> None:
    # 16 Nov 2026, source 29.
    url = (
        "https://www.kitsapsun.com/press-release/story/826397/"
        "ai-standards-report-urges-runtime-controls-over-congressional-ai-pause"
    )
    score, comment = score_press_release_path(_report("Text [1].", f"1. Kitsap Sun — R — 2026-11 — {url}\n"))
    assert score == 0.0
    assert "kitsapsun.com/press-release/" in comment


def test_only_whole_path_segments_match() -> None:
    sources = (
        "1. Outlet — A — 2026-11-10 — https://example.com/press/ai-news\n"
        "2. Outlet — B — 2026-11-10 — https://example.com/news/press-releases-are-changing\n"
        "3. Outlet — C — 2026-11-10 — https://example.com/a?type=press-release\n"
    )
    score, comment = score_press_release_path(_report("Text [1][2][3].", sources))
    assert score == 1.0, comment
