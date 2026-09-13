"""One failed network call must not kill the weekly run.

A scheduled run died when Tavily closed a pooled connection mid-request inside
one researcher's `internet_search`: the exception climbed out of the tool, out of
the subagent, through the editor's `task` call, and ended the run with no report.
These tests pin the three layers that now stand between that failure and the
report, all offline (no Tavily, no model):

1. the Tavily session retries POSTs on connection/read errors and 429/5xx;
2. `internet_search` returns an error result instead of raising;
3. the editor's `task` tool is retried once and then answered with an error
   message rather than an exception.
"""

from __future__ import annotations

from http.client import RemoteDisconnected
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from requests.adapters import Retry
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3.exceptions import ProtocolError

from throughline import tools
from throughline.agent import build_agent, task_failure_guard
from throughline.config import PRESS_RELEASE_DOMAINS, REPUTABLE_DOMAINS
from throughline.tools import internet_search, scan_ai_week

# The exact failure from the LangSmith trace: urllib3 wraps the dropped socket as
# a ProtocolError, which requests re-raises as a ConnectionError.
DROPPED_CONNECTION = ProtocolError(
    "Connection aborted.",
    RemoteDisconnected("Remote end closed connection without response"),
)


class _FakeTavily:
    """Stand-in for TavilyClient: raises for some queries, answers for the rest."""

    def __init__(self, results: list[dict] | None = None, fail_on: set[str] | None = None):
        self.results = results or []
        self.fail_on = fail_on or set()
        self.calls: list[str] = []

    def search(self, query: str, **_kwargs) -> dict:
        self.calls.append(query)
        if query in self.fail_on:
            raise RequestsConnectionError(DROPPED_CONNECTION)
        return {"query": query, "results": [dict(r) for r in self.results]}


@pytest.fixture
def fresh_client(monkeypatch):
    """Reset the module-level client so each test builds or injects its own."""
    monkeypatch.setattr(tools, "_tavily", None)
    yield
    monkeypatch.setattr(tools, "_tavily", None)


# --- 1. transport retries ---------------------------------------------------


def test_tavily_session_retries_a_dropped_post(fresh_client, monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    client = tools._client()

    adapter = client.session.get_adapter("https://api.tavily.com/search")
    retry = adapter.max_retries
    assert isinstance(retry, Retry)
    assert retry.total and retry.total >= 1
    assert "POST" in retry.allowed_methods, "Tavily's search endpoint is a POST"

    # The dropped-connection error is a *read* error to urllib3. With the policy
    # mounted, incrementing on it yields a fresh Retry (i.e. the request is
    # re-sent) instead of re-raising.
    after = retry.increment(method="POST", url="/search", error=DROPPED_CONNECTION)
    assert after.total == retry.total - 1

    # Rate limits and server errors are retried too, and a final non-2xx is
    # returned (not raised) so the Tavily client's own typed errors still fire.
    assert {429, 500, 502, 503, 504} <= set(retry.status_forcelist)
    assert retry.raise_on_status is False


def test_requests_default_policy_would_have_reraised() -> None:
    # Documents WHY the custom policy exists: requests' stock adapter policy
    # (Retry(0, read=False)) re-raises the very first read error on a POST.
    stock = Retry(0, read=False)
    with pytest.raises(ProtocolError):
        stock.increment(method="POST", url="/search", error=DROPPED_CONNECTION)


def test_client_still_requires_a_key_lazily(fresh_client, monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        tools._client()


# --- 2. the search tool degrades instead of raising -------------------------


def test_internet_search_returns_an_error_result_not_an_exception(
    fresh_client, monkeypatch
) -> None:
    fake = _FakeTavily(fail_on={"agent evals"})
    monkeypatch.setattr(tools, "_tavily", fake)

    out = internet_search.invoke({"query": "agent evals"})

    assert out["results"] == []
    assert out["query"] == "agent evals"
    assert "ConnectionError" in out["error"]
    assert "Retry" in out["error"], "the model is told to try again, not to give up"


def test_internet_search_filters_and_tags_when_the_search_succeeds(
    fresh_client, monkeypatch
) -> None:
    press = next(iter(PRESS_RELEASE_DOMAINS))
    reputable = next(iter(REPUTABLE_DOMAINS))
    fake = _FakeTavily(
        results=[
            {"url": f"https://www.{press}/release/1", "title": "wire copy"},
            {"url": f"https://{reputable}/story/2", "title": "reporting"},
            {"url": "https://example.org/blog/3", "title": "a blog"},
        ]
    )
    monkeypatch.setattr(tools, "_tavily", fake)

    out = internet_search.invoke({"query": "anything"})

    urls = [r["url"] for r in out["results"]]
    assert f"https://www.{press}/release/1" not in urls, "press-release domains are dropped"
    quality = {r["url"]: r["source_quality"] for r in out["results"]}
    assert quality[f"https://{reputable}/story/2"] == "reputable"
    assert quality["https://example.org/blog/3"] == "unverified"
    assert "error" not in out


def test_scan_ai_week_keeps_scanning_past_a_failed_seed(fresh_client, monkeypatch) -> None:
    seeds = ["seed one", "seed two", "seed three"]
    monkeypatch.setattr(tools, "_SCAN_SEEDS", seeds)
    fake = _FakeTavily(
        results=[{"url": "https://example.org/a", "title": "A", "content": "x"}],
        fail_on={"seed two"},
    )
    monkeypatch.setattr(tools, "_tavily", fake)

    out = scan_ai_week.invoke({"extra_query": ""})

    assert fake.calls == seeds, "a failing seed must not stop the later seeds"
    assert "search failed for 'seed two'" in out
    assert "https://example.org/a" in out


# --- 3. a failed delegation is retried once, then reported, never raised ------


def _task_request() -> SimpleNamespace:
    # Only the fields the retry middleware reads; no state/runtime needed.
    return SimpleNamespace(tool=None, tool_call={"name": "task", "id": "call_1"})


def test_task_guard_retries_once_then_returns_an_error_message(monkeypatch) -> None:
    monkeypatch.setattr("langchain.agents.middleware.tool_retry.time.sleep", lambda _s: None)
    attempts: list[int] = []

    def crashing_subagent(_request):
        attempts.append(1)
        raise RequestsConnectionError(DROPPED_CONNECTION)

    result = task_failure_guard.wrap_tool_call(_task_request(), crashing_subagent)

    assert len(attempts) == 2, "initial attempt + exactly one retry"
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call_1"
    assert result.name == "task"
    assert "failed twice" in result.content
    assert "ConnectionError" in result.content
    assert "Drop this topic" in result.content


def test_task_guard_passes_a_successful_delegation_through(monkeypatch) -> None:
    ok = ToolMessage(content="TOPIC: x\nVERDICT: KEEP", tool_call_id="call_1", name="task")
    result = task_failure_guard.wrap_tool_call(_task_request(), lambda _r: ok)
    assert result is ok


def test_task_guard_leaves_other_tools_alone() -> None:
    request = SimpleNamespace(tool=None, tool_call={"name": "write_file", "id": "call_2"})

    def raising_handler(_request):
        raise RuntimeError("write_file is not the guard's business")

    # No retry, no error-message conversion: the exception propagates as before
    # so the rest of the stack (e.g. the review interrupt) behaves unchanged.
    with pytest.raises(RuntimeError):
        task_failure_guard.wrap_tool_call(request, raising_handler)


def test_agent_builds_with_the_guard_installed() -> None:
    # The guard is a wrap_tool_call hook, not a node, so it leaves no trace in
    # the graph shape; what matters is that the editor still compiles with it.
    assert build_agent() is not None
