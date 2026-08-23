"""Create (or list) the weekly cron for the Throughline deployment.

Loop 3, the event loop: once the graph is deployed on LangGraph Platform, this
schedules it to run on its own — no local machine, no you. Run it ONCE after the
deployment is live (and again only to change the schedule).

The cron fires the deployed ``throughline`` graph on a schedule. Cross-week
memory lives in the deployment's persistent Store (see ``build_agent`` /
``StoreBackend``), so each scheduled run reads and extends the same ledger and the
weeks keep building on each other. The report is finalised in-graph (citations
renumbered by the agent's ``after_agent`` step), so no host post-processing is
needed — the finished report lands in the run's output and its LangSmith trace.

Setup:
    uv sync --group deploy
    export LANGGRAPH_DEPLOYMENT_URL="https://<your-deployment>.us.langgraph.app"
    export LANGSMITH_API_KEY="..."   # same key as .env

Usage:
    uv run python scripts/create_cron.py                      # weekly, Mon 05:00 UTC
    uv run python scripts/create_cron.py --schedule "0 7 * * 1"
    uv run python scripts/create_cron.py --list               # show existing crons
"""

from __future__ import annotations

import argparse
import os
import sys

# Weekly on Monday at 05:00 UTC (06:00 BST) — the report is finished and delivered
# well before the start of the working day.
DEFAULT_SCHEDULE = "0 5 * * 1"
GRAPH_ID = "throughline"
# Static input — the same prompt a local run uses. Today's date is injected inside
# the graph at run time (see agent.todays_date_middleware), so a cron created once
# still dates each week's report and memory correctly.
RUN_INPUT = {
    "messages": [
        {"role": "user", "content": "Build this week's AI-news report."}
    ]
}


def _client():
    url = os.getenv("LANGGRAPH_DEPLOYMENT_URL")
    if not url:
        raise SystemExit(
            "Set LANGGRAPH_DEPLOYMENT_URL to your deployment's API URL "
            "(and LANGSMITH_API_KEY) before running this script."
        )
    try:
        from langgraph_sdk import get_sync_client
    except ImportError as exc:  # pragma: no cover - guidance path
        raise SystemExit(
            "langgraph-sdk is not installed. Run: uv sync --group deploy"
        ) from exc
    return get_sync_client(url=url, api_key=os.getenv("LANGSMITH_API_KEY"))


def _resolve_assistant_id(client) -> str:
    """Resolve the deployed assistant for our graph, falling back to the graph name.

    The default assistant usually shares its graph's name, but resolving the real
    id first makes the cron robust if that ever changes.
    """
    try:
        assistants = client.assistants.search(graph_id=GRAPH_ID, limit=1)
    except Exception:  # noqa: BLE001 - best-effort; fall back to the graph name
        return GRAPH_ID
    if assistants:
        return assistants[0].get("assistant_id", GRAPH_ID)
    return GRAPH_ID


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the Throughline weekly cron.")
    parser.add_argument(
        "--schedule",
        default=DEFAULT_SCHEDULE,
        help=f"5-field cron expression (default: '{DEFAULT_SCHEDULE}', Mon 05:00 UTC).",
    )
    parser.add_argument(
        "--list", action="store_true", help="List existing crons and exit."
    )
    args = parser.parse_args()

    client = _client()

    if args.list:
        crons = client.crons.search()
        if not crons:
            print("No crons found for this deployment.")
            return
        for cron in crons:
            print(f"{cron.get('cron_id', '?')}  schedule={cron.get('schedule', '?')}")
        return

    assistant_id = _resolve_assistant_id(client)
    client.crons.create(assistant_id, schedule=args.schedule, input=RUN_INPUT)
    print(f"Scheduled '{GRAPH_ID}' (assistant {assistant_id}) — cron '{args.schedule}'.")
    print("Throughline will now build the week's report on that schedule, unattended.")
    print("Confirm with: uv run python scripts/create_cron.py --list")


if __name__ == "__main__":
    sys.exit(main())
