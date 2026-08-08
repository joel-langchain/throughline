"""Load source-legitimacy config from sources.toml.

The allow/deny domain lists and discovery seeds used to be hard-coded in
``tools.py``. They now live in ``sources.toml`` at the project root so the trust
model can be tuned without editing code. This module reads that file once at
import time and exposes the parsed lists.
"""

import tomllib
from pathlib import Path

# sources.toml sits at the project root (two levels up from this package file),
# alongside pyproject.toml — the same anchor run.py uses for ROOT.
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "sources.toml"


def _load() -> dict:
    if not _CONFIG_PATH.is_file():
        raise RuntimeError(
            f"Source config not found at {_CONFIG_PATH}. Expected sources.toml at "
            "the project root (allow/deny domain lists and scan seeds)."
        )
    with open(_CONFIG_PATH, "rb") as fh:
        return tomllib.load(fh)


_cfg = _load()

# Press-release / wire domains: dropped before any researcher sees them.
PRESS_RELEASE_DOMAINS = frozenset(_cfg.get("deny", []))
# Reputable domains: tagged so the model gets a deterministic quality signal.
REPUTABLE_DOMAINS = frozenset(_cfg.get("allow", []))
# Broad discovery seeds for scan_ai_week.
SCAN_SEEDS = list(_cfg.get("scan_seeds", []))
