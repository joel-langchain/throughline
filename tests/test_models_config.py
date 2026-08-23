"""The model selection is config-driven — direct Anthropic by default, or any
gateway-routed model via environment, with no code change.

Each case imports ``throughline.models`` in a fresh subprocess so the module-level
env read is exercised cleanly (and no API key is needed — the clients construct
lazily).
"""

from __future__ import annotations

import os
import subprocess
import sys

_CODE = "from throughline.models import model, strong_model; print(model.model); print(strong_model.model)"


def _model_ids(overrides: dict[str, str]) -> tuple[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("THROUGHLINE_")}
    env.update(overrides)
    out = subprocess.run(
        [sys.executable, "-c", _CODE],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    worker, editor = out.stdout.strip().splitlines()[-2:]
    return worker, editor


def test_defaults_call_anthropic_directly() -> None:
    worker, editor = _model_ids({})
    assert worker == "claude-haiku-4-5"
    assert editor == "claude-sonnet-4-6"


def test_models_are_overridable_for_the_gateway() -> None:
    worker, editor = _model_ids(
        {
            "THROUGHLINE_WORKER_MODEL": "anthropic:anthropic/claude-haiku-4-5",
            # A different provider entirely, to prove any gateway model can be picked.
            "THROUGHLINE_EDITOR_MODEL": "anthropic:openai/gpt-5.4-mini",
        }
    )
    assert worker == "anthropic/claude-haiku-4-5"
    assert editor == "openai/gpt-5.4-mini"
