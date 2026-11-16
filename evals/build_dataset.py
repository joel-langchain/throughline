"""Upload the Throughline golden dataset to LangSmith (idempotent).

Creates the ``throughline-golden`` dataset if it doesn't exist and populates it
from ``golden.golden_examples()``. Safe to re-run: if the dataset already has
examples it leaves them alone unless you pass ``--sync`` or ``--replace``.

Usage:
    uv run python -m evals.build_dataset            # create/populate if empty
    uv run python -m evals.build_dataset --sync     # update in place, add new ones
    uv run python -m evals.build_dataset --replace  # clear and re-upload

``--sync`` matches examples by their metadata name and updates them in place, so
they keep their IDs and past experiments still line up example by example.
``--replace`` gives every example a new ID. Both also set the description.

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
    "(unsupported claim, duplicate topic, off-voice, weak sources, citation mismatch, "
    "press-release path, stale source, unsourced quote, wrong derived number, "
    "contradiction, overclaiming heading)."
)


def sync_examples(client, dataset_id, existing: list, examples: list[dict]) -> list[dict]:
    """Update the examples already in the dataset by name; return the ones to add."""
    by_name = {(e.metadata or {}).get("name"): e for e in existing}
    updates = [
        {
            "id": by_name[e["metadata"]["name"]].id,
            "inputs": e["inputs"],
            "outputs": e["outputs"],
            "metadata": e["metadata"],
        }
        for e in examples
        if e["metadata"]["name"] in by_name
    ]
    if updates:
        client.update_examples(dataset_id=dataset_id, updates=updates)
    print(f"Updated {len(updates)} examples in place in '{DATASET_NAME}'.")
    stale = set(by_name) - {e["metadata"]["name"] for e in examples}
    if stale:
        print(f"Left {len(stale)} examples not in golden.py: {', '.join(sorted(map(str, stale)))}")
    return [e for e in examples if e["metadata"]["name"] not in by_name]


def main() -> None:
    import os

    if not os.getenv("LANGSMITH_API_KEY"):
        raise SystemExit(
            "LANGSMITH_API_KEY is not set. Add it to .env before uploading the dataset."
        )

    from langsmith import Client

    sync = "--sync" in sys.argv[1:]
    replace = "--replace" in sys.argv[1:]
    client = Client()
    examples = golden_examples()

    if client.has_dataset(dataset_name=DATASET_NAME):
        dataset = client.read_dataset(dataset_name=DATASET_NAME)
        existing = list(client.list_examples(dataset_id=dataset.id))
        if existing and not (sync or replace):
            print(
                f"Dataset '{DATASET_NAME}' already has {len(existing)} examples "
                "— nothing to do (pass --sync or --replace to re-upload)."
            )
            return
        # create_dataset sets the description only for a new dataset.
        client.request_with_retries(
            "PATCH", f"/datasets/{dataset.id}", json={"description": DATASET_DESCRIPTION}
        )
        if existing and sync:
            examples = sync_examples(client, dataset.id, existing, examples)
        elif existing and replace:
            client.delete_examples(example_ids=[e.id for e in existing])
            print(f"Cleared {len(existing)} existing examples from '{DATASET_NAME}'.")
    else:
        dataset = client.create_dataset(
            dataset_name=DATASET_NAME, description=DATASET_DESCRIPTION
        )
        print(f"Created dataset '{DATASET_NAME}'.")

    if examples:
        client.create_examples(
            dataset_id=dataset.id,
            inputs=[e["inputs"] for e in examples],
            outputs=[e["outputs"] for e in examples],
            metadata=[e["metadata"] for e in examples],
        )
    names = ", ".join(e["metadata"]["name"] for e in examples) or "none"
    print(f"Uploaded {len(examples)} new examples to '{DATASET_NAME}': {names}")


if __name__ == "__main__":
    main()
