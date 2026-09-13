"""LangSmith evaluator adapters for Throughline reports.

The scoring itself lives in `throughline.scoring` — in the library, not here,
because the deployed agent scores its own production runs with the same
functions (see `throughline.monitoring`). Keeping one implementation is the point:
a `groundedness` score on the golden set and a `groundedness` score on Monday's
report mean exactly the same thing, so a CI regression and a production
regression are comparable.

This module is the harness layer: it adapts those scorers to LangSmith's
evaluator signature, and adds the one judge that needs a model call.

    source_quality     — cited domains are reputable, none are press-release/wire
    groundedness       — every quantitative claim carries an inline [n] citation
    citation_integrity — every [n] resolves 1:1 to a contiguous Sources list
    dedup              — no topic tagged NEW was already covered in a prior week
    voice              — measured tone, not marketing hype (hype-word density)
    voice_judge        — LLM-as-judge for the prose quality code can't grade

Each returns a LangSmith feedback dict {"key", "score", "comment"} and tolerates
being called with a RunTree (local evaluate()) or a plain dict (online).
"""

from __future__ import annotations

from throughline.scoring import (
    HYPE_TERMS,  # noqa: F401  (re-exported: tests and callers import it from here)
    score_citation_integrity,
    score_dedup,
    score_groundedness,
    score_source_quality,
    score_voice,
)


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


def citation_integrity(run, example=None) -> dict:
    report, _ = _extract(run, example)
    score, comment = score_citation_integrity(report)
    return {"key": "citation_integrity", "score": score, "comment": comment}


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
