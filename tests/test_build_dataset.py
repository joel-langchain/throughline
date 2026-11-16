"""The golden dataset upload: the description and the in-place sync."""

from types import SimpleNamespace

from evals.build_dataset import DATASET_DESCRIPTION, sync_examples
from evals.golden import golden_examples


def _words(text: str) -> str:
    return " ".join(text.replace("-", " ").lower().split())


def test_description_names_every_planted_defect() -> None:
    description = _words(DATASET_DESCRIPTION)
    for example in golden_examples():
        name = example["metadata"]["name"]
        if name != "clean":
            assert _words(name.removeprefix("defect-")) in description, name


class FakeClient:
    def __init__(self):
        self.updates = None

    def update_examples(self, dataset_id, updates):
        self.updates = updates


def test_sync_updates_by_name_and_returns_the_new_ones() -> None:
    examples = golden_examples()
    existing = [
        SimpleNamespace(id="id-clean", metadata={"name": "clean"}),
        SimpleNamespace(id="id-gone", metadata={"name": "defect-retired"}),
    ]
    client = FakeClient()
    to_add = sync_examples(client, "dataset", existing, examples)

    # The clean week keeps its ID and gets this branch's inputs and outputs.
    assert [u["id"] for u in client.updates] == ["id-clean"]
    clean = next(e for e in examples if e["metadata"]["name"] == "clean")
    assert client.updates[0]["inputs"] == clean["inputs"]
    # Everything else is new; the retired example is left alone, not deleted.
    assert len(to_add) == len(examples) - 1
    assert "clean" not in {e["metadata"]["name"] for e in to_add}
