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

# Verification loop: how many times the editor may re-dispatch a researcher to
# close unsupported-citation gaps before it must give up and drop the claims.
# A hard cap on the verify -> re-research -> re-verify loop (bounds loop 2).
MAX_VERIFY_RETRIES = int(_cfg.get("max_verify_retries", 2))

# Conditional human review: risk signals that force a human to approve the report
# before it is published. A clean week (none of these) is auto-approved.
_review = _cfg.get("review", {})
# Sensitive domains where a human should always look, however well-sourced.
SENSITIVE_TERMS = [t.lower() for t in _review.get("sensitive_terms", [])]
# Phrases the editor uses when unsure — a proxy for low confidence / open gaps.
UNCERTAINTY_MARKERS = [m.lower() for m in _review.get("uncertainty_markers", [])]
# Minimum distinct cited sources before the report may publish unreviewed.
MIN_SOURCES = int(_review.get("min_sources", 0))
