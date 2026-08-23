"""Model configuration.

Swap models by editing `WORKER_MODEL` / `EDITOR_MODEL` below — plain Anthropic
model names. `model` is the cheap, fast worker (researchers); `strong_model` is the
stronger reasoner (the editor that clusters and synthesises).

Model calls route through the LangSmith **LLM Gateway** when `ANTHROPIC_BASE_URL`
is set: one LangSmith key powers both tracing and (via the gateway) the model, so
no separate provider key is needed, and the gateway accepts the bare model names
below via its `/anthropic` route. With no gateway configured it calls Anthropic
directly with `ANTHROPIC_API_KEY`.
"""

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=True)

ModelName = Literal["claude-haiku-4-5", "claude-sonnet-4-6"]

# --- Swap models here -------------------------------------------------------
# Cheap worker for the many parallel research subagents.
WORKER_MODEL: ModelName = "claude-haiku-4-5"
# Stronger reasoner for clustering topics and synthesising the final report.
EDITOR_MODEL: ModelName = "claude-sonnet-4-6"


def _gateway_kwargs() -> dict[str, str]:
    """Resolve credentials/endpoint for the model client.

    Authenticate with the single LangSmith key — LangSmith Deployment injects
    `LANGSMITH_API_KEY` at run time, so it's readable here; `ANTHROPIC_API_KEY` and
    `LANGSMITH_API_KEY_GATEWAY` are optional overrides. When `ANTHROPIC_BASE_URL`
    is set, calls route through the gateway; otherwise they hit Anthropic directly.
    """
    key = (
        os.environ.get("LANGSMITH_API_KEY_GATEWAY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("LANGSMITH_API_KEY")
    )
    kwargs: dict[str, str] = {}
    if key:
        kwargs["api_key"] = key
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return kwargs


def build_model(model_name: ModelName):
    """Build a chat model, routing through the LangSmith gateway when configured."""
    return init_chat_model(f"anthropic:{model_name}", **_gateway_kwargs())


model = build_model(WORKER_MODEL)
strong_model = build_model(EDITOR_MODEL)
