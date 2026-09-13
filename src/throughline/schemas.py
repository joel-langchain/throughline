"""Typed contracts between the editor and its subagents.

Every subagent's reply gates an editor decision: drop this topic or keep it,
re-research or move on, revise the report or publish it. Those replies used to be
free text in a prescribed shape ("VERDICT: KEEP | SKIP", and so on), which meant
the decision rested on a small model formatting a header correctly and the editor
reading it correctly. A dropped colon or a reworded label is a silently wrong
decision, not a visible error.

These schemas make the shape the model's obligation rather than its intention:
deepagents gives each subagent a `response_format`, the model fills in fields, and
the JSON comes back as the task tool's result. A verdict can now only be one of
its allowed values.

The prose fields are still prose — a summary is a summary. What is pinned here is
the structure the editor branches on.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Source(BaseModel):
    """One cited source behind a topic summary."""

    title: str = Field(description="The article or paper title.")
    url: str = Field(description="The source's URL, exactly as the search returned it.")


class ResearchResult(BaseModel):
    """What a topic-researcher returns for its one topic."""

    topic: str = Field(description="The topic you were asked to research.")
    verdict: Literal["KEEP", "SKIP"] = Field(
        description=(
            "KEEP if the topic is backed by independent, reputable reporting. "
            "SKIP if it fails source quality, or if its only substance is a "
            "reworded press release."
        )
    )
    reason: str = Field(description="One line: why you kept or skipped it.")
    summary: str = Field(
        description=(
            "120-180 words, factual, with inline [n] citation markers keyed to "
            "the sources list. Empty when the verdict is SKIP."
        )
    )
    sources: list[Source] = Field(
        default_factory=list,
        description=(
            "The sources behind the summary, in citation order: the first entry "
            "is [1], the second [2], and so on."
        ),
    )


class UnsupportedClaim(BaseModel):
    """One claim the citation-verifier could not find support for."""

    claim: str = Field(description="The claim's exact text, quoted from the summary.")
    citation: int | None = Field(
        default=None, description="The [n] marker on that claim, if it carried one."
    )
    what_the_source_says: str = Field(
        description="One line: what the cited source actually says instead."
    )


class VerificationResult(BaseModel):
    """What the citation-verifier returns for one topic."""

    topic: str = Field(description="The topic you checked.")
    verdict: Literal["PASS", "FLAG"] = Field(
        description="FLAG if ANY cited claim is unsupported, otherwise PASS."
    )
    unsupported: list[UnsupportedClaim] = Field(
        default_factory=list,
        description="The unsupported claims. Empty when the verdict is PASS.",
    )


class FinalPassResult(BaseModel):
    """What the final-pass reviewer returns for the assembled report."""

    verdict: Literal["APPROVE", "REVISE"] = Field(
        description="REVISE only for a real whole-report problem the editor can fix."
    )
    issues: list[str] = Field(
        default_factory=list,
        description=(
            "One line per concrete, fixable problem. Empty when the verdict is "
            "APPROVE."
        ),
    )
    note: str = Field(description="One line: your overall read on publishing this.")
