"""Model configuration.

Default: Anthropic. `model` is the cheap, fast worker (researchers); `strong_model`
is the more capable reasoner (the editor that clusters and synthesises).
"""

from pathlib import Path

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=True)

# Cheap worker for the many parallel research subagents.
model = init_chat_model("anthropic:claude-haiku-4-5")

# Stronger reasoner for clustering topics and synthesising the final report.
strong_model = init_chat_model("anthropic:claude-sonnet-4-6")
