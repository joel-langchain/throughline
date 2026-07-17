"""Search tools backed by Tavily.

`scan_ai_week` casts a wide net so topics can *emerge* from what people are
actually writing about this week. `internet_search` is the researchers' tool for
going deep on a single topic.
"""

import os

from langchain_core.tools import tool
from tavily import TavilyClient

_api_key = os.environ.get("TAVILY_API_KEY")
if not _api_key:
    raise RuntimeError(
        "TAVILY_API_KEY is required. Copy .env.example to .env and fill it in."
    )

_tavily = TavilyClient(api_key=_api_key)

# Broad seeds used only for discovery/clustering. These are deliberately generic
# so the topics come from the conversation, not a fixed editorial list.
_SCAN_SEEDS = [
    "artificial intelligence",
    "large language models",
    "AI model release",
    "AI research paper",
    "AI agents",
    "AI policy regulation",
    "AI infrastructure compute",
]


@tool
def scan_ai_week(extra_query: str = "") -> str:
    """Scan the week's AI writing to discover what people are talking about.

    Runs several broad recent-news searches and returns a deduplicated list of
    headlines with URLs and snippets. Use this ONCE up front to cluster the week
    into topics that emerge from the data. Optionally pass `extra_query` to probe
    a specific angle. Does not go deep — that is the researchers' job.
    """
    seeds = list(_SCAN_SEEDS)
    if extra_query.strip():
        seeds.append(extra_query.strip())

    seen: set[str] = set()
    lines: list[str] = []
    for seed in seeds:
        try:
            res = _tavily.search(seed, max_results=6, topic="news", days=7)
        except Exception as exc:  # keep scanning even if one seed fails
            lines.append(f"[search failed for '{seed}': {exc}]")
            continue
        for item in res.get("results", []):
            url = item.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            title = item.get("title", "").strip()
            snippet = (item.get("content", "") or "").strip().replace("\n", " ")
            lines.append(f"- {title}\n  {url}\n  {snippet[:280]}")

    if not lines:
        return "No results found. Try again or pass a different extra_query."
    return f"Found {len(seen)} recent items across the week:\n\n" + "\n".join(lines)


@tool
def internet_search(query: str, max_results: int = 8) -> dict:
    """Search recent news for one topic in depth.

    Returns raw Tavily results (titles, URLs, content). Prefer independent,
    reputable sources — researchers, analysts, primary papers — over vendor
    marketing blogs.
    """
    return _tavily.search(query, max_results=max_results, topic="news", days=7)
