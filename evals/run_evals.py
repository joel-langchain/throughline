"""Run the Throughline evaluators over the golden dataset.

Scores each frozen example with the reference-free evaluators and records an
experiment in LangSmith. The clean example should score ~1 across the board;
each planted-defect example should be caught by its matching evaluator.

Usage:
    uv run python -m evals.run_evals               # deterministic evaluators
    uv run python -m evals.run_evals --with-judge  # also run the LLM voice judge

Requires LANGSMITH_API_KEY in the environment (.env). The LLM judge additionally
needs ANTHROPIC_API_KEY (one cheap call per example).
"""

from __future__ import annotations

import os
import sys

from evals import evaluators
from evals.build_dataset import DATASET_NAME

# Load .env before touching the LangSmith / model SDKs.
from throughline import models  # noqa: F401  (import side effect: loads .env)


def as_report(inputs: dict) -> dict:
    """Evaluation target: surface the frozen report (and coverage) for scoring.

    The report is a static artifact, so the 'system under test' just returns it;
    the evaluators do the work. This is the regression-set pattern for scoring a
    frozen output rather than re-running the (expensive, non-deterministic) agent.
    """
    return {"report": inputs["report"], "coverage": inputs.get("coverage", "")}


def main() -> None:
    if not os.getenv("LANGSMITH_API_KEY"):
        raise SystemExit(
            "LANGSMITH_API_KEY is not set. Add it to .env before running evals."
        )

    from langsmith import evaluate

    chosen = [
        evaluators.source_quality,
        evaluators.groundedness,
        evaluators.dedup,
        evaluators.voice,
    ]
    if "--with-judge" in sys.argv[1:]:
        chosen.append(evaluators.voice_judge)

    results = evaluate(
        as_report,
        data=DATASET_NAME,
        evaluators=chosen,
        experiment_prefix="throughline-golden",
    )
    print(f"\nEvaluation complete. Experiment: {getattr(results, 'experiment_name', '?')}")


if __name__ == "__main__":
    main()
