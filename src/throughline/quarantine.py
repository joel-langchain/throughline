"""Archive every search result verbatim, in code rather than by asking the model.

The citation-verifier's whole job is to check a claim against the source text the
researcher actually saw. That only works if the archive IS what the researcher
saw. Asking the model to paste its raw results into a file does not hold: in
production the researcher condensed every result into its own prose, so the
verifier was checking claims against a paraphrase of the evidence and calling it
verification.

So the host does it. This middleware sits on the researcher, watches every
``internet_search`` call, and writes the tool's own output — byte for byte, the
same string handed to the model — into the topic's research folder. The model is
no longer asked to archive anything and cannot forget, trim, or rewrite it.

Concurrency: a researcher usually fires several searches in ONE turn, and those
run concurrently. Appending to a single file would mean read-modify-write from
several coroutines against the same pre-call state, and the last write would win
and silently drop the others. Each search therefore gets its OWN file, keyed by
its tool-call id. The files channel is a delta channel that merges by key, so
concurrent writes to different keys all survive.
"""

from __future__ import annotations

import re

from deepagents.backends.utils import create_file_data
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command

# The tool whose output is quarantined, and where the archive lives.
SEARCH_TOOL = "internet_search"
RESEARCH_ROOT = "/research"
SOURCES_DIR = "sources"
# Failed searches are archived too, but in their own directory. The verifier
# reads SOURCES_DIR only, so a failure never pollutes the evidence; keeping them
# somewhere makes them countable from the finished run, which matters because a
# researcher's own tool calls are invisible from the editor's isolated state —
# a run can quietly lose half its searches and still look healthy from outside.
FAILURES_DIR = "failed-searches"
# Fallback folder for a search that arrives with no usable research folder, so a
# stray call is still archived somewhere the verifier can find it.
UNFILED = "unfiled"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_length: int = 40) -> str:
    """Lowercase, hyphenated, filesystem-safe fragment of `text` (may be empty)."""
    return _SLUG_STRIP.sub("-", text.lower()).strip("-")[:max_length].strip("-")


def _folder(raw: object) -> str:
    """Normalise a research_folder argument to a single path segment.

    Accepts what a model actually passes: "ai-agents", "/research/ai-agents",
    "/research/ai-agents/", even "research/ai-agents/sources.md". Anything
    unusable becomes UNFILED rather than escaping the research root.
    """
    text = raw if isinstance(raw, str) else ""
    text = text.strip().strip("/")
    if text.startswith("research/"):
        text = text[len("research/") :]
    # Keep the first segment only; a trailing file name or subdir is not a topic.
    segment = text.split("/")[0] if text else ""
    return slugify(segment) or UNFILED


def sources_path(
    research_folder: object,
    tool_call_id: object,
    query: object = "",
    *,
    directory: str = SOURCES_DIR,
) -> str:
    """Path for one search's archive: /research/<folder>/<directory>/<query>-<id>.md.

    The tool-call id makes the path unique per call, which is what keeps
    concurrent searches from overwriting each other. The query slug is only there
    so a human reading the folder can tell the files apart.
    """
    call_id = slugify(str(tool_call_id or ""), max_length=12) or "call"
    stem = slugify(str(query or ""), max_length=48)
    name = f"{stem}-{call_id}" if stem else call_id
    return f"{RESEARCH_ROOT}/{_folder(research_folder)}/{directory}/{name}.md"


def archive_entry(query: object, content: str) -> str:
    """The archived file body: the query, then the tool output exactly as returned."""
    return f"# Search: {query}\n\n{content}\n"


def _message_text(message: ToolMessage) -> str:
    """Tool-message content as text, whatever block form it arrives in."""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block if isinstance(block, str) else block.get("text", "")
            for block in content
            if isinstance(block, (str, dict))
        ]
        return "\n".join(p for p in parts if p)
    return str(content)


class QuarantineSourcesMiddleware(AgentMiddleware):
    """Write each `internet_search` result verbatim into the topic's sources folder.

    Wraps the tool call rather than replacing it: the model still gets the exact
    same tool message it would have got, and the archive is a side effect it
    cannot skip. A failure to archive never fails the search — the research still
    matters more than the bookkeeping — and the tool message is returned as-is.
    """

    def _archive(
        self, request, result: ToolMessage | Command
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != SEARCH_TOOL:
            return result
        # Only a plain tool message carries archivable text; a Command means
        # another middleware already took the result over, so leave it alone.
        if not isinstance(result, ToolMessage):
            return result

        args = request.tool_call.get("args") or {}
        text = _message_text(result)
        # A failed search is archived apart from the evidence: it is not a source,
        # but its existence is the signal that a run lost searches.
        failed = result.status == "error" or '"error":' in text
        path = sources_path(
            args.get("research_folder"),
            request.tool_call.get("id"),
            args.get("query"),
            directory=FAILURES_DIR if failed else SOURCES_DIR,
        )
        body = archive_entry(args.get("query", ""), text)
        return Command(
            update={
                "files": {path: create_file_data(body)},
                "messages": [result],
            }
        )

    def wrap_tool_call(self, request, handler):
        return self._archive(request, handler(request))

    async def awrap_tool_call(self, request, handler):
        return self._archive(request, await handler(request))
