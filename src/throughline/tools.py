"""Search tools backed by Tavily.

`scan_ai_week` casts a wide net so topics can *emerge* from what people are
actually writing about this week. `internet_search` is the researchers' tool for
going deep on a single topic.

Both tools are built to degrade, not crash. A search is a network call to a
third party, and a scheduled run has nobody watching it: one dropped connection
must cost a researcher one search result, not the week's report. So the Tavily
session retries transient transport failures itself, and neither tool lets an
exception escape into the graph — a failed search comes back as an empty result
with an `error` the model can read and act on.

Some failures are not transient, though. When Tavily refuses the plan (usage
limit reached) or the key, every later search in the run fails the same way.
Three scheduled runs ended like that with no report: the researchers did as told
and retried, dozens of times, and each run still finished "successfully". So the
first such refusal stops search for the rest of the run, in code, and the tools
say so in words the model is told to act on.
"""

import os
import re
from urllib.parse import urlparse

from langchain_core.tools import tool
from requests import Session
from requests.adapters import HTTPAdapter, Retry
from tavily import TavilyClient
from tavily.errors import ForbiddenError, InvalidAPIKeyError, MissingAPIKeyError

from throughline.config import (
    PRESS_RELEASE_DOMAINS as _PRESS_RELEASE_DOMAINS,
)
from throughline.config import (
    REPUTABLE_DOMAINS as _REPUTABLE_DOMAINS,
)
from throughline.config import (
    SCAN_SEEDS as _SCAN_SEEDS,
)

_tavily: TavilyClient | None = None

# Transport-level retries for the Tavily session. The client keeps a pooled
# keep-alive connection open between calls; in a long-lived deployment that
# connection can be closed by the far end while idle, and the next request then
# fails with "Remote end closed connection without response". `requests` does
# not retry at all by default, and urllib3's default retry policy excludes POST
# (which Tavily's search endpoint is), so that exact failure used to surface as
# a hard ConnectionError. This policy retries POSTs on connection/read errors and
# on 429/5xx, with a short backoff (first retry is immediate, then 1s, then 2s).
# `raise_on_status=False` hands a final non-2xx response back to the Tavily
# client so its own typed errors (invalid key, usage limit) still fire.
_RETRY_POLICY = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"POST"}),
    raise_on_status=False,
)


def _retrying_session() -> Session:
    """A requests session that retries transient failures against Tavily."""
    session = Session()
    adapter = HTTPAdapter(max_retries=_RETRY_POLICY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _client() -> TavilyClient:
    """Return the Tavily client, constructing it on first use.

    The key is checked lazily (at call time, not import time) so importing this
    module — and the agent/graph that depends on it — never requires a secret.
    Only actually running a search needs TAVILY_API_KEY. This keeps offline tests,
    `langgraph validate`, and graph construction working with no credentials.
    """
    global _tavily
    if _tavily is None:
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError(
                "TAVILY_API_KEY is required. Copy .env.example to .env and fill it in."
            )
        _tavily = TavilyClient(api_key=api_key, session=_retrying_session())
    return _tavily


# Refusals no retry can fix within a run. Tavily maps 403 and its plan-limit
# codes (432, 433) to ForbiddenError and 401 to InvalidAPIKeyError. A 429 is a
# rate limit, which the transport already retries, so it is not on this list.
_FATAL_SEARCH_ERRORS = (ForbiddenError, InvalidAPIKeyError, MissingAPIKeyError)

# Why search stopped in this run, or None while it works. Process state on
# purpose: a usage limit or a bad key belongs to the API key, not to one
# researcher, so the first refusal holds for every search after it. The editor
# clears it at the start of each run, so a plan or key fixed between runs is
# tried again.
_outage: str | None = None

_KEY_LIKE = re.compile(r"tvly-[A-Za-z0-9_-]+")


class SearchUnavailable(RuntimeError):
    """Search was refused for the rest of this run; retrying cannot help."""


def search_outage() -> str | None:
    """Why search stopped in this run, or None if it has not."""
    return _outage


def reset_search_outage() -> None:
    """Forget a previous run's outage, so this run tries search again."""
    global _outage
    _outage = None


def _describe(exc: Exception) -> str:
    """A short, key-free description of a provider refusal, safe to post to Slack."""
    return _KEY_LIKE.sub("tvly-…", f"{type(exc).__name__}: {exc}")[:200]


def _search(query: str, max_results: int) -> dict:
    """One recent-news search; the single place both tools call Tavily from."""
    global _outage
    if _outage is not None:
        raise SearchUnavailable(_outage)
    try:
        return _client().search(query, max_results=max_results, topic="news", days=7)
    except _FATAL_SEARCH_ERRORS as exc:
        _outage = _describe(exc)
        raise SearchUnavailable(_outage) from exc


def _unavailable(exc: SearchUnavailable) -> str:
    """The model-facing text for a search that will not work again this run."""
    return (
        f"search is unavailable for the rest of this run ({exc}). "
        "Do not retry: every search will fail the same way."
    )


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
            res = _search(seed, max_results=6)
        except SearchUnavailable as exc:
            # The researchers cannot search either, so a partial scan would only
            # send them off to fail. Stop the week here; the run reports it.
            return (
                f"STOP: {_unavailable(exc)} The researchers cannot search either, so "
                "do not delegate research and do not write a report. The run reports "
                "the outage itself."
            )
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
def internet_search(query: str, research_folder: str = "", max_results: int = 8) -> dict:
    """Search recent news for one topic in depth.

    Pass `research_folder` — the folder you were assigned, e.g. "ai-agents" or
    "/research/ai-agents". Every result is archived verbatim under that folder
    automatically, so you never need to copy search output into a file yourself.

    Returns raw Tavily results (titles, URLs, content). Press-release / wire
    domains are removed before returning, and each remaining result carries a
    `source_quality` of "reputable" or "unverified". Prefer independent,
    reputable sources — researchers, analysts, primary papers — over vendor
    marketing blogs.

    If the search itself fails (network or provider error), the result has an
    empty `results` list and an `error` field describing what went wrong. That
    is not a signal that nothing was published — run the search again, or try a
    rephrased query, before concluding a topic has no coverage. The exception is
    an error saying search is unavailable for the rest of this run: then stop
    searching, because every further search will fail the same way.
    """
    try:
        res = _search(query, max_results=max_results)
    except SearchUnavailable as exc:
        return {"query": query, "results": [], "error": _unavailable(exc)}
    except Exception as exc:  # a failed search is a result, never a crash
        return {
            "query": query,
            "results": [],
            "error": (
                f"search failed ({type(exc).__name__}: {exc}). "
                "Retry this query or try a rephrased one."
            ),
        }
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
