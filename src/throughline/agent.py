"""The weekly AI-news agent.

Architecture (deepagents editor + subagent team):

    editor (strong model)
      │  read /memories/coverage.md → know what past weeks already covered
      │  scan_ai_week() once → cluster the week into topics
      │  tag each NEW vs DEVELOPING, drop pure repeats (cross-week dedup)
      │  delegate each kept topic in parallel via the task tool
      ├──► topic-researcher (cheap model, own tools + scoped disk)
      │       search reputable sources, quarantine raw hits to
      │       /research/<topic>/sources.md, return one cited summary
      │       + a press-release verdict
      └──► ... one researcher per topic ...
      collect summaries, drop the ones that fail the quality gate,
      then check each kept topic's citations against its quarantined sources
      via a citation-verifier subagent; on a FLAG, re-dispatch the researcher to
      close the gap and re-verify (bounded retry — loop 2), dropping/correcting
      any claims that still cannot be supported,
      synthesise ONE report with citations → /output/report.md, then
      have a final-pass-reviewer subagent read the assembled report end to end
      (APPROVE/REVISE on whole-report coherence) before publishing, then
      update memory (/memories/coverage.md + /memories/this-week.json) so
      next week builds on this one.

Two rules the system must hold to:
  1. Only reputable, independent sources (researchers/analysts, not vendor blogs).
  2. If a summary is just a reworded press release, it does not go out.

Cross-week memory lives under /memories/ (the deepagents convention). Today it is
persisted by the runner mirroring /memories/ to ./memory; on deployment that path
swaps to a persistent StoreBackend with no change to this agent.
"""

import re
from datetime import date

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langchain.agents.middleware import InterruptOnConfig, after_agent, dynamic_prompt
from langchain.agents.middleware.types import ToolCallRequest

from throughline.citations import renumber as renumber_citations
from throughline.config import (
    MAX_VERIFY_RETRIES,
    MIN_SOURCES,
    PRESS_RELEASE_DOMAINS,
    SENSITIVE_TERMS,
    UNCERTAINTY_MARKERS,
)
from throughline.delivery import deliver_report
from throughline.models import model, strong_model
from throughline.tools import internet_search, scan_ai_week

# --- The research subagent -------------------------------------------------

RESEARCHER_PROMPT = """You are an AI-news analyst researching ONE topic for a
weekly report. You will be given a single topic and an assigned research folder.

How to work:
1. Use internet_search a few times to find what actually happened on this topic
   this week: releases, results, primary papers, credible analysis.
2. Save the COMPLETE, verbatim output of ALL your searches to a single file:
   write_file("/research/<topic>/sources.md", ...). Paste results exactly as the
   tool returned them — every title, URL, and content snippet. Do NOT summarise
   or trim. This raw archive stays here so it never clutters the editor.
3. Only then write your summary from what you found.

FOLLOW-UP REQUESTS (a gap-closing re-dispatch):
- If your task says specific claims were UNSUPPORTED in a previous pass, this is a
  second look. FIRST read your existing /research/<topic>/sources.md, THEN run
  additional searches aimed squarely at those flagged claims, THEN write the
  COMBINED old + new results back to /research/<topic>/sources.md (keep the prior
  sources, add the new ones — do not lose what was already there).
- Return an updated summary that keeps ONLY claims your sources actually support;
  correct or drop the ones you could not substantiate. Do not restate a claim you
  still cannot back.

SOURCE QUALITY (rule 1 — hard requirement):
- Prefer independent, reputable sources: primary papers (arXiv), respected
  researchers and analysts, independent journalists.
- Each search result carries a `source_quality` field ("reputable" or
  "unverified") and known press-release/wire domains are already removed. Lean on
  the "reputable" ones; treat "unverified" results with more caution.
- Treat vendor/company blogs and press releases as claims, not facts. You may
  mention them, but do not present marketing as findings.

PRESS-RELEASE CHECK (rule 2 — hard requirement):
- If, after searching, the only substance you can find is a reworded press
  release or announcement with no independent reporting or analysis, do NOT
  fabricate significance. Say so.

Return ONLY this, as your reply:
  TOPIC: <topic>
  VERDICT: KEEP | SKIP        # SKIP if it fails rule 1 or 2
  REASON: <one line — why keep or skip>
  SUMMARY: <120-180 words, factual, with inline [n] citation markers>
  SOURCES:
    [1] <title> — <url>
    [2] ...
Do not paste raw search dumps into your reply — those live in your files."""

research_permissions = [
    FilesystemPermission(operations=["read", "write"], paths=["/research/**"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]

topic_researcher = {
    "name": "topic-researcher",
    "description": (
        "Research ONE AI-news topic and return a short cited summary plus a "
        "KEEP/SKIP verdict. Delegate one topic per call."
    ),
    "system_prompt": RESEARCHER_PROMPT,
    "tools": [internet_search],
    "model": model,
    "permissions": research_permissions,
}


# --- The citation verifier subagent ----------------------------------------

VERIFIER_PROMPT = """You are a citation checker for a weekly AI-news report. You
are given ONE topic, its research folder, and the researcher's summary with
inline [n] citation markers. Your job is to decide whether each cited claim is
actually supported by the quarantined source material — nothing else.

How to work:
1. Read /research/<topic>/sources.md — the researcher's complete, verbatim search
   results for this topic. This is the ONLY evidence you may use. Do NOT search
   the web and do NOT rely on outside knowledge.
2. Go through the summary claim by claim. For each sentence carrying a [n] marker,
   check whether source [n] in sources.md actually substantiates it.
3. Judge each cited claim:
   - SUPPORTED — the cited source states or clearly implies the claim.
   - UNSUPPORTED — the source does not back it, the [n] points to the wrong
     source, or the claim overstates what the source actually says.
4. Two extra faithfulness checks, using ONLY sources.md:
   - PROPER-NOUN / NUMBER FIDELITY: every model name, product name, organisation,
     person, and quantity in the summary must appear in the source material. Flag
     any that do not — a name or figure the sources never mention (e.g. a
     model called something the sources don't call it) is UNSUPPORTED, even if the
     surrounding claim is broadly true. This guards against invented or
     misremembered names.
   - ATTRIBUTION BALANCE: if the summary pins a behaviour or result on ONE party
     (one company, one model) but the cited source attributes it to several, that
     is an overstatement — flag it as UNSUPPORTED so the editor can attribute it
     evenly.

Return ONLY this, as your reply:
  TOPIC: <topic>
  VERDICT: PASS | FLAG        # FLAG if ANY cited claim is UNSUPPORTED
  UNSUPPORTED:
    - "<exact claim text>" [n] — <one line: what source [n] actually says>
    - ...                       # leave empty when VERDICT is PASS
Keep it terse. Do NOT rewrite the summary — only report what is not supported."""

verifier_permissions = [
    # Read-only: the verifier checks claims against the quarantined sources and
    # must never write anywhere (it produces a verdict in its reply, not files).
    FilesystemPermission(operations=["read"], paths=["/research/**"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]

citation_verifier = {
    "name": "citation-verifier",
    "description": (
        "Check one topic's cited claims against its quarantined sources and return "
        "PASS or FLAG with the unsupported claims. Delegate one topic per call."
    ),
    "system_prompt": VERIFIER_PROMPT,
    "tools": [],
    "model": model,
    "permissions": verifier_permissions,
}


# --- The final-pass reviewer subagent --------------------------------------

FINAL_PASS_PROMPT = """You are the final-pass reviewer for a weekly AI-news
report. The per-topic research and citation checks are already done; your job is
the last read-through a human editor does before publishing — does the WHOLE
report hold together as one coherent piece? You judge the assembled report, not
individual sources.

How to work:
1. Read /output/report.md — the finished, synthesised report. This is the ONLY
   thing you review. Do NOT search the web, do NOT re-check citations against
   sources (that has already been done), and do NOT read the research folders.
2. Read it end to end as a subscriber would and check the whole-report qualities:
   - COHERENCE: the topics form one thread, not a disconnected list; the opening
     paragraph's "what actually happened / where the market is moving" actually
     matches the sections that follow. If the intro claims storylines "converge"
     or "connect", a body section must actually make that link — flag an intro
     promise the body never delivers. Flag an unexplained prior-week callback that
     assumes the reader saw last week's edition.
   - CONTINUITY: if there are DEVELOPING storylines, the "Since last week:" thread
     reads sensibly and each developing section leads with what changed.
   - CONSISTENCY: no two sections contradict each other; the framing is even.
   - COMPLETENESS: required structure is present (title with date, intro
     paragraph, one "##" section per kept topic, a "## Sources" list) and nothing
     is a leftover placeholder, TODO, empty heading, or truncated sentence.
   - CITATIONS: citations are written as [[url]] markers in the prose (a
     downstream step converts them to numbered [n] and builds the numbered
     Sources list, so do NOT police numbering here). Check instead that every
     claim that needs a source carries a [[url]] marker, that each such URL also
     appears once in the Sources list, and that the Sources list has no entry that
     is never cited in the prose.
   - PRECISION: flag a count given as a range for a single quantity, and a
     comparative (e.g. "100x cheaper") stated without the unit it is measured in.

Return ONLY this, as your reply:
  VERDICT: APPROVE | REVISE
  ISSUES:
    - <one line: a concrete, fixable problem with the assembled report>
    - ...                       # leave empty when VERDICT is APPROVE
  NOTE: <one line — overall read on whether this is ready to publish>
Report REVISE only for real whole-report problems the editor can fix by editing
/output/report.md; do not nitpick wording. Keep it terse and do not rewrite the
report yourself."""

final_pass_permissions = [
    # Read-only on the finished report; the reviewer returns a verdict in its
    # reply, never writes, and never touches the research folders or memory.
    FilesystemPermission(operations=["read"], paths=["/output/**"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]

final_pass_reviewer = {
    "name": "final-pass-reviewer",
    "description": (
        "Read the finished /output/report.md end to end and return APPROVE or "
        "REVISE on whether the whole report holds together (coherence, continuity, "
        "consistency, completeness). Call once after the report is written."
    ),
    "system_prompt": FINAL_PASS_PROMPT,
    "tools": [],
    "model": model,
    "permissions": final_pass_permissions,
}


# --- The editor (main agent) -----------------------------------------------

EDITOR_PROMPT = """You are the editor of Throughline, a weekly AI-news report.
Your job is to cut through the volume and surface the small part that actually
matters — the thread running through the week's noise.

SOURCE STANDARD (set here, upheld everywhere):
- A claim only counts as fact when an independent, reputable source stands behind
  it: primary papers (e.g. arXiv), respected researchers/analysts, or independent
  journalism. Vendor and company blogs, and press releases, are CLAIMS, not facts.
- The search tools already drop known press-release/wire domains and tag
  reputable ones, but the standard is yours to enforce: if a topic rests only on
  marketing or a reworded announcement, it does not go in the report.
- Every researcher works to this same standard; you apply it again at synthesis.

Work in this order:
1. Read /memories/coverage.md FIRST. This is your memory of what Throughline has
   already covered in previous weeks. Use it so the weeks build on each other
   instead of repeating. (If it says there is no prior coverage, treat this as
   the first week and everything is NEW.)
2. Call scan_ai_week ONCE to see what people are writing about this week.
   (Call it a second time with an `extra_query` only if the picture is thin.)
3. CLUSTER what you see into 4-6 topics that EMERGE from the writing — the
   themes people are actually discussing, not a fixed list you decided in
   advance. Give each a short, specific name. Then, using your memory from step
   1, tag each topic:
     - NEW — not meaningfully covered before.
     - DEVELOPING — a storyline you have covered before. Keep it ONLY if there is
       a genuine update this week; note in one line WHAT CHANGED since last time.
   DEDUPLICATE: if a topic is just a restatement of something already covered
   with nothing new, drop it — that is exactly the repetition memory exists to
   prevent.
4. For EACH topic you are keeping, delegate to the topic-researcher subagent
   using the task tool — fire them off IN PARALLEL. Tell each one its topic and
   its assigned folder (/research/<topic>/). Do NOT research topics yourself.
5. Collect the returned summaries. Apply the QUALITY GATE: drop any topic whose
   verdict is SKIP (failed source quality or was just a reworded press release).
6. VERIFY CITATIONS (the verification loop). For EACH kept topic:
   a) Delegate to the citation-verifier subagent via the task tool. Give it the
      topic, its research folder (/research/<topic>/), and the researcher's
      summary. It reads ONLY the quarantined sources and returns PASS or FLAG
      with the unsupported claims.
   b) If PASS, keep the topic as researched.
   c) If FLAG, GO AGAIN: re-dispatch the SAME topic to the topic-researcher via
      the task tool, telling it EXACTLY which claims were unsupported and to find
      independent support for them (or correct/remove them). Then re-verify the
      updated summary by repeating step (a).
   d) STOP CONDITION: run this re-research + re-verify loop AT MOST
      <<MAX_VERIFY_RETRIES>> time(s) per topic. This cap is a HARD limit, not the
      verifier's or your own judgement — do not exceed it however tempting. If a
      topic still FLAGs after the final allowed attempt, drop the unsupported
      claims (or the whole topic if its substance depends on them), keeping ONLY
      what the sources support. Never invent support to save a claim.
7. Synthesise the KEPT topics into ONE report and write it with write_file to
   /output/report.md. Structure:
     # Throughline — This Week in AI · <date>
     _One-paragraph "what actually happened and where the market is moving."
     Open this with a short "Since last week:" clause when there are DEVELOPING
     storylines, so the reader feels the continuity._
     ## <Topic>  <tag each heading: "(developing)" for a DEVELOPING storyline,
     "(new)" for a NEW one — the tag MUST match the status you assigned in step 3>
     <2-4 sentences of synthesis with inline [[url]] citations (see CITATIONS
     below); for a developing story, LEAD with what actually changed since last
     week. Give NEW and DEVELOPING sections even treatment — do not let one type
     carry a "what changed" line while another of the same type omits it.>
     <where a genuine, concrete practitioner implication exists, close the section
     with ONE plain sentence on what it means for someone building with AI. Only
     when it is real — never manufacture a takeaway to fill the slot.>
     ...
     ## Sources
     <one source per line, each line: "<outlet> — <title> — <date> — <url>".
     Do NOT number these lines yourself and do not worry about their order — a
     downstream step numbers them. Just make sure every source you cited in the
     prose appears here exactly once, with its URL.>

   CITATIONS — cite by URL, do NOT number:
   - In the prose, cite each claim by wrapping the source's URL in DOUBLE square
     brackets right after the claim: e.g. "...17 unsanctioned actions
     [[https://www.theguardian.com/technology/2026/aug/05/...]]." Stack them for
     multiple sources: "[[url1]][[url2]]". Use the exact URL from the researcher's
     SOURCES for that claim.
   - Do NOT write bare numeric markers like [1] or per-section numbers — a
     deterministic downstream step reads your [[url]] markers, assigns the global
     1..N numbering, and rebuilds the numbered Sources list. Your only job is to
     attach the RIGHT URL to each claim and list that URL in Sources. This removes
     the whole class of "sections restart numbering / sources get orphaned" bugs.
   - Cite PRIMARY sources where a story rests on one (e.g. a Science/Nature/arXiv
     paper): put the paper's own URL on the claim and in Sources, not only the
     secondary coverage.
   - Precision in the prose: give counts as a single number (if two sources
     disagree, say so and pick one — do not print a range for a single quantity);
     and when you state a comparative (e.g. "100x cheaper"), name the unit it is
     measured in (per output token, per task, etc.).

   INTRO discipline: only assert that storylines "converge" or "connect" if a
   section in the body actually makes that link — if so, write the connective
   sentence; if not, drop the claim and just say what happened. Do not open with an
   unexplained callback (e.g. a named prior-week event) that assumes the reader saw
   last week's edition; name it only if you explain it in one clause.
8. FINAL PASS. After the report is written, delegate ONCE to the
   final-pass-reviewer subagent via the task tool. It reads the whole
   /output/report.md end to end and returns APPROVE or REVISE on whether the
   report holds together as one coherent piece (coherence, continuity,
   consistency, completeness) — the last read-through before publishing.
   - If APPROVE, proceed.
   - If REVISE, fix the concrete issues it lists by editing /output/report.md
     (write the corrected report back to the same path), then delegate to the
     final-pass-reviewer ONE more time to confirm. Do this re-review AT MOST once;
     if it still reports issues, apply the clearest fixes and move on. Do NOT
     re-run research or re-write topic summaries here — this pass is about the
     assembled report only.
9. UPDATE YOUR MEMORY so next week can build on this one. Write TWO files:
   a) /memories/coverage.md — the running ledger. Keep the prior entries, then
      append a section for this week:
        ## Week of <date>
        - <Topic> [NEW|DEVELOPING] — <one-line synopsis of what happened>
        ...
      Keep it compact (one line per topic). If it is getting long, you may drop
      entries older than ~8 weeks.
   b) /memories/this-week.json — a structured record of this week only, exactly:
      {"week": "<date>", "topics": [
        {"topic": "<name>", "status": "NEW|DEVELOPING",
         "synopsis": "<one line>", "sources": ["<url>", ...]}, ...]}
10. End your reply with a SHORT plain-text teaser (2-3 sentences) suitable for a
   social post, plus a note of how many topics you kept vs dropped, how many were
   NEW vs DEVELOPING, how many had citations flagged by the verifier, and why.

Keep your own context on coordination and synthesis, not raw search text."""

# The retry cap is enforced as a hard number injected from config, not left to
# the model to decide — the stop condition for loop 2 lives outside the agent's
# own judgement.
EDITOR_PROMPT = EDITOR_PROMPT.replace("<<MAX_VERIFY_RETRIES>>", str(MAX_VERIFY_RETRIES))

editor_permissions = [
    # The editor curates persistent memory; researchers must not touch it.
    FilesystemPermission(operations=["read", "write"], paths=["/memories/**"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/research/**"], mode="deny"),
]


# --- Human review before the report is finalised ---------------------------

# The report is written with the built-in write_file tool. Only the single write
# to this path is the finished report; every other write_file call (researchers'
# sources, memory files) must run untouched, so the review interrupt is scoped to
# exactly this path via the `when` predicate below.
REPORT_PATH = "/output/report.md"


def _is_report_write(request: ToolCallRequest) -> bool:
    """True only when the model is writing the finished report to REPORT_PATH.

    Used as the path gate for the review interrupt so only the report itself is a
    review candidate, not the many other file writes in a run.
    """
    tool_call = request.tool_call
    if tool_call.get("name") != "write_file":
        return False
    return tool_call.get("args", {}).get("file_path") == REPORT_PATH


def report_risk_signals(content: str) -> list[str]:
    """Reasons the drafted report warrants a human look, or [] for a clean week.

    The trigger for human review is deliberately conditional: a well-sourced,
    confident, non-sensitive report is auto-approved, and a person is only pulled
    in when the report trips one of the config-driven risk signals below. Also
    used by the runner to tell the reviewer WHY the report was held.
    """
    reasons: list[str] = []
    lower = content.lower()

    # Sensitive/dual-use subject matter: a human should look however well-sourced.
    hits = sorted(t for t in SENSITIVE_TERMS if t in lower)
    if hits:
        reasons.append(f"sensitive topic ({', '.join(hits)})")

    # The editor's own hedging — a proxy for low confidence / gaps it couldn't close.
    markers = sorted(m for m in UNCERTAINTY_MARKERS if m in lower)
    if markers:
        reasons.append(f"editor flagged uncertainty ({', '.join(markers)})")

    # Thin sourcing: too few distinct cited URLs to publish unreviewed.
    distinct = {u.rstrip(".,);]") for u in re.findall(r"https?://[^\s)\]]+", content)}
    if MIN_SOURCES and len(distinct) < MIN_SOURCES:
        reasons.append(f"thin sourcing ({len(distinct)} cited, want >= {MIN_SOURCES})")

    # A press-release/wire domain slipped into the citations despite the filters.
    cited_press = sorted(d for d in PRESS_RELEASE_DOMAINS if d in lower)
    if cited_press:
        reasons.append(f"press-release source cited ({', '.join(cited_press)})")

    return reasons


def _report_needs_review(request: ToolCallRequest) -> bool:
    """Fire the interrupt only for a report write that trips a risk signal."""
    if not _is_report_write(request):
        return False
    content = request.tool_call.get("args", {}).get("content", "")
    return bool(report_risk_signals(content))


# Pause the run to let a human approve, edit, or reject the report ONLY when it
# trips a risk signal (sensitive topic, editor uncertainty, thin/weak sourcing).
# Clean weeks are auto-approved. Requires a checkpointer at build time (see
# build_agent) so the graph can pause and resume.
report_review = {
    "write_file": InterruptOnConfig(
        allowed_decisions=["approve", "edit", "reject"],
        when=_report_needs_review,
        description="This week's report tripped a risk signal — review before it is finalised.",
    )
}


# --- Persistent cross-week memory (deployment) -----------------------------

# The one agent path whose contents must survive between runs: the coverage
# ledger and the structured week record. Locally the runner mirrors this to
# ./memory; on a deployment it is routed to a persistent Store instead (see
# build_agent), with no change to the agent's prompt or paths.
MEMORIES_PREFIX = "/memories/"


def _memory_namespace(runtime: object) -> tuple[str, ...]:
    """Store namespace for memory: one shared ledger for the whole app.

    A fixed namespace (not per-thread or per-assistant) is deliberate — every
    scheduled weekly run reads and extends the SAME cross-week memory, which is
    exactly what makes the weeks build on each other.
    """
    return ("throughline", "memories")


# --- Deterministic citation numbering, done in-graph -----------------------


def _report_body(files: dict | None) -> str | None:
    """Return the report text from filesystem state, or None if not written yet."""
    fd = (files or {}).get(REPORT_PATH)
    if not fd:
        return None
    body = fd["content"] if isinstance(fd, dict) else fd
    return "\n".join(body) if isinstance(body, list) else body


def renumber_report_in_files(files: dict | None) -> dict | None:
    """Return a files-state update that renumbers the report, or None if N/A.

    Pure and side-effect free so it can be unit-tested without running the agent.
    Reads /output/report.md from filesystem state, assigns global 1..N citation
    numbers (see citations.renumber), and returns ONLY the changed file — the
    files channel merges by key, so the research archive and memory are untouched.
    """
    body = _report_body(files)
    if body is None:
        return None
    new_body, _warnings = renumber_citations(body)
    if new_body == body:
        return None
    return {"files": {REPORT_PATH: {"content": new_body, "encoding": "utf-8"}}}


@after_agent
def renumber_citations_middleware(state, runtime) -> dict | None:
    """After the editor finishes: renumber the report in-graph, then deliver it.

    Renumbering (deterministic host-style work, run INSIDE the graph) makes a
    headless / scheduled run's report final on its own. Delivery then pushes that
    finished report to Slack when SLACK_WEBHOOK_URL is set — a no-op otherwise — so
    a scheduled run lands somewhere readable without a laptop. Delivery is
    best-effort and never breaks the run or drops the report from state.
    """
    files = state.get("files")
    update = renumber_report_in_files(files)
    final_body = update["files"][REPORT_PATH]["content"] if update else _report_body(files)
    if final_body:
        deliver_report(final_body)
    return update


# --- Today's date, injected at run time ------------------------------------


def _system_text(system_message: object) -> str:
    """Coerce a system prompt (str, SystemMessage, or content blocks) to text."""
    if system_message is None:
        return ""
    if isinstance(system_message, str):
        return system_message
    content = getattr(system_message, "content", system_message)
    if isinstance(content, list):
        parts = [
            block if isinstance(block, str) else block.get("text", "")
            for block in content
            if isinstance(block, (str, dict))
        ]
        return "\n".join(p for p in parts if p)
    return str(content)


def append_todays_date(system_message: object, today: date | None = None) -> str:
    """Return the system prompt with today's date appended.

    The report title, the coverage ledger's week label, and this-week.json all key
    off *today's* date, and a model does not know the wall-clock date on its own.
    Supplying it here — rather than only in the local runner's prompt — means a
    headless / scheduled run dates its report and memory correctly too, and local
    and deployed runs get the date the exact same way.
    """
    day = today or date.today()
    return f"{_system_text(system_message)}\n\nFor this run, today's date is {day:%Y-%m-%d}."


@dynamic_prompt
def todays_date_middleware(request) -> str:
    return append_todays_date(request.system_message)


def build_agent(checkpointer=None, *, persistent_memory=False, review=True):
    """Build the editor agent.

    The human-review interrupt needs a checkpointer to pause and resume, so the
    runner supplies an in-memory one for local runs. The module-level ``agent``
    below is built WITHOUT a checkpointer to stay deployment-safe (the platform
    injects its own persistence); pass a checkpointer only for local,
    interruptible runs.

    ``persistent_memory`` swaps the ``/memories/`` path from ephemeral run state
    to a persistent Store (via a CompositeBackend), so cross-week memory survives
    between scheduled runs on a deployment. The store is supplied by the LangGraph
    platform at run time; every other path stays in run state.

    ``review`` keeps the opt-in human-review interrupt. A headless deployment sets
    ``review=False`` so a risk-flagged report is auto-finalised rather than pausing
    forever with no human to resume it (matching the documented unattended
    default). Either way the report still goes through the risk checks and the
    in-graph citation renumbering.
    """
    backend = None
    if persistent_memory:
        backend = CompositeBackend(
            default=StateBackend(),
            routes={MEMORIES_PREFIX: StoreBackend(namespace=_memory_namespace)},
        )
    return create_deep_agent(
        model=strong_model,
        tools=[scan_ai_week],
        system_prompt=EDITOR_PROMPT,
        subagents=[topic_researcher, citation_verifier, final_pass_reviewer],
        permissions=editor_permissions,
        interrupt_on=report_review if review else None,
        middleware=[todays_date_middleware, renumber_citations_middleware],
        backend=backend,
        checkpointer=checkpointer,
    )


# The deployable graph (see langgraph.json). Cross-week memory is persistent so a
# scheduled run builds on the last, and review is off so an unattended run never
# blocks. The platform supplies the checkpointer and store at run time.
agent = build_agent(persistent_memory=True, review=False)
