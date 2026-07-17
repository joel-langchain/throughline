"""Run the editor agent and save everything it produced.

The agent runs on the default (ephemeral) StateBackend, so its writes land in
agent state, not on your real disk — important because it processes untrusted
web-search text. This trusted host code reads files out of state after the run
and mirrors them into ./output, with a path-traversal guard.

Usage:
    uv run python -m ai_news_agent.run
"""

from datetime import date
from pathlib import Path

from ai_news_agent.agent import agent

OUT_DIR = Path(__file__).resolve().parents[2] / "output"


def _content(fd) -> str:
    body = fd["content"] if isinstance(fd, dict) else fd
    return "\n".join(body) if isinstance(body, list) else body


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    prompt = f"Build this week's AI-news report. Today is {date.today():%Y-%m-%d}."

    result = agent.invoke(
        {"messages": [{"role": "user", "content": prompt}]},
        config={"recursion_limit": 100},
    )

    # The editor's final reply (teaser + kept/dropped summary).
    print(result["messages"][-1].content)

    files = result.get("files", {})
    if "/output/report.md" not in files:
        raise SystemExit("Agent did not write /output/report.md — check the run above.")

    out_root = OUT_DIR.resolve()
    print("\nWriting agent files to disk:")
    for path in sorted(files):
        rel = path[len("/output/"):] if path.startswith("/output/") else path.lstrip("/")
        dest = (OUT_DIR / rel).resolve()

        # Contents are untrusted web text and paths came from the agent — refuse
        # anything that would escape ./output.
        if dest != out_root and out_root not in dest.parents:
            print(f"  SKIPPED (escapes output dir): {path}")
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        body = _content(files[path])
        dest.write_text(body, encoding="utf-8")
        print(f"  {path}  ->  {dest.relative_to(out_root)}  ({len(body):,} chars)")

    print(f"\nRead this week's report: {OUT_DIR / 'report.md'}")


if __name__ == "__main__":
    main()
