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
    ClaimCheck,
    FinalPassResult,
    ResearchResult,
    Source,
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


def test_a_pass_with_nothing_checked_carries_no_claims() -> None:
    assert VerificationResult(topic="t", verdict="PASS").claims == []


def test_a_flag_carries_the_claim_and_what_the_source_really_says() -> None:
    result = VerificationResult(
        topic="t",
        verdict="FLAG",
        claims=[
            ClaimCheck(
                claim="The model scored 84.5%.",
                citation=2,
                support="UNSUPPORTED",
                note="The source gives no figure.",
            )
        ],
    )
    # The editor re-dispatches using these fields, so each must survive intact.
    (claim,) = result.to_research
    assert claim.claim == "The model scored 84.5%."
    assert claim.citation == 2
    assert "no figure" in claim.note


def test_an_unsupported_claim_may_have_carried_no_marker() -> None:
    claim = ClaimCheck(claim="Unmarked claim.", support="UNSUPPORTED", note="Nothing backs it.")
    assert claim.citation is None
    assert claim.sources == []


def test_an_approve_carries_no_issues() -> None:
    assert FinalPassResult(verdict="APPROVE", note="ready").issues == []


# --- the schemas describe themselves to the model ---------------------------


def test_every_field_tells_the_model_what_to_put_there() -> None:
    # The model fills these in from the schema alone, so an undescribed field is
    # a field it has to guess at.
    for schema in (ResearchResult, VerificationResult, FinalPassResult, Source, ClaimCheck):
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


# --- corroboration: the verdict follows the claim grades ---------------------


def _check(support: str, *urls: str) -> ClaimCheck:
    return ClaimCheck(
        claim=f"A {support.lower()} claim.",
        citation=1,
        support=support,
        sources=[Source(title=u, url=u) for u in urls],
        note="n",
    )


def test_an_unsupported_claim_flags_the_topic_whatever_the_model_said() -> None:
    # The failure designed out: a model that grades a claim unsupported and
    # still answers PASS. Code derives the verdict, so the topic goes back.
    result = VerificationResult(
        topic="t",
        verdict="PASS",
        claims=[_check("CORROBORATED", "https://a.example/1", "https://b.example/2"),
                _check("UNSUPPORTED")],
    )
    assert result.verdict == "FLAG"
    assert [c.support for c in result.to_research] == ["UNSUPPORTED"]


def test_a_single_source_claim_is_left_out_without_more_research() -> None:
    # Re-researching single-source claims would add searches and model calls to
    # every run. They are dropped from the report instead, so grading costs no
    # extra research: only UNSUPPORTED sends a topic back, as before.
    result = VerificationResult(
        topic="t",
        verdict="FLAG",
        claims=[_check("PRIMARY", "https://arxiv.org/abs/1"),
                _check("SINGLE_SOURCE", "https://c.example/3")],
    )
    assert result.verdict == "PASS"
    assert result.to_research == []
    assert [c.support for c in result.to_leave_out] == ["SINGLE_SOURCE"]


def test_primary_or_corroborated_claims_pass_even_if_the_model_flagged() -> None:
    result = VerificationResult(
        topic="t",
        verdict="FLAG",
        claims=[_check("PRIMARY", "https://arxiv.org/abs/1"),
                _check("CORROBORATED", "https://a.example/1", "https://b.example/2")],
    )
    assert result.verdict == "PASS"
    assert result.to_leave_out == []


def test_the_verdict_is_derived_when_parsed_from_a_model_reply() -> None:
    # deepagents parses the subagent's structured reply through the schema, so
    # the derivation has to hold on that path too, not only on construction.
    reply = (
        '{"topic": "t", "verdict": "PASS", "claims": [{"claim": "c", "citation": 1, '
        '"support": "UNSUPPORTED", "sources": [], '
        '"note": "the source gives no date"}]}'
    )
    assert VerificationResult.model_validate_json(reply).verdict == "FLAG"


def test_support_can_only_be_one_of_the_four_levels() -> None:
    with pytest.raises(ValidationError):
        ClaimCheck(claim="c", support="LIKELY", note="n")


def test_the_verifier_is_told_what_independent_means() -> None:
    prompt = citation_verifier["system_prompt"]
    for level in ("PRIMARY", "CORROBORATED", "SINGLE_SOURCE", "UNSUPPORTED"):
        assert level in prompt
    assert "counts once" in prompt, "a republished story must not count as a second source"
    assert "WHOLE archive" in prompt, "corroboration may come from uncited archive results"


def test_the_editor_leaves_single_source_claims_out_instead_of_researching_them() -> None:
    from throughline.agent import EDITOR_PROMPT

    assert "Do NOT re-research SINGLE_SOURCE" in EDITOR_PROMPT
    assert "Leave every SINGLE_SOURCE claim out of the report" in EDITOR_PROMPT
