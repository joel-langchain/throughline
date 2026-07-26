# Throughline

_Find the thread through the week's AI noise._

A weekly AI-news agent. It reads across the week's writing and returns **one
cited report**: what actually happened, where the market is moving, and what's
worth paying attention to.

It cuts through volume the way a small editorial team would — an editor
delegates each topic to a researcher, then synthesises the results.

## Architecture

Built on the [deepagents](https://pypi.org/project/deepagents/) editor +
subagent-team pattern:

```mermaid
flowchart TD
    A[Scan the week's<br/>AI writing] --> B{Editor<br/>clusters emerging topics}
    B --> R1[Researcher<br/>Topic 1]
    B --> R2[Researcher<br/>Topic 2]
    B --> R3[Researcher<br/>Topic 3]
    subgraph G [Two rules every researcher holds to]
        direction LR
        Q1[Reputable, independent<br/>sources only]
        Q2[No reworded<br/>press releases]
    end
    R1 -.-> G
    R2 -.-> G
    R3 -.-> G
    R1 --> S[Editor drops SKIPs, then<br/>synthesises ONE cited report]
    R2 --> S
    R3 --> S
```

```
editor (strong model)
  │  scan_ai_week() once → cluster the week into topics that emerge from the data
  │  delegate each topic in parallel via the task tool
  ├──► topic-researcher (cheap model, own tools + scoped scratch disk)
  │       search reputable sources, quarantine raw hits to
  │       /research/<topic>/sources.md, return one cited summary + KEEP/SKIP
  │       verdict
  └──► ... one researcher per topic ...
  collect summaries → drop the SKIPs (quality gate) → synthesise ONE cited
  report → /output/report.md
```

Why multi-agent: topics are independent, so researchers run **in parallel**, each
in its **own isolated context**. Bulky raw search text is quarantined in each
researcher's scratch folder so it never floods the editor's context.

### Two rules the system holds to

1. **Reputable, independent sources only** — researchers and analysts, not vendor
   blogs. Company posts are treated as claims, not facts.
2. **No reworded press releases** — if a topic's only substance is a reworded
   announcement, the researcher returns `SKIP` and it doesn't go in the report.

## Setup

```bash
cd throughline
cp .env.example .env      # then fill in TAVILY_API_KEY and ANTHROPIC_API_KEY
uv sync
```

## Run

```bash
uv run python -m throughline.run
```

The finished report lands in `output/report.md`; each researcher's raw archive is
mirrored under `output/research/<topic>/` so you can inspect what was quarantined.
What the agent remembers between weeks lives in `memory/` (see below).

## Memory & continuity

Throughline remembers what it has already covered, so the weeks build on each
other instead of repeating. Before each run the editor reads a running coverage
ledger; when it clusters the week it tags every topic **NEW** or **DEVELOPING**,
drops pure repeats, and for a developing storyline leads with *what actually
changed* since last time. After the run it updates its own memory for next week.

Memory is just files under the agent path `/memories/` (the deepagents
convention):

- `memory/coverage.md` — the running ledger, one line per topic per week.
- `memory/this-week.json` — a structured record of the latest week.

Right now the runner persists these by mirroring `/memories/` to the local
`memory/` folder either side of a run — deliberately simple. When the agent is
deployed (see the roadmap), that same `/memories/` path swaps to a persistent
`StoreBackend` and the local mirror goes away, with **no change** to the agent
itself.

## Shipped so far

Building in public, newest first. Ticked items in the roadmap below are done;
the core that predates the roadmap is captured here too. Everything is a
deliberately simple first cut and will be refined.

- **Cross-week memory & continuity** — the editor reads a running coverage ledger
  before it clusters, tags each topic **NEW** or **DEVELOPING**, drops pure
  repeats, and leads developing storylines with what changed. Weeks now build on
  each other. _(memory lives under `/memories/`; see [Memory & continuity](#memory--continuity))_
- **Source legitimacy enforcement** — known press-release / wire domains are
  dropped before a researcher ever sees them, reputable domains are tagged, and
  the quality rubric is enforced at both the researcher and the editor.
- **Core system** — an editor clusters the week's writing into emerging topics and
  delegates each to a parallel researcher team, then synthesises one cited report.
  Researchers run in isolated contexts with raw search text quarantined per topic.

## Roadmap

Building this in public — rough order, not fixed. Contributions and ideas welcome.

**Memory & continuity**

- [x] Long-term memory — remember what's already been covered so weeks build on
  each other instead of repeating
- [x] Track ongoing storylines week-over-week (what changed, what's new)
- [x] Cross-week deduplication of topics _(topic-level, editor judgement for now;
  source-level dedup still to come)_

**Trust & quality**

- [ ] Human-in-the-loop review/approve step before anything is published
- [ ] Config-driven source allow/deny lists (currently hard-coded)
- [ ] Citation verification — check that claims actually match the cited source
- [ ] Evals — score reports on source quality, factuality, dedup, and voice

**Publishing**

- [ ] LinkedIn posting via MCP (draft → review → post)
- [ ] Consistent editorial voice as the architecture changes underneath
- [ ] Multiple output formats (report, social teaser, email digest)

**Interaction & intelligence**

- [ ] Chat with the week — Q&A over the finished report and its research
  archives (deep dives, "why keep this?", "show me the primary paper")
- [ ] Suggest follow-up research ideas from what it surfaced
- [ ] Market-movement analysis — where things are heading, not just what happened
- [ ] Forecasting — record explicit predictions each week and score them against
  what actually happens (a self-evaluating prediction loop)

**Operations**

- [ ] Deployment (LangSmith Deployment)
- [ ] Scheduled weekly runs (cron)
- [ ] Monitoring & tracing (LangSmith)
- [ ] Cost controls — token budgets and per-run cost tracking
- [ ] Resilience — retries, rate-limit handling, and graceful search failures

**Foundations**

- [ ] Automated tests + CI
- [ ] Structured, typed outputs from researchers (not just free text)
- [ ] Configurable scan seeds and topic count

## Notes on safety

- The agent runs on the default ephemeral StateBackend — it never writes to your
  real filesystem. The runner copies files out of agent state with a
  path-traversal guard.
- Web-search text is untrusted; keep that in mind before rendering it anywhere.
