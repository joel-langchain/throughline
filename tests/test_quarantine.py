"""The source archive must be the tool's output, not the researcher's retelling.

In production the researcher condensed every search result into its own prose
before saving it, so the citation-verifier was checking claims against a
paraphrase of the evidence. These tests pin the middleware that now archives each
search in code: the exact tool message, one file per call so concurrent searches
cannot overwrite each other, and never at the cost of failing the search.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from throughline.agent import topic_researcher
from throughline.quarantine import (
    FAILURES_DIR,
    SOURCES_DIR,
    UNFILED,
    QuarantineSourcesMiddleware,
    archive_entry,
    sources_path,
)
from throughline.tools import internet_search

RAW = '{"query": "agent evals", "results": [{"url": "https://example.org/a", "content": "Exact text."}]}'


def _request(name: str = "internet_search", **args) -> SimpleNamespace:
    call_id = args.pop("_id", "toolu_abc123")
    return SimpleNamespace(tool=None, tool_call={"name": name, "id": call_id, "args": args})


def _middleware() -> QuarantineSourcesMiddleware:
    return QuarantineSourcesMiddleware()


# --- paths ------------------------------------------------------------------


def test_path_is_per_call_so_concurrent_searches_cannot_collide() -> None:
    a = sources_path("ai-agents", "toolu_aaa", "first query")
    b = sources_path("ai-agents", "toolu_bbb", "first query")
    assert a != b, "same query in two concurrent calls must not share a path"
    assert a.startswith("/research/ai-agents/sources/")
    assert b.startswith("/research/ai-agents/sources/")


@pytest.mark.parametrize(
    "given",
    ["ai-agents", "/research/ai-agents", "research/ai-agents/", "/research/ai-agents/sources.md"],
)
def test_folder_forms_a_model_actually_passes_all_normalise(given: str) -> None:
    assert sources_path(given, "id", "q").startswith("/research/ai-agents/sources/")


@pytest.mark.parametrize("given", ["", None, "   ", "/", "../../etc"])
def test_unusable_folder_is_filed_not_escaped(given: object) -> None:
    path = sources_path(given, "id", "q")
    assert path.startswith(f"/research/{UNFILED}/sources/")
    assert ".." not in path


def test_path_stays_readable_for_a_human_browsing_the_folder() -> None:
    path = sources_path("ai-agents", "toolu_abc123", "Anthropic agent evals 2026")
    assert "anthropic-agent-evals-2026" in path
    assert path.endswith(".md")


# --- the archive is verbatim ------------------------------------------------


def test_archives_the_tool_message_byte_for_byte() -> None:
    result = ToolMessage(content=RAW, tool_call_id="toolu_abc123", name="internet_search")
    out = _middleware().wrap_tool_call(
        _request(query="agent evals", research_folder="ai-agents"), lambda _r: result
    )

    assert isinstance(out, Command)
    files = out.update["files"]
    (path, data), = files.items()
    assert path == sources_path("ai-agents", "toolu_abc123", "agent evals")
    # The exact string the model was handed appears untouched in the archive.
    assert RAW in data["content"]
    assert data["content"] == archive_entry("agent evals", RAW)
    assert data["encoding"] == "utf-8"


def test_the_model_still_receives_the_unchanged_tool_message() -> None:
    result = ToolMessage(content=RAW, tool_call_id="toolu_abc123", name="internet_search")
    out = _middleware().wrap_tool_call(
        _request(query="q", research_folder="ai-agents"), lambda _r: result
    )
    assert out.update["messages"] == [result], "archiving must not alter what the model sees"


def test_two_concurrent_searches_both_survive() -> None:
    mw = _middleware()
    updates = {}
    for call_id, query in (("toolu_one", "first"), ("toolu_two", "second")):
        msg = ToolMessage(content=f"body-{query}", tool_call_id=call_id, name="internet_search")
        out = mw.wrap_tool_call(
            _request(_id=call_id, query=query, research_folder="ai-agents"), lambda _r, m=msg: m
        )
        updates.update(out.update["files"])

    # The files channel merges by key; distinct keys mean nothing is lost.
    assert len(updates) == 2
    bodies = "".join(d["content"] for d in updates.values())
    assert "body-first" in bodies and "body-second" in bodies


def test_follow_up_searches_add_to_the_archive_rather_than_replacing_it() -> None:
    mw = _middleware()
    first = mw.wrap_tool_call(
        _request(_id="toolu_pass1", query="original", research_folder="ai-agents"),
        lambda _r: ToolMessage(content="pass one", tool_call_id="toolu_pass1", name="internet_search"),
    )
    second = mw.wrap_tool_call(
        _request(_id="toolu_pass2", query="gap closing", research_folder="ai-agents"),
        lambda _r: ToolMessage(content="pass two", tool_call_id="toolu_pass2", name="internet_search"),
    )
    assert set(first.update["files"]).isdisjoint(second.update["files"])


# --- it never costs a search ------------------------------------------------


def test_a_failed_search_is_filed_apart_from_the_evidence() -> None:
    # A failure is not a source, so it must not reach the verifier's folder. It
    # is still archived, because a researcher's tool calls are invisible from the
    # editor's state: without this, a run could lose half its searches silently.
    failed = ToolMessage(
        content="search failed", tool_call_id="toolu_abc123", name="internet_search", status="error"
    )
    out = _middleware().wrap_tool_call(
        _request(query="q", research_folder="ai-agents"), lambda _r: failed
    )

    (path,) = out.update["files"]
    assert f"/{FAILURES_DIR}/" in path, "a failed search belongs in the failures folder"
    assert f"/{SOURCES_DIR}/" not in path, "a failure must never look like evidence"
    assert out.update["messages"] == [failed], "the model still sees the real result"


def test_a_search_that_returns_an_error_payload_is_also_filed_as_a_failure() -> None:
    # internet_search returns an error dict rather than raising, so the failure
    # arrives as a normal tool message carrying an "error" field.
    errored = ToolMessage(
        content='{"query": "q", "results": [], "error": "search failed (ConnectionError)"}',
        tool_call_id="toolu_abc123",
        name="internet_search",
    )
    out = _middleware().wrap_tool_call(
        _request(query="q", research_folder="ai-agents"), lambda _r: errored
    )
    (path,) = out.update["files"]
    assert f"/{FAILURES_DIR}/" in path


def test_other_tools_are_left_alone() -> None:
    msg = ToolMessage(content="ok", tool_call_id="toolu_abc123", name="write_file")
    out = _middleware().wrap_tool_call(_request(name="write_file"), lambda _r: msg)
    assert out is msg


def test_a_command_result_from_another_middleware_is_passed_through() -> None:
    command = Command(update={"messages": []})
    out = _middleware().wrap_tool_call(
        _request(query="q", research_folder="ai-agents"), lambda _r: command
    )
    assert out is command


def test_async_path_archives_the_same_way() -> None:
    result = ToolMessage(content=RAW, tool_call_id="toolu_abc123", name="internet_search")

    async def handler(_request):
        return result

    out = asyncio.run(
        _middleware().awrap_tool_call(
            _request(query="agent evals", research_folder="ai-agents"), handler
        )
    )
    assert isinstance(out, Command)
    assert RAW in next(iter(out.update["files"].values()))["content"]


# --- wiring -----------------------------------------------------------------


def test_the_researcher_carries_the_quarantine_middleware() -> None:
    assert any(
        isinstance(m, QuarantineSourcesMiddleware)
        for m in topic_researcher.get("middleware", [])
    ), "the researcher must archive its own searches"


def test_the_search_tool_takes_a_research_folder() -> None:
    schema = internet_search.args_schema.model_json_schema()
    assert "research_folder" in schema["properties"]
