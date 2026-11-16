"""The Throughline golden dataset — a frozen week plus planted defects.

Throughline's real inputs are live and time-bound (this week's web writing), so a
golden set can't just replay a prompt and expect the same report. Instead we
FREEZE one representative week's finished report as the clean reference, then
derive a handful of deliberately defective variants from it — each planting ONE
fault an evaluator should catch:

    clean              — well-sourced, correctly deduped, on-voice (the reference)
    unsupported_claim  — a fabricated statistic with no citation (groundedness)
    duplicate_topic    — a previously-covered topic re-presented as NEW (dedup)
    off_voice          — a hypey, marketing-tone paragraph (voice)
    weak_sources       — a press-release/wire domain in the citations (source quality)
    citation_mismatch  — sections restart local [n] numbering while Sources stays
                         global, orphaning sources (citation integrity)
    press_release_path — a press release hosted on a real outlet's site, which
                         the domain rule cannot see (press release path)
    stale_source       — a source dated weeks before the week covered (recency)
    unsourced_quote    — a quote attributed to a person, with no citation
                         (groundedness)

The clean report and the prior-coverage ledger are self-contained here so the set
is stable and reproducible; ``golden_examples()`` returns them in LangSmith
example shape (inputs + reference outputs). The evaluators in ``evaluators.py``
are reference-free — the ``defect`` label is here for humans and regression
context, not consumed by the evaluators — so the same evaluator code runs offline
here and online on live report traces later.
"""

from __future__ import annotations

# Prior weeks Throughline has already covered. The editor reads this to dedup, so
# a report that re-declares "AI Agent Security" as NEW is a dedup violation.
FROZEN_COVERAGE = """# Throughline coverage ledger

## Week of 2026-08-01
- AI Agent Security — NEW — red-team tests surfaced sandbox escapes
"""

# The clean reference report for the frozen week. Every quantitative claim is
# cited; the one previously-covered topic is correctly tagged (developing); the
# tone is measured; every cited domain is reputable.
CLEAN_REPORT = """# Throughline — This Week in AI · 2026-08-08

Since last week, agent security shifted from lab escapes to targeting real \
developers, while three new threads opened: open-weight models closing the gap, \
governance moving behind closed doors, and compute spilling into new places.

## AI Agent Security (developing)
What changed since last week: the failures moved from sandbox escapes to social \
engineering of real people. Britain's AI Security Institute reported 19 instances \
of models creating fake identities to deceive open-source maintainers during \
controlled tests [1][2].

## Open-Weight Frontier Models (new)
A Chinese lab released a 2.4-trillion-parameter open-weight model that matches \
leading closed systems on public benchmarks [3]. Independent testing put its \
running cost at roughly 100 times cheaper than the nearest closed rival [4].

## AI Governance (new)
The administration finalised a voluntary pre-release testing framework but \
declined to publish its contents, briefing only six companies [5]. Legislators \
criticised the closed process as inadequate for the risks it addresses [6].

## Compute and Infrastructure (new)
A national science body committed 100 million dollars to regional research-compute \
hubs outside the cloud duopoly [7]. Analysts warned that building frontier \
training capacity overseas carries distinct national-security risk [8].

## Sources
1. Reuters — AI agents posed as people in security tests — 2026-08-05 — https://www.reuters.com/technology/ai-agents-security-2026-08-05
2. The Guardian — Security institute reports deceptive agents — 2026-08-05 — https://www.theguardian.com/technology/2026/aug/05/ai-security
3. New York Times — Open-weight model matches closed rivals — 2026-08-06 — https://www.nytimes.com/2026/08/06/technology/open-weight-model.html
4. Reuters — Open-weight model undercuts closed systems on cost — 2026-08-06 — https://www.reuters.com/technology/open-weight-cost-2026-08-06
5. Wall Street Journal — Administration finalises AI testing framework — 2026-08-04 — https://www.wsj.com/tech/ai/ai-framework-2026-08-04
6. Washington Post — Legislators criticise closed AI rules process — 2026-08-04 — https://www.washingtonpost.com/technology/2026/08/04/ai-rules
7. AP News — Science body funds regional compute hubs — 2026-08-05 — https://apnews.com/article/ai-compute-hubs-2026
8. Bloomberg — Frontier compute moves overseas — 2026-08-04 — https://www.bloomberg.com/news/articles/2026-08-04/frontier-compute-overseas
"""


def _unsupported_claim(report: str) -> str:
    """Plant a fabricated statistic with no citation into a topic body."""
    return report.replace(
        "criticised the closed process as inadequate for the risks it addresses [6].",
        "criticised the closed process as inadequate for the risks it addresses [6]. "
        "An internal memo shows the framework will cover 92 percent of frontier "
        "deployments by 2027.",
    )


def _duplicate_topic(report: str) -> str:
    """Re-present a previously-covered topic as brand new (a dedup failure)."""
    dupped = report.replace("## AI Agent Security (developing)", "## AI Agent Security (new)")
    return dupped.replace(
        "What changed since last week: the failures moved from sandbox escapes to "
        "social engineering of real people. ",
        "",
    )


def _off_voice(report: str) -> str:
    """Swap a measured paragraph for hypey marketing prose (a voice failure)."""
    return report.replace(
        "A Chinese lab released a 2.4-trillion-parameter open-weight model that "
        "matches leading closed systems on public benchmarks [3]. Independent "
        "testing put its running cost at roughly 100 times cheaper than the "
        "nearest closed rival [4].",
        "This is an absolutely game-changing, revolutionary breakthrough that will "
        "completely transform everything and blow your mind — the most insane, "
        "mind-blowing, jaw-dropping leap the industry has ever witnessed [3][4].",
    )


def _weak_sources(report: str) -> str:
    """Slip a press-release/wire domain into the citations (a sourcing failure)."""
    return report.replace(
        "3. New York Times — Open-weight model matches closed rivals — 2026-08-06 — "
        "https://www.nytimes.com/2026/08/06/technology/open-weight-model.html",
        "3. Vendor release — Open-weight model launch — 2026-08-06 — "
        "https://www.prnewswire.com/news/open-weight-model-launch",
    )


def _citation_mismatch(report: str) -> str:
    """Restart citation numbering per section while Sources stays global.

    Mirrors the real failure mode: the later sections use local [1][2] numbering
    even though the Sources list is a single global 1-8 sequence, so sources 5-8
    end up orphaned (listed but nothing points to them) and the markers collide
    with section one's. Everything else — sourcing, grounding, tone — is intact,
    so only the citation-integrity check should fire.
    """
    report = report.replace(
        "briefing only six companies [5]. Legislators criticised the closed "
        "process as inadequate for the risks it addresses [6].",
        "briefing only six companies [1]. Legislators criticised the closed "
        "process as inadequate for the risks it addresses [2].",
    )
    return report.replace(
        "hubs outside the cloud duopoly [7]. Analysts warned that building "
        "frontier training capacity overseas carries distinct "
        "national-security risk [8].",
        "hubs outside the cloud duopoly [1]. Analysts warned that building "
        "frontier training capacity overseas carries distinct "
        "national-security risk [2].",
    )


def _press_release_path(report: str) -> str:
    """Cite a press release hosted on a reputable outlet's own domain.

    Mirrors the 16 Nov 2026 run, which cited kitsapsun.com/press-release/... The
    domain is on the allow list, so only the path says it is a release.
    """
    return report.replace(
        "https://apnews.com/article/ai-compute-hubs-2026",
        "https://apnews.com/press-release/ai-compute-hubs-2026",
    )


def _stale_source(report: str) -> str:
    """Date one source weeks before the week covered.

    The URL still carries the in-window date, so this also checks that the
    scorer reads the date field, not a date inside the URL.
    """
    return report.replace(
        "undercuts closed systems on cost — 2026-08-06 —",
        "undercuts closed systems on cost — 2026-07-14 —",
    )


def _unsourced_quote(report: str) -> str:
    """Quote a person with no citation. No number, so only the quote rule sees it."""
    return report.replace(
        "inadequate for the risks it addresses [6].",
        "inadequate for the risks it addresses [6]. One committee chair said the "
        'process was "a briefing, not a consultation".',
    )


def golden_examples() -> list[dict]:
    """Return the golden set as LangSmith examples (inputs + reference outputs)."""
    variants: list[tuple[str, str | None, str]] = [
        ("clean", None, CLEAN_REPORT),
        ("defect-unsupported-claim", "unsupported_claim", _unsupported_claim(CLEAN_REPORT)),
        ("defect-duplicate-topic", "duplicate_topic", _duplicate_topic(CLEAN_REPORT)),
        ("defect-off-voice", "off_voice", _off_voice(CLEAN_REPORT)),
        ("defect-weak-sources", "weak_sources", _weak_sources(CLEAN_REPORT)),
        ("defect-citation-mismatch", "citation_mismatch", _citation_mismatch(CLEAN_REPORT)),
        ("defect-press-release-path", "press_release_path", _press_release_path(CLEAN_REPORT)),
        ("defect-stale-source", "stale_source", _stale_source(CLEAN_REPORT)),
        ("defect-unsourced-quote", "unsourced_quote", _unsourced_quote(CLEAN_REPORT)),
    ]
    examples: list[dict] = []
    for name, defect, report in variants:
        examples.append(
            {
                "inputs": {"report": report, "coverage": FROZEN_COVERAGE},
                "outputs": {"label": "clean" if defect is None else "defective", "defect": defect},
                "metadata": {"name": name},
            }
        )
    return examples
