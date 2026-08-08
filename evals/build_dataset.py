"""Upload the Throughline golden dataset to LangSmith (idempotent).

Creates the ``throughline-golden`` dataset if it doesn't exist and populates it
from ``golden.golden_examples()``. Safe to re-run: if the dataset already has
examples it leaves them alone unless you pass ``--replace``.

Usage:
    uv run python -m evals.build_dataset            # create/populate if empty
    uv run python -m evals.build_dataset --replace  # clear and re-upload

Requires LANGSMITH_API_KEY in the environment (.env).
"""

from __future__ import annotations

import sys

from evals.golden import golden_examples

# Load .env (TAVILY/ANTHROPIC/LANGSMITH keys) before touching the LangSmith SDK.
from throughline import models  # noqa: F401  (import side effect: loads .env)

DATASET_NAME = "throughline-golden"
DATASET_DESCRIPTION = (
    "Throughline golden set: one frozen clean week plus planted-defect variants "
    "(unsupported claim, duplicate topic, off-voice, weak sources, citation mismatch)."
)


def main() -> None:
    import os

    if not os.getenv("LANGSMITH_API_KEY"):
        raise SystemExit(
            "LANGSMITH_API_KEY is not set. Add it to .env before uploading the dataset."
        )

    from langsmith import Client

    replace = "--replace" in sys.argv[1:]
    client = Client()

    if client.has_dataset(dataset_name=DATASET_NAME):
        dataset = client.read_dataset(dataset_name=DATASET_NAME)
        existing = list(client.list_examples(dataset_id=dataset.id))
        if existing and not replace:
            print(
                f"Dataset '{DATASET_NAME}' already has {len(existing)} examples "
                "— nothing to do (pass --replace to re-upload)."
            )
            return
        if existing and replace:
            client.delete_examples(example_ids=[e.id for e in existing])
            print(f"Cleared {len(existing)} existing examples from '{DATASET_NAME}'.")
    else:
        dataset = client.create_dataset(
            dataset_name=DATASET_NAME, description=DATASET_DESCRIPTION
        )
        print(f"Created dataset '{DATASET_NAME}'.")

    examples = golden_examples()
    client.create_examples(
        dataset_id=dataset.id,
        inputs=[e["inputs"] for e in examples],
        outputs=[e["outputs"] for e in examples],
        metadata=[e["metadata"] for e in examples],
    )
    names = ", ".join(e["metadata"]["name"] for e in examples)
    print(f"Uploaded {len(examples)} examples to '{DATASET_NAME}': {names}")


if __name__ == "__main__":
    main()
