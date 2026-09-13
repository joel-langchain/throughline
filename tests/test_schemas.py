"""Every subagent verdict the editor branches on is a typed field, not parsed text.

A researcher's KEEP/SKIP decides whether a topic reaches the report, a verifier's
PASS/FLAG decides whether it is re-researched, and a reviewer's APPROVE/REVISE
decides whether the report is rewritten before publishing. When those arrived as
prescribed free text, a dropped colon or a reworded label was a silently wrong
decision rather than a visible error. These tests pin the schemas and their
wiring onto the subagents.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from throughline.agent import citation_verifier, final_pass_reviewer, topic_researcher
from throughline.schemas import (
    FinalPassResult,
    ResearchResult,
    Source,
    UnsupportedClaim,
    VerificationResult,
)

# --- a verdict can only be a verdict ----------------------------------------


@pytest.mark.parametrize(
    ("schema", "field", "good", "bad"),
    [
        (ResearchResult, "verdict", "KEEP", "keep"),
        (ResearchResult, "verdict", "SKIP", "MAYBE"),
        (VerificationResult, "verdict", "FLAG", "flagged"),
        (FinalPassResult, "verdict", "APPROVE", "APPROVED"),
    ],
)
def test_only_the_allowed_verdicts_validate(schema, field, good, bad) -> None:
    base = {
        ResearchResult: {"topic": "t", "reason": "r", "summary": "s"},
        VerificationResult: {"topic": "t"},
        FinalPassResult: {"note": "n"},
    }[schema]
    assert getattr(schema(**base, **{field: good}), field) == good
    with pytest.raises(ValidationError):
        schema(**base, **{field: bad})


def test_a_missing_verdict_is_an_error_not_a_default() -> None:
    # The failure mode being designed out: a reply that simply omits the verdict
    # must fail loudly rather than quietly reading as KEEP or PASS.
    with pytest.raises(ValidationError):
        ResearchResult(topic="t", reason="r", summary="s")
    with pytest.raises(ValidationError):
        VerificationResult(topic="t")


# --- the shapes the editor reads --------------------------------------------


def test_research_result_carries_its_sources_in_citation_order() -> None:
    result = ResearchResult(
        topic="ai-agents",
        verdict="KEEP",
        reason="independent reporting",
        summary="A claim [1] and another [2].",
        sources=[
            Source(title="First", url="https://example.org/a"),
            Source(title="Second", url="https://example.org/b"),
        ],
    )
    assert result.sources[0].url == "https://example.org/a", "[1] is the first source"
    assert [s.title for s in result.sources] == ["First", "Second"]


def test_a_skip_needs_no_sources() -> None:
    result = ResearchResult(topic="t", verdict="SKIP", reason="press release only", summary="")
    assert result.sources == []


def test_a_pass_carries_no_unsupported_claims() -> None:
    assert VerificationResult(topic="t", verdict="PASS").unsupported == []


def test_a_flag_carries_the_claim_and_what_the_source_really_says() -> None:
    result = VerificationResult(
        topic="t",
        verdict="FLAG",
        unsupported=[
            UnsupportedClaim(
                claim="The model scored 84.5%.",
                citation=2,
                what_the_source_says="The source gives no figure.",
            )
        ],
    )
    # The editor re-dispatches using these fields, so each must survive intact.
    (claim,) = result.unsupported
    assert claim.claim == "The model scored 84.5%."
    assert claim.citation == 2
    assert "no figure" in claim.what_the_source_says


def test_an_unsupported_claim_may_have_carried_no_marker() -> None:
    claim = UnsupportedClaim(claim="Unmarked claim.", what_the_source_says="Nothing backs it.")
    assert claim.citation is None


def test_an_approve_carries_no_issues() -> None:
    assert FinalPassResult(verdict="APPROVE", note="ready").issues == []


# --- the schemas describe themselves to the model ---------------------------


def test_every_field_tells_the_model_what_to_put_there() -> None:
    # The model fills these in from the schema alone, so an undescribed field is
    # a field it has to guess at.
    for schema in (ResearchResult, VerificationResult, FinalPassResult, Source, UnsupportedClaim):
        for name, field in schema.model_fields.items():
            assert field.description, f"{schema.__name__}.{name} has no description"


def test_the_verdict_descriptions_state_the_decision_rule() -> None:
    assert "SKIP" in ResearchResult.model_fields["verdict"].description
    assert "FLAG" in VerificationResult.model_fields["verdict"].description


# --- wiring -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "schema"),
    [
        (topic_researcher, ResearchResult),
        (citation_verifier, VerificationResult),
        (final_pass_reviewer, FinalPassResult),
    ],
)
def test_each_subagent_is_wired_to_its_schema(spec, schema) -> None:
    assert spec.get("response_format") is schema, f"{spec['name']} must return {schema.__name__}"


def test_no_subagent_prompt_still_dictates_a_text_format() -> None:
    # A leftover "Return ONLY this, as your reply: VERDICT: ..." block would
    # compete with the schema and reintroduce the parsing it replaced.
    for spec in (topic_researcher, citation_verifier, final_pass_reviewer):
        prompt = spec["system_prompt"]
        assert "VERDICT:" not in prompt, f"{spec['name']} still prescribes a text verdict line"
