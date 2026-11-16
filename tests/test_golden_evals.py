"""Offline golden-eval gate — the CI regression check for the evaluators.

Runs the deterministic, reference-free scorers from :mod:`evals.evaluators`
over the frozen golden set in :mod:`evals.golden` and enforces the contract the
set was designed around:

    * the clean report scores a perfect 1.0 on every evaluator, and
    * each planted-defect variant is CAUGHT (score < 1.0) by exactly the one
      evaluator that targets it, while every other evaluator stays clean (1.0).

That second half is what makes this a real regression gate: it fails not only
when an evaluator stops catching its defect, but also when an evaluator starts
firing on something clean (a false positive). It is fully offline — no LangSmith
call and no API keys — so it can gate every push and pull request.

The LLM voice judge (:func:`evals.evaluators.voice_judge`) is intentionally left
out: it needs a model call and is non-deterministic, so it is not part of the CI
backbone. The same goes for the three logic scorers. Offline, their planted
defects must trip no deterministic scorer. With THROUGHLINE_LIVE_EVALS=1 the
live test below holds them to the same contract with real model calls (about
$1 for the whole set).
"""

from __future__ import annotations

import os

import pytest

from evals.evaluators import (
    score_citation_integrity,
    score_dedup,
    score_groundedness,
    score_press_release_path,
    score_source_quality,
    score_source_recency,
    score_voice,
)
from evals.golden import golden_examples
from throughline import logic_check

# A perfect score. The clean report should hit this on every evaluator, and any
# non-target evaluator on a defect variant should too.
PASS = 1.0

# Maps each planted defect (the ``defect`` label in the golden outputs) to the
# evaluator that is supposed to catch it. Kept here, next to the assertions, so
# the one-defect-one-evaluator design is explicit and easy to extend.
DEFECT_TO_EVALUATOR = {
    "unsupported_claim": "groundedness",
    "duplicate_topic": "dedup",
    "off_voice": "voice",
    "weak_sources": "source_quality",
    "citation_mismatch": "citation_integrity",
    "press_release_path": "press_release_path",
    "stale_source": "source_recency",
    "unsourced_quote": "groundedness",
    "wrong_derived_number": "logic_numbers",
    "contradiction": "logic_contradictions",
    "overclaiming_heading": "logic_headings",
}

# The logic scorers and the kind of logic finding each one scores.
LOGIC_EVALUATORS: dict[str, logic_check.Kind] = {
    "logic_numbers": "derived_figure",
    "logic_contradictions": "contradiction",
    "logic_headings": "heading",
}


def _score_all(report: str, coverage: str) -> dict[str, tuple[float, str]]:
    """Run every deterministic evaluator and return {name: (score, comment)}."""
    return {
        "source_quality": score_source_quality(report),
        "press_release_path": score_press_release_path(report),
        "source_recency": score_source_recency(report),
        "groundedness": score_groundedness(report),
        "citation_integrity": score_citation_integrity(report),
        "dedup": score_dedup(report, coverage),
        "voice": score_voice(report),
    }


def _by_name() -> dict[str, dict]:
    return {ex["metadata"]["name"]: ex for ex in golden_examples()}


_EXAMPLES = _by_name()
_CLEAN = next(
    ex for ex in _EXAMPLES.values() if ex["outputs"]["label"] == "clean"
)
_DEFECTS = [
    ex for ex in _EXAMPLES.values() if ex["outputs"]["label"] == "defective"
]
_DETERMINISTIC = sorted(set(DEFECT_TO_EVALUATOR.values()) - set(LOGIC_EVALUATORS))


def test_defect_coverage_is_complete() -> None:
    """Every mapped defect is present in the golden set, and vice versa."""
    planted = {ex["outputs"]["defect"] for ex in _DEFECTS}
    assert planted == set(DEFECT_TO_EVALUATOR), (
        "golden set defects and DEFECT_TO_EVALUATOR have drifted apart: "
        f"golden={sorted(planted)} map={sorted(DEFECT_TO_EVALUATOR)}"
    )


@pytest.mark.parametrize("evaluator", _DETERMINISTIC)
def test_clean_report_scores_perfectly(evaluator: str) -> None:
    """The clean reference report trips no evaluator (no false positives)."""
    inputs = _CLEAN["inputs"]
    score, comment = _score_all(inputs["report"], inputs["coverage"])[evaluator]
    assert score == PASS, f"clean report scored {score} on {evaluator}: {comment}"


@pytest.mark.parametrize(
    "example",
    _DEFECTS,
    ids=[ex["metadata"]["name"] for ex in _DEFECTS],
)
def test_defect_is_caught_by_its_evaluator(example: dict) -> None:
    """The mapped evaluator catches the defect; all others stay clean.

    A logic defect's target needs a model call, so offline this only checks
    that no deterministic scorer fires on it.
    """
    defect = example["outputs"]["defect"]
    target = DEFECT_TO_EVALUATOR[defect]
    inputs = example["inputs"]
    scores = _score_all(inputs["report"], inputs["coverage"])

    if target in scores:
        target_score, target_comment = scores[target]
        assert target_score < PASS, (
            f"{target} failed to catch planted '{defect}' defect "
            f"(scored {target_score}): {target_comment}"
        )

    for name, (score, comment) in scores.items():
        if name == target:
            continue
        assert score == PASS, (
            f"{name} fired on the '{defect}' variant (scored {score}: {comment}) "
            f"but only {target} should — a collateral false positive"
        )


@pytest.mark.skipif(
    os.environ.get("THROUGHLINE_LIVE_EVALS") != "1",
    reason="makes model calls; set THROUGHLINE_LIVE_EVALS=1 to run",
)
@pytest.mark.parametrize(
    "example",
    [_CLEAN, *_DEFECTS],
    ids=[ex["metadata"]["name"] for ex in [_CLEAN, *_DEFECTS]],
)
def test_logic_scorers_live(example: dict) -> None:
    """Each logic scorer fires on its own planted defect and on nothing else."""
    report = example["inputs"]["report"]
    target = DEFECT_TO_EVALUATOR.get(example["outputs"]["defect"])
    findings = logic_check.check(report)
    for name, kind in LOGIC_EVALUATORS.items():
        score, comment = logic_check.score(findings, kind)
        if name == target:
            assert score < PASS, f"{name} missed the planted defect: {comment}"
        else:
            assert score == PASS, f"{name} fired on {example['metadata']['name']}: {comment}"
