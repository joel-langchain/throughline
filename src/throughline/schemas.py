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

The verifier goes one step further: the model grades each claim, and code derives
the verdict from those grades. A model that marks a claim single-source and then
answers PASS gets FLAG anyway.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


# How well one claim is backed, using only the archived search results. The first
# two are enough to publish; the last two send the topic back for more research.
Support = Literal["PRIMARY", "CORROBORATED", "SINGLE_SOURCE", "UNSUPPORTED"]
PUBLISHABLE: frozenset[str] = frozenset({"PRIMARY", "CORROBORATED"})


class ClaimCheck(BaseModel):
    """The citation-verifier's finding for one cited claim."""

    claim: str = Field(description="The claim's exact text, quoted from the summary.")
    citation: int | None = Field(
        default=None, description="The [n] marker on that claim, if it carried one."
    )
    support: Support = Field(
        description=(
            "PRIMARY: the archive holds the claim's original source (the paper, "
            "the filing, the official results page, or the organisation's own "
            "announcement of what it itself did, attributed to it in the claim). "
            "CORROBORATED: two or more independent archived sources state it. "
            "SINGLE_SOURCE: only one secondary source states it. UNSUPPORTED: no "
            "archived source states it, the [n] points to the wrong source, or "
            "the claim overstates what the sources say."
        )
    )
    sources: list[Source] = Field(
        default_factory=list,
        description=(
            "The archived sources that back the claim: the primary source, or "
            "every independent source that states it, cited or not. Empty when "
            "UNSUPPORTED."
        ),
    )
    note: str = Field(
        description=(
            "One line. UNSUPPORTED: what the cited source actually says instead. "
            "SINGLE_SOURCE: which source, and why nothing else in the archive "
            "counts. Otherwise: what backs it."
        )
    )


class VerificationResult(BaseModel):
    """What the citation-verifier returns for one topic."""

    topic: str = Field(description="The topic you checked.")
    verdict: Literal["PASS", "FLAG"] = Field(
        description=(
            "FLAG if ANY claim is SINGLE_SOURCE or UNSUPPORTED, otherwise PASS. "
            "Corrected from the claims if it disagrees with them."
        )
    )
    claims: list[ClaimCheck] = Field(
        default_factory=list,
        description="One entry per cited claim in the summary, in order.",
    )

    @model_validator(mode="after")
    def _verdict_follows_the_claims(self) -> VerificationResult:
        # The decision rule lives here, not in the model's judgement. With no
        # claims there is nothing to derive from (an empty archive, say), so the
        # model's own verdict stands.
        if self.claims:
            ok = all(c.support in PUBLISHABLE for c in self.claims)
            self.verdict = "PASS" if ok else "FLAG"
        return self

    @property
    def needs_work(self) -> list[ClaimCheck]:
        """The claims that send this topic back for more research."""
        return [c for c in self.claims if c.support not in PUBLISHABLE]


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
