"""Model configuration.

Model IDs are config-driven so a deployment can point them anywhere without a
code change. `model` is the cheap, fast worker (researchers); `strong_model` is
the more capable reasoner (the editor that clusters and synthesises).

Defaults call Anthropic directly. To route through the LangSmith **LLM Gateway**
instead — one LangSmith key, any provider's model, centrally traced and governed
— set these in the environment (e.g. as deployment secrets):

    ANTHROPIC_BASE_URL        the gateway host, e.g. https://gateway.smith.langchain.com
    ANTHROPIC_API_KEY         your LangSmith API key (lsv2_...)
    THROUGHLINE_WORKER_MODEL  anthropic:anthropic/claude-haiku-4-5
    THROUGHLINE_EDITOR_MODEL  anthropic:anthropic/claude-sonnet-4-6

Each value is a LangChain "<client>:<model-id>" spec. The part before the colon
picks the client — `anthropic`, which reads ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY
from the environment — and the part after is the model id sent upstream. For the
gateway that's a provider-prefixed id such as `anthropic/claude-sonnet-4-6` (the
gateway can also front other providers, e.g. `openai/gpt-5.4-mini`).
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=True)

# Cheap worker for the many parallel research subagents.
WORKER_MODEL = os.getenv("THROUGHLINE_WORKER_MODEL", "anthropic:claude-haiku-4-5")
# Stronger reasoner for clustering topics and synthesising the final report.
EDITOR_MODEL = os.getenv("THROUGHLINE_EDITOR_MODEL", "anthropic:claude-sonnet-4-6")

model = init_chat_model(WORKER_MODEL)
strong_model = init_chat_model(EDITOR_MODEL)
