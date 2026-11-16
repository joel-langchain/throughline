"""A narrow model check for three logic errors the final-pass reviewer misses.

The 16 Nov 2026 report stated a valuation "implied" by the shares on sale that
the arithmetic does not give, called one legislative window both "narrower" and
the "best", and headed a section "AI Is Corrupting Scientific Publishing" over a
body whose evidence was mixed. The final-pass reviewer had NUMBERS, HEADINGS, and
contradiction checks in its prompt and caught none of the three in three runs on
Haiku or on Sonnet. A reviewer asked for one verdict on a whole report reads for
the gist. These errors only show when someone does the arithmetic, sets two
statements side by side, and holds each heading against its body.

So this check does only those three things, and the model returns its working
rather than a verdict: each derived figure with its inputs and arithmetic, each
uncited sentence set against the cited sentence about the same thing, each
subject that two cited sentences describe, and each heading with what its
section shows and the sentence that cuts against it. Code turns the failing
items into findings. Every finding quotes the report, and a quote that is not in
the report (for a heading, not in its own section) is dropped, so a finding
always points at text the editor can find.

The uncited sentences are found in code and listed for the model by number. They
are the editor's own words and where the 16 Nov contradiction was, and in
development the model could not reliably tell which sentences carry a citation.

It is four Sonnet calls run at once, one per part, so the check takes as long
as its slowest call. It runs before publish, alongside the final-pass reviewer,
and its findings join the reviewer's issues (see `LogicCheckMiddleware` in
agent.py). It starts when the editor writes the report and is collected when
the reviewer returns, so it adds time to a run only for as long as it outlasts
the reviewer.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Literal

from langchain_core.runnables.config import ContextThreadPoolExecutor
from pydantic import BaseModel, Field

logger = logging.getLogger("throughline.logic_check")

Kind = Literal["derived_figure", "contradiction", "heading"]
KINDS: tuple[Kind, ...] = ("derived_figure", "contradiction", "heading")

# Contradictions take two calls: uncited summary sentences against the cited
# ones, and cited sentences against each other. Split, no call runs long.
Call = Literal["derived_figure", "summary", "repeated", "heading"]
CALLS: dict[Call, Kind] = {
    "derived_figure": "derived_figure",
    "summary": "contradiction",
    "repeated": "contradiction",
    "heading": "heading",
}

# How long to wait for the check once the reviewer has returned. It starts when
# the report is written and took 20 to 27 seconds on past reports, about as long
# as the reviewer, so the wait is usually a few seconds. A check that hangs must
# not hold the run, so after this it is skipped for that review.
WAIT_SECONDS = 30.0

_CITATION = re.compile(r"\[\[\s*https?://[^\]\s]+?\s*\]\]|\[\d+\]")
_SOURCES = re.compile(r"^##\s+Sources\b", re.MULTILINE)
_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_TAG = re.compile(r"\s*\((?:new|developing)\)\s*$", re.IGNORECASE)
_ELLIPSIS = re.compile(r"\.\.\.|…")
# A sentence ends at . ! or ? (after any closing quote or bracket) before a
# capital or a digit. Abbreviations that end in a full stop are rejoined.
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)]*\s+(?=[\"“(]?[A-Z0-9$])")
_ABBREVIATION = re.compile(
    r"\b(?:vs|e\.g|i\.e|etc|approx|Inc|Corp|Ltd|Co|No|Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr"
    r"|Rep|Reps|Sen|Sens|Gov|Gen|U\.S|U\.K|H\.R|S)\.$"
)
# An uncited fragment shorter than this is a label, not a claim.
_MIN_SENTENCE = 25
_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
# A quote fragment shorter than this could match by chance.
_MIN_FRAGMENT = 12


_PREAMBLE = """You check one weekly AI-news report for one kind of logic error.
You do not check facts against the world or the sources, and you do not judge
style or tone. You only check whether the report agrees with itself. A number in
square brackets, such as [4], marks a sentence that cites a source. A sentence
without one is the editor's own words."""

_CLOSING = """Copy every sentence and heading exactly as it appears in the report,
including its [n] markers. Keep every other field to one short sentence. List
items that pass as well as items that fail."""

_CONFLICT = """Two sentences conflict when they put the same thing in opposite
directions (more and less, wider and narrower, rising and falling, ahead and
behind, better and worse, easier and harder, first and not first) or give it two
different values, so that a reader who meets both cannot tell which is right.
Two sentences that rate something with the same word (a window, a lead, a
gap, a cost) speak to the same thing, even when one names a single case of it,
unless the report makes clear that they describe different things, different
times, or different measures. Read each sentence's grammar, including what "it"
or "they" refers to."""

_TASKS: dict[Call, str] = {
    "derived_figure": """DERIVED FIGURES. List every figure the report presents as
worked out from other figures it states: a total, a difference, a share or
percentage, a per-unit amount, or a figure introduced by words such as
"implying", "meaning", "which puts", "giving", "a total of", "each", or "up
from". For each, copy the input figures, do the arithmetic, and say whether the
stated figure follows. It follows when the arithmetic on those inputs gives that
figure, allowing for rounding. It does not follow when the arithmetic gives a
different number, or a different kind of quantity from the one the sentence
names. When the report does not state every figure the arithmetic needs, as
with a comparison a source reports ("twice as fast", "20% cheaper"), the
verdict is "inputs not stated": that is the source's figure, not the report's
working. But when the sentence itself presents the figure as following from the
figures it gives ("implying", "meaning", "which puts"), judge it on those
figures alone, even if some other figure would make it right.""",
    "summary": f"""SUMMARY SENTENCES. After the report comes a numbered list of
its body sentences that carry no [n] marker. These are the editor's own words,
written from the cited sentences, so they are where a report most often
disagrees with itself. For every numbered sentence, say whether it says how
something stands: how large, how fast, how likely, how good or bad, which comes
first, whether it is rising or falling, or how an outlook, opening, or risk
stands. If it does, name that thing in the sentence's own words, then find
every cited sentence in the report, in the same section or elsewhere, that
speaks to the same thing or to one case of it. Copy
the one that agrees with the numbered sentence least, and say whether the two
agree.

{_CONFLICT}""",
    "repeated": f"""REPEATED SUBJECTS. List each thing that two cited sentences in
the body describe, in one section or across sections; headings are not part of
this check. Copy both and say whether they agree.

{_CONFLICT}""",
    "heading": """HEADINGS. For every section heading, say what it asserts as
established (a cause, an effect, a trend, a verdict, or a ranking), or "names a
topic" when it asserts nothing. Then say what the section's own body shows about
that assertion, and copy the sentence in that section's body that cuts against
it most, if one does. The heading is unsupported when it states as established
something the body shows as partial, mixed, disputed, unconfirmed, or true of
fewer cases than the heading implies, or when the body gives evidence against
it. Mark it unsupported only when a reader who saw just the heading would come
away believing something the section does not show, and you can point to the
sentence that shows it. A heading that names a topic is supported. Judge the
heading only: a figure in the body that does not add up, or two body sentences
that disagree, is checked separately and does not count against the heading.""",
}


def system_prompt(call: Call) -> str:
    """The judge's instructions for one call."""
    return f"{_PREAMBLE}\n\n{_TASKS[call]}\n\n{_CLOSING}"


class DerivedFigure(BaseModel):
    sentence: str = Field(description="The sentence, copied exactly from the report.")
    stated: str = Field(description="The figure the sentence works out, as written.")
    inputs: str = Field(description="The figures in the report it is worked out from.")
    working: str = Field(description="The arithmetic on those inputs, and its result.")
    verdict: Literal["follows", "does not follow", "inputs not stated"] = Field(
        description=(
            "'follows' if the working gives the stated figure, allowing for rounding; "
            "'does not follow' if it gives a different number or kind of quantity; "
            "'inputs not stated' if the report does not state every figure it needs."
        )
    )


class DerivedFigures(BaseModel):
    """The judge's working on derived figures. Findings are derived from it in code."""

    items: list[DerivedFigure] = Field(default_factory=list)


class SummaryCheck(BaseModel):
    number: int = Field(description="The uncited sentence's number in the list.")
    rates: str = Field(
        description=(
            "What it says how something stands about, in the sentence's own words, "
            "or an empty string if it says nothing of that kind."
        )
    )
    cited: str = Field(
        description=(
            "Of the cited sentences about the same thing, the one that agrees with "
            "the numbered sentence least, copied exactly. An empty string if the "
            "numbered sentence says nothing of how something stands."
        )
    )
    why: str = Field(description="Whether the two agree, and why.")
    conflict: bool = Field(description="True if the two cannot both be right.")


class Summaries(BaseModel):
    """The judge's working on summary sentences. Findings are derived from it in code."""

    items: list[SummaryCheck] = Field(default_factory=list)


class RepeatedSubject(BaseModel):
    subject: str = Field(description="What both sentences describe.")
    first: str = Field(description="One cited body sentence about it, copied exactly.")
    second: str = Field(
        description="Another cited body sentence about it, copied exactly."
    )
    why: str = Field(description="Whether the two agree, and why.")
    conflict: bool = Field(description="True if the two cannot both be right.")


class RepeatedSubjects(BaseModel):
    """The judge's working on repeated subjects. Findings are derived from it in code."""

    items: list[RepeatedSubject] = Field(default_factory=list)


class HeadingCheck(BaseModel):
    heading: str = Field(
        description="The heading text, copied exactly, without '##' or a (new)/(developing) tag."
    )
    asserts: str = Field(
        description="What the heading states as established, or 'names a topic'."
    )
    body_shows: str = Field(description="What the section's own body shows about that.")
    against: str = Field(
        description=(
            "The sentence in this section's body that cuts against the assertion "
            "most, copied exactly, or an empty string if none does."
        )
    )
    supported: bool = Field(
        description="True if the body bears out what the heading asserts."
    )


class Headings(BaseModel):
    """The judge's working on headings. Findings are derived from it in code."""

    items: list[HeadingCheck] = Field(default_factory=list)


Working = DerivedFigures | Summaries | RepeatedSubjects | Headings
SCHEMAS: dict[Call, type[BaseModel]] = {
    "derived_figure": DerivedFigures,
    "summary": Summaries,
    "repeated": RepeatedSubjects,
    "heading": Headings,
}


@dataclass(frozen=True)
class LogicFinding:
    kind: Kind
    quote: str
    detail: str

    def issue(self) -> str:
        """The finding as a line in the final-pass reviewer's issues."""
        if self.kind == "derived_figure":
            return (
                f'Logic check, derived figure. "{self.quote}" The figure does not follow '
                f"from its inputs ({self.detail}). Correct it or cut the derivation."
            )
        if self.kind == "contradiction":
            return (
                f"Logic check, contradiction. {self.quote} ({self.detail}). Make the "
                "two agree with their sources, or cut one."
            )
        return (
            f'Logic check, heading. "{self.quote}" claims more than its section shows '
            f"({self.detail}). Reword the heading to say what the section shows."
        )


# --- the text the judge reads -----------------------------------------------


def judge_text(report: str) -> str:
    """The report body as the reader sees it, without the Sources list.

    The draft cites with [[url]] markers that are renumbered to [n] at publish.
    The judge needs to know which sentences cite a source and which are the
    editor's own, not where they point, so every marker becomes [n] numbered by
    first appearance and the Sources list is left out.
    """
    body = _SOURCES.split(report, maxsplit=1)[0]
    numbers: dict[str, int] = {}

    def number(match: re.Match[str]) -> str:
        marker = match.group(0)
        if marker[1].isdigit():
            return marker
        url = marker.strip("[] ")
        return f"[{numbers.setdefault(url, len(numbers) + 1)}]"

    body = _CITATION.sub(number, body)
    return re.sub(r"[ \t]{2,}", " ", body).strip()


def _fold(text: str) -> str:
    """Text for comparing quotes: markers, curly quotes, case, and spacing ignored."""
    text = _CITATION.sub(" ", text.translate(_FOLD)).lower()
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    return " ".join(text.split()).strip(" .\"'")


def _in_text(quote: str, folded_text: str) -> bool:
    """True if the quote is in the text, allowing for elided middles ("...")."""
    fragments = [_fold(f) for f in _ELLIPSIS.split(quote)]
    fragments = [f for f in fragments if f]
    if not fragments or max(len(f) for f in fragments) < _MIN_FRAGMENT:
        return False
    return all(f in folded_text for f in fragments if len(f) >= _MIN_FRAGMENT)


def _sections(text: str) -> dict[str, str]:
    """Each section's body, keyed by its heading without the (new) tag, all folded."""
    parts = _HEADING.split(text)
    # split() with one group gives [preamble, heading, body, heading, body, ...]
    return {_fold(_TAG.sub("", h)): _fold(b) for h, b in zip(parts[1::2], parts[2::2])}


def _clean(quote: str) -> str:
    return " ".join(_CITATION.sub(" ", quote).split()).replace(" .", ".").strip()


def _sentences(paragraph: str) -> list[str]:
    pieces = _SENTENCE_END.split(paragraph)
    out: list[str] = []
    for piece in pieces:
        if out and _ABBREVIATION.search(out[-1]):
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return [p.strip() for p in out if p.strip()]


def uncited_sentences(report: str) -> list[str]:
    """The body sentences that carry no citation marker: the editor's own words.

    Found in code, not by the judge: in development Sonnet listed cited
    sentences as uncited and skipped an uncited one that contradicted its
    section.
    """
    found: list[str] = []
    for line in judge_text(report).splitlines():
        line = line.strip().lstrip("-*> ").strip()
        if not line or line.startswith("#"):
            continue
        for sentence in _sentences(line):
            if not _CITATION.search(sentence) and len(sentence) >= _MIN_SENTENCE:
                found.append(sentence)
    return found


def judge_input(report: str, call: Call) -> str | None:
    """What the judge reads for one call, or None when the call has nothing to do."""
    text = judge_text(report)
    if call != "summary":
        return text
    uncited = uncited_sentences(report)
    if not uncited:
        return None
    listed = "\n".join(f"{n}. {sentence}" for n, sentence in enumerate(uncited, 1))
    return f"{text}\n\n---\nUNCITED SENTENCES\n{listed}"


def findings_from(working: Working, report: str) -> list[LogicFinding]:
    """The failing items in the judge's working that quote the report correctly."""
    text = judge_text(report)
    folded = _fold(text)
    found: list[LogicFinding] = []
    dropped = 0
    if isinstance(working, DerivedFigures):
        for item in working.items:
            if item.verdict != "does not follow":
                continue
            if _in_text(item.sentence, folded):
                found.append(
                    LogicFinding("derived_figure", _clean(item.sentence), item.working)
                )
            else:
                dropped += 1
    elif isinstance(working, Summaries | RepeatedSubjects):
        if isinstance(working, Summaries):
            uncited = uncited_sentences(report)
            pairs = [
                (uncited[i.number - 1], i.cited, i.why)
                for i in working.items
                if i.conflict and 0 < i.number <= len(uncited)
            ]
        else:
            pairs = [(i.first, i.second, i.why) for i in working.items if i.conflict]
        for first, second, why in pairs:
            if _in_text(first, folded) and _in_text(second, folded):
                quote = f'"{_clean(first)}" and "{_clean(second)}"'
                found.append(LogicFinding("contradiction", quote, why))
            else:
                dropped += 1
    elif isinstance(working, Headings):
        # An unsupported heading must quote the sentence in its own section
        # that cuts against it, so the editor can see why.
        sections = _sections(text)
        for item in working.items:
            # A heading that names a topic claims nothing to overreach.
            if item.supported or _fold(item.asserts).rstrip(".") == "names a topic":
                continue
            body = sections.get(_fold(_TAG.sub("", item.heading.lstrip("# "))))
            if body is not None and item.against and _in_text(item.against, body):
                detail = f'{item.body_shows} The section says "{_clean(item.against)}"'
                found.append(LogicFinding("heading", item.heading.strip(), detail))
            else:
                dropped += 1
    if dropped:
        logger.info("dropped %d finding(s) whose quote is not in the report", dropped)
    return found


# --- the model calls --------------------------------------------------------


def _judge(call: Call):
    """The judge for one call: the editor's model, with typed output.

    Sonnet, not the workers' Haiku. In development Haiku listed figures that a
    source reports as failed derivations and read a concession ("even though it
    trails") as a contradiction. Native JSON-schema output, not a tool call: with
    a tool call Sonnet sometimes sent a nested list as a string.
    """
    from throughline.models import strong_model

    return strong_model.with_structured_output(
        SCHEMAS[call], method="json_schema", include_raw=True
    ).with_config(run_name=f"logic-check-{call.replace('_', '-')}")


# Tests swap this for a fake judge.
_judge_factory = _judge

# The calls run at once, so a check takes as long as its slowest call.
_calls = ContextThreadPoolExecutor(max_workers=8, thread_name_prefix="logic-call")


def review(report: str, call: Call, judge=None) -> tuple[Working, dict]:
    """One judge call on the report: its working, and the token usage."""
    content = judge_input(report, call)
    if content is None:
        return SCHEMAS[call](), {}
    out = (judge or _judge_factory(call)).invoke(
        [("system", system_prompt(call)), ("human", content)]
    )
    if isinstance(out, BaseModel):  # a judge without include_raw
        return out, {}
    if out.get("parsing_error") or out.get("parsed") is None:
        raise ValueError(
            f"{call} judge reply did not parse: {out.get('parsing_error')}"
        )
    raw = out.get("raw")
    usage = dict(getattr(raw, "usage_metadata", None) or {})
    return out["parsed"], usage


def _check_call(report: str, call: Call) -> list[LogicFinding]:
    working, _usage = review(report, call)
    return findings_from(working, report)


def _submit(report: str, kinds: tuple[Kind, ...]) -> dict[Call, Future]:
    return {
        call: _calls.submit(_check_call, report, call)
        for call, kind in CALLS.items()
        if kind in kinds
    }


def check(report: str, kinds: tuple[Kind, ...] = KINDS) -> list[LogicFinding]:
    """Findings of the given kinds, every call run at once. Raises if any fails."""
    calls = _submit(report, kinds)
    return [finding for call in calls.values() for finding in call.result()]


def score(findings: list[LogicFinding], kind: Kind) -> tuple[float, str]:
    """1.0 when the check found nothing of this kind, else 0.0 with the quotes."""
    hits = [f for f in findings if f.kind == kind]
    if not hits:
        return 1.0, f"no {kind.replace('_', ' ')} errors found"
    return 0.0, "; ".join(f"{f.quote} ({f.detail})" for f in hits)


# --- running it alongside the reviewer --------------------------------------

# One check per distinct draft. Keyed by the text the judge reads, so the
# renumbering step does not start a second check on the same prose.
_executor = ContextThreadPoolExecutor(max_workers=2, thread_name_prefix="logic-check")
_pending: dict[str, Future] = {}
_lock = threading.Lock()
_MAX_PENDING = 4


def _run(report: str) -> list[LogicFinding] | None:
    """The check, never raising: a failed call costs its own kind, not the review."""
    began = time.monotonic()
    calls = _submit(report, KINDS)
    found: list[LogicFinding] = []
    failed: list[str] = []
    for call, pending in calls.items():
        try:
            found += pending.result()
        except Exception as exc:  # noqa: BLE001 - the check is best-effort
            logger.warning("logic check (%s) failed: %s", call, exc)
            failed.append(f"{call}: {type(exc).__name__}: {exc}")
    elapsed = time.monotonic() - began
    for failure in failed:
        print(f"[logic-check] failed, {failure}", flush=True)
    if len(failed) == len(calls):
        return None
    print(f"[logic-check] {len(found)} finding(s) in {elapsed:.1f}s", flush=True)
    return found


def start(report: str) -> Future | None:
    """Start the check on a draft in the background, or join one already running."""
    if not report or not report.strip():
        return None
    key = hashlib.sha256(_fold(judge_text(report)).encode()).hexdigest()
    with _lock:
        pending = _pending.get(key)
        if pending is None:
            pending = _executor.submit(_run, report)
            _pending[key] = pending
            while len(_pending) > _MAX_PENDING:
                _pending.pop(next(iter(_pending)))
    return pending


def collect(
    pending: Future | None, timeout: float = WAIT_SECONDS
) -> list[LogicFinding]:
    """The findings, or [] if the check failed or did not finish in time."""
    if pending is None:
        return []
    try:
        return pending.result(timeout=timeout) or []
    except FutureTimeout:
        print(f"[logic-check] not finished after {timeout:.0f}s, skipped", flush=True)
        return []


async def acollect(
    pending: Future | None, timeout: float = WAIT_SECONDS
) -> list[LogicFinding]:
    """`collect` for the async graph, without blocking the event loop."""
    if pending is None:
        return []
    # asyncio.wait, not wait_for: a timeout here must not cancel the shared check.
    waiting = asyncio.wrap_future(pending)
    done, _ = await asyncio.wait({waiting}, timeout=timeout)
    if not done:
        print(f"[logic-check] not finished after {timeout:.0f}s, skipped", flush=True)
        return []
    return waiting.result() or []
