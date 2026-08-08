"""Run the editor agent and save everything it produced.

The agent runs on the default (ephemeral) StateBackend, so its writes land in
agent state, not on your real disk — important because it processes untrusted
web-search text. This trusted host code reads files out of state after the run
and mirrors them to disk, with a path-traversal guard.

Cross-week memory lives under the agent path ``/memories/`` (the deepagents
convention). It is ephemeral in state like everything else, so this runner
persists it: memory files are seeded IN from ./memory before the run and mirrored
OUT to ./memory after, letting each week build on the last. When this agent is
deployed later, ``/memories/`` swaps to a persistent ``StoreBackend`` and this
host-side mirror goes away — the agent's paths and prompt stay identical.

Usage:
    uv run python -m throughline.run
"""

import uuid
from datetime import date
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from throughline.agent import build_agent, report_risk_signals

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "output"
MEM_DIR = ROOT / "memory"
# Dated, append-only archive of each week's report (the latest also stays at
# output/report.md for convenience). Past weeks are kept, never overwritten.
REPORTS_DIR = OUT_DIR / "reports"

# Agent-side path prefix for persistent cross-week memory (deepagents convention).
MEM_PREFIX = "/memories/"

_STUB_COVERAGE = """# Throughline coverage ledger

No prior coverage yet — this is the first run. Treat every topic as NEW.
"""


def _content(fd) -> str:
    body = fd["content"] if isinstance(fd, dict) else fd
    return "\n".join(body) if isinstance(body, list) else body


def _load_memory() -> dict[str, dict]:
    """Read persisted memory from ./memory into agent files keyed under /memories/.

    Values use the deepagents file shape ({"content": <str>}) so the agent's
    filesystem backend can read them. Falls back to a seed stub so the editor
    always has a coverage file to read on its first turn, even before any week
    has been recorded.
    """
    files: dict[str, dict] = {}
    if MEM_DIR.is_dir():
        for path in sorted(MEM_DIR.rglob("*")):
            # Skip committed *.example.* docs — they illustrate the format but are
            # not real memory and must not be fed to the editor.
            if path.is_file() and ".example." not in path.name:
                rel = path.relative_to(MEM_DIR).as_posix()
                files[MEM_PREFIX + rel] = {"content": path.read_text(encoding="utf-8")}
    files.setdefault(MEM_PREFIX + "coverage.md", {"content": _STUB_COVERAGE})
    return files


def _mirror_file(path: str, body: str, dest_root: Path, root: Path, rel: str) -> None:
    """Write one agent file to disk under dest_root, refusing path escapes.

    Contents are untrusted web-derived text and paths came from the agent, so
    anything that would escape dest_root is skipped.
    """
    dest = (dest_root / rel).resolve()
    if dest != root and root not in dest.parents:
        print(f"  SKIPPED (escapes {dest_root.name}/): {path}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    print(f"  {path}  ->  {dest_root.name}/{dest.relative_to(root)}  ({len(body):,} chars)")


def _shorten(text: str, limit: int = 80) -> str:
    """Collapse whitespace and trim to a single readable line."""
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _describe_tool_call(tc: dict) -> str | None:
    """Turn one tool call into a friendly progress line (or None to stay quiet)."""
    name = tc.get("name", "")
    args = tc.get("args", {}) or {}
    if name == "read_file":
        return f"  📖 recalling memory: {args.get('file_path', '?')}"
    if name == "scan_ai_week":
        extra = args.get("extra_query") or ""
        return f"  🔎 scanning the week's AI writing{f' — {extra}' if extra else ''}"
    if name == "task":
        topic = args.get("description") or args.get("subagent_type") or "a topic"
        return f"  🧵 researcher dispatched → {_shorten(topic)}"
    if name == "internet_search":
        return f"     ↳ searching: {_shorten(args.get('query', '?'))}"
    if name == "write_file":
        return f"  📝 writing {args.get('file_path', '?')}"
    return None


def _print_progress(update: dict) -> None:
    """Announce tool calls as the agent makes them, for a live sense of progress."""
    for node_update in update.values():
        msgs = node_update.get("messages") if isinstance(node_update, dict) else None
        for msg in msgs or []:
            for tc in getattr(msg, "tool_calls", None) or []:
                line = _describe_tool_call(tc)
                if line:
                    print(line, flush=True)


def _pending_review(agent, config) -> dict | None:
    """Return the pending human-review request if the run is paused, else None.

    When the report-review interrupt fires, the graph pauses and the HITLRequest
    payload (action_requests + review_configs) is stored on the paused task.
    """
    state = agent.get_state(config)
    if not state.next:
        return None
    for task in state.tasks:
        for intr in task.interrupts or ():
            return intr.value
    return None


def _review_report(request: dict) -> dict:
    """Show the drafted report and collect a human decision.

    Returns a HumanInTheLoopMiddleware decision dict: approve, edit (supply a file
    path with the revised report), or reject.
    """
    action = request["action_requests"][0]
    args = action.get("args", {})
    content = args.get("content") or "(no content in the write call)"

    print("\n" + "=" * 60)
    print("HUMAN REVIEW — draft report held for the following reason(s):")
    for reason in report_risk_signals(content):
        print(f"  ⚠ {reason}")
    print("\nDraft report:\n")
    print(content)
    print("=" * 60)
    print("Decision: [a]pprove  /  [e]dit (supply a file path)  /  [r]eject")
    choice = input("> ").strip().lower()

    if choice.startswith("r"):
        return {
            "type": "reject",
            "message": "Report rejected by human reviewer; nothing was published.",
        }
    if choice.startswith("e"):
        path = input("Path to a markdown file with the edited report: ").strip()
        edited = Path(path).expanduser().read_text(encoding="utf-8")
        return {
            "type": "edit",
            "edited_action": {"name": action["name"], "args": {**args, "content": edited}},
        }
    return {"type": "approve"}


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    MEM_DIR.mkdir(exist_ok=True)
    prompt = f"Build this week's AI-news report. Today is {date.today():%Y-%m-%d}."

    # A checkpointer + thread_id let the report-review interrupt pause and resume.
    agent = build_agent(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 100}

    print("Throughline — building this week's report. Live progress:\n", flush=True)

    # Stream so the run narrates itself instead of sitting silent. "updates" drives
    # the progress lines; "values" carries the full state, whose last root emission
    # is the finished run we mirror to disk. subgraphs=True lets the parallel
    # researchers' searches surface too. The run may pause for human review of the
    # report, so wrap the stream in a resume loop.
    stream_input: object = {
        "messages": [{"role": "user", "content": prompt}],
        # Seed prior weeks so the editor can dedup and track storylines.
        "files": _load_memory(),
    }
    final_state: dict = {}
    rejected = False
    reviewed = False

    while True:
        for namespace, mode, chunk in agent.stream(
            stream_input,
            config=config,
            stream_mode=["updates", "values"],
            subgraphs=True,
        ):
            if mode == "values":
                if not namespace:  # only the root graph holds the final, complete state
                    final_state = chunk
            else:
                _print_progress(chunk)

        request = _pending_review(agent, config)
        if request is None:
            break  # the run finished (no pending interrupt)
        reviewed = True
        decision = _review_report(request)
        if decision["type"] == "reject":
            rejected = True
        # Resume the paused run with the human's decision.
        stream_input = Command(resume={"decisions": [decision]})

    if rejected:
        print("\n" + "=" * 60)
        print("Report rejected — nothing written to disk, memory left unchanged.")
        return

    if not reviewed:
        print("\n  ✓ no risk signals — report auto-approved (no human review needed)")

    result = final_state

    # The editor's final reply (teaser + kept/dropped summary).
    print("\n" + "=" * 60)
    print(result["messages"][-1].content)

    files = result.get("files", {})
    if "/output/report.md" not in files:
        raise SystemExit("Agent did not write /output/report.md — check the run above.")
    report_body = _content(files["/output/report.md"])

    out_root = OUT_DIR.resolve()
    mem_root = MEM_DIR.resolve()
    print("\nWriting agent files to disk:")
    for path in sorted(files):
        body = _content(files[path])
        if path.startswith(MEM_PREFIX):
            # Persistent cross-week memory -> ./memory
            _mirror_file(path, body, MEM_DIR, mem_root, path[len(MEM_PREFIX):])
        else:
            # This week's report and research archive -> ./output
            rel = path[len("/output/"):] if path.startswith("/output/") else path.lstrip("/")
            _mirror_file(path, body, OUT_DIR, out_root, rel)

    # Keep a dated copy so the weeks accumulate into a browsable archive rather
    # than each run overwriting the last.
    REPORTS_DIR.mkdir(exist_ok=True)
    archived = REPORTS_DIR / f"{date.today():%Y-%m-%d}.md"
    archived.write_text(report_body, encoding="utf-8")
    print(f"  archived  ->  output/{archived.relative_to(OUT_DIR)}  ({len(report_body):,} chars)")

    print(f"\nRead this week's report: {OUT_DIR / 'report.md'}")
    print(f"Browse past weeks:       {REPORTS_DIR}")


if __name__ == "__main__":
    main()
