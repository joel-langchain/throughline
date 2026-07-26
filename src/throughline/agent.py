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
      synthesise ONE report with citations → /output/report.md, then
      update memory (/memories/coverage.md + /memories/this-week.json) so
      next week builds on this one.

Two rules the system must hold to:
  1. Only reputable, independent sources (researchers/analysts, not vendor blogs).
  2. If a summary is just a reworded press release, it does not go out.

Cross-week memory lives under /memories/ (the deepagents convention). Today it is
persisted by the runner mirroring /memories/ to ./memory; on deployment that path
swaps to a persistent StoreBackend with no change to this agent.
"""

from deepagents import FilesystemPermission, create_deep_agent

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
6. Synthesise the KEPT topics into ONE report and write it with write_file to
   /output/report.md. Structure:
     # Throughline — This Week in AI · <date>
     _One-paragraph "what actually happened and where the market is moving."
     Open this with a short "Since last week:" clause when there are DEVELOPING
     storylines, so the reader feels the continuity._
     ## <Topic>  <add "(developing)" to the heading for DEVELOPING storylines>
     <2-4 sentences of synthesis with inline [n] citations; for a developing
     story, lead with what actually changed since last week>
     ...
     ## Sources
     <numbered list of every cited source across topics>
7. UPDATE YOUR MEMORY so next week can build on this one. Write TWO files:
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
8. End your reply with a SHORT plain-text teaser (2-3 sentences) suitable for a
   social post, plus a note of how many topics you kept vs dropped, how many were
   NEW vs DEVELOPING, and why.

Keep your own context on coordination and synthesis, not raw search text."""

editor_permissions = [
    # The editor curates persistent memory; researchers must not touch it.
    FilesystemPermission(operations=["read", "write"], paths=["/memories/**"], mode="allow"),
    FilesystemPermission(operations=["write"], paths=["/research/**"], mode="deny"),
]

agent = create_deep_agent(
    model=strong_model,
    tools=[scan_ai_week],
    system_prompt=EDITOR_PROMPT,
    subagents=[topic_researcher],
    permissions=editor_permissions,
)
