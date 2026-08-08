"""Search tools backed by Tavily.

`scan_ai_week` casts a wide net so topics can *emerge* from what people are
actually writing about this week. `internet_search` is the researchers' tool for
going deep on a single topic.
"""

import os
from urllib.parse import urlparse

from langchain_core.tools import tool
from tavily import TavilyClient

from throughline.config import (
    PRESS_RELEASE_DOMAINS as _PRESS_RELEASE_DOMAINS,
)
from throughline.config import (
    REPUTABLE_DOMAINS as _REPUTABLE_DOMAINS,
)
from throughline.config import (
    SCAN_SEEDS as _SCAN_SEEDS,
)

_api_key = os.environ.get("TAVILY_API_KEY")
if not _api_key:
    raise RuntimeError(
        "TAVILY_API_KEY is required. Copy .env.example to .env and fill it in."
    )

_tavily = TavilyClient(api_key=_api_key)

# Legitimacy is enforced deterministically, not left to the model's judgement.
# The trust lists (deny/allow domains) and discovery seeds now live in
# sources.toml and are loaded via throughline.config, so they can be tuned
# without editing code. Press-release / wire domains are dropped outright so they
# never reach a researcher; reputable domains are tagged so the model gets a
# deterministic signal; everything else passes through as "unverified".


def _domain(url: str) -> str:
    """Return the registrable host for a URL, minus a leading 'www.'."""
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _domain_in(url: str, domains: frozenset[str]) -> bool:
    """True if the URL's host is one of `domains` (or a subdomain of one)."""
    host = _domain(url)
    return any(host == d or host.endswith("." + d) for d in domains)


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
            if _domain_in(url, _PRESS_RELEASE_DOMAINS):
                continue  # drop press-release / wire slop before it is seen
            seen.add(url)
            title = item.get("title", "").strip()
            snippet = (item.get("content", "") or "").strip().replace("\n", " ")
            tag = " [reputable]" if _domain_in(url, _REPUTABLE_DOMAINS) else ""
            lines.append(f"- {title}{tag}\n  {url}\n  {snippet[:280]}")

    if not lines:
        return "No results found. Try again or pass a different extra_query."
    return f"Found {len(seen)} recent items across the week:\n\n" + "\n".join(lines)


@tool
def internet_search(query: str, max_results: int = 8) -> dict:
    """Search recent news for one topic in depth.

    Returns raw Tavily results (titles, URLs, content). Press-release / wire
    domains are removed before returning, and each remaining result carries a
    `source_quality` of "reputable" or "unverified". Prefer independent,
    reputable sources — researchers, analysts, primary papers — over vendor
    marketing blogs.
    """
    res = _tavily.search(query, max_results=max_results, topic="news", days=7)
    kept = []
    for item in res.get("results", []):
        url = item.get("url", "")
        if _domain_in(url, _PRESS_RELEASE_DOMAINS):
            continue  # drop press-release / wire slop
        item["source_quality"] = (
            "reputable" if _domain_in(url, _REPUTABLE_DOMAINS) else "unverified"
        )
        kept.append(item)
    res["results"] = kept
    return res
