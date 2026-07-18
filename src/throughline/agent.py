"""The weekly AI-news agent.

Architecture (deepagents editor + subagent team):

    editor (strong model)
      │  scan_ai_week() once -> cluster the week into topics
      │  delegate each topic in parallel via the task tool
      ├──► topic-researcher (cheap model, own tools + scoped disk)
      │       search reputable sources, quarantine raw hits to
      │       /research/<topic>/sources.md, return one cited summary
      │       + a press-release verdict
      └──► ... one researcher per topic ...
      collect summaries, drop the ones that fail the quality gate,
      synthesise ONE report with citations -> /output/report.md

Two rules the system must hold to:
  1. Only reputable, independent sources (researchers/analysts, not vendor blogs).
  2. If a summary is just a reworded press release, it does not go out.
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
1. Call scan_ai_week ONCE to see what people are writing about this week.
   (Call it a second time with an `extra_query` only if the picture is thin.)
2. CLUSTER what you see into 4-6 topics that EMERGE from the writing — the
   themes people are actually discussing, not a fixed list you decided in
   advance. Give each a short, specific name.
3. For EACH topic, delegate to the topic-researcher subagent using the task
   tool — fire them off IN PARALLEL. Tell each one its topic and its assigned
   folder (/research/<topic>/). Do NOT research topics yourself.
4. Collect the returned summaries. Apply the QUALITY GATE: drop any topic whose
   verdict is SKIP (failed source quality or was just a reworded press release).
5. Synthesise the KEPT topics into ONE report and write it with write_file to
   /output/report.md. Structure:
     # Throughline — This Week in AI · <date>
     _One-paragraph "what actually happened and where the market is moving."_
     ## <Topic>
     <2-4 sentences of synthesis with inline [n] citations>
     ...
     ## Sources
     <numbered list of every cited source across topics>
6. End your reply with a SHORT plain-text teaser (2-3 sentences) suitable for a
   social post, plus a note of how many topics you kept vs dropped and why.

Keep your own context on coordination and synthesis, not raw search text."""

editor_permissions = [
    FilesystemPermission(operations=["write"], paths=["/research/**"], mode="deny"),
]

agent = create_deep_agent(
    model=strong_model,
    tools=[scan_ai_week],
    system_prompt=EDITOR_PROMPT,
    subagents=[topic_researcher],
    permissions=editor_permissions,
)
