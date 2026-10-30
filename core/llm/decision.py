"""Turn agent contract (#36): what the model may hand back, and nothing else.

The model DECIDES; it never writes merchant-facing text. ``TurnDecision`` is claim-only:
  * extra keys are forbidden, so a ``reply`` / ``message`` field is a schema error, not a feature;
  * every claim needs an id and a ``source`` of the form ``tool:...`` / ``kb:...`` / ``policy:...`` (the ids the grounding gate's
    SourceBook uses). That the source EXISTS this turn, and that the claim's numbers match it, is checked by the gate (#43);
  * claims are short English statements of fact. Text that reads like a reply (non-English script, a question, a greeting or an
    address to the merchant, too long) is rejected as a backstop. The structural defence is that claims are never sent: the renderer
    (#47) writes the reply from verified claims.

``decide`` gives the model ONE retry on invalid output. A second failure returns ``escalate_reason="INVALID_OUTPUT"`` and
``invalid_outputs=2``, which is exactly what ``HardTriggers`` reads as ``invalid_output_count`` (INVALID_OUTPUT_TWICE).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from core.engine.workflow import Decision as EngineDecision
from core.llm.client import LLMClient, LLMResponse, LLMUnavailable
from core.llm.sarvam_http import parse_json_loose

LANGUAGES = ("hi-IN", "kn-IN", "en-IN", "hinglish")
_LANG_ALIASES = {"hi": "hi-IN", "kn": "kn-IN", "en": "en-IN", "hi-in": "hi-IN", "kn-in": "kn-IN", "en-in": "en-IN", "hinglish": "hinglish",
                 "hi-latn": "hinglish", "hi_latn": "hinglish"}
SOURCE_RE = re.compile(r"^(tool|kb|policy):\S+$")
_INDIC = re.compile(r"[ऀ-ॿಀ-೿]")
_ADDRESSING = re.compile(r"^\s*(hello|hi|hey|namaste|namaskara|dear|sir|madam|please|thanks?|thank you|sorry|don'?t worry|we will|i will|i'll|let me)\b", re.I)
MAX_CLAIM_CHARS = 240
INVALID_OUTPUT = "INVALID_OUTPUT"
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE: llm"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntentWeight(_Strict):
    intent: str = Field(min_length=1)
    w: float = Field(ge=0.0, le=1.0)


class NewCaseRequest(_Strict):
    playbook_id: str = Field(min_length=1)
    reason: Optional[str] = None


class NextAction(_Strict):
    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class TurnClaim(_Strict):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source: str = Field(min_length=1)

    @field_validator("source")
    @classmethod
    def _source_shape(cls, v: str) -> str:
        if not SOURCE_RE.match(v):
            raise ValueError("source must be tool:<id>, kb:<doc>#<section>@<version> or policy:<RULE-ID>")
        return v

    @field_validator("text")
    @classmethod
    def _claim_not_a_reply(cls, v: str) -> str:
        if _INDIC.search(v):
            raise ValueError("claims_en must be English")
        if "?" in v or "!" in v:
            raise ValueError("a claim is a statement, not a question or exclamation")
        if _ADDRESSING.match(v):
            raise ValueError("a claim must not address the merchant")
        if len(v) > MAX_CLAIM_CHARS:
            raise ValueError(f"a claim is one short fact (max {MAX_CLAIM_CHARS} characters)")
        return v


class TurnDecision(_Strict):
    language: Literal["hi-IN", "kn-IN", "en-IN", "hinglish"]
    intents: list[IntentWeight] = Field(default_factory=list)
    focus_case: Optional[str] = None
    new_case_requests: list[NewCaseRequest] = Field(default_factory=list)
    next_action: Optional[NextAction] = None
    claims_en: list[TurnClaim] = Field(default_factory=list)
    ask_clarification: bool = False

    @field_validator("language", mode="before")
    @classmethod
    def _language_codes(cls, v: Any) -> Any:
        return _LANG_ALIASES.get(v.strip().lower(), v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _unique_claim_ids(self) -> "TurnDecision":
        ids = [c.id for c in self.claims_en]
        if len(ids) != len(set(ids)):
            raise ValueError("claim ids must be unique")
        return self


def response_format() -> dict:
    """json_schema mode with reasoning off is what the Sarvam spike found reliable (config/sarvam.yaml sets reasoning off)."""
    return {"type": "json_schema", "json_schema": {"name": "decision", "schema": TurnDecision.model_json_schema()}}


class InvalidDecision(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _errors(e: ValidationError) -> list[str]:
    # location + message only: pydantic's 'input' would echo untrusted model/merchant text into logs and retry prompts
    return [f"{'.'.join(str(p) for p in x['loc']) or 'root'}: {x['msg']}" for x in e.errors()][:8]


def parse_decision(text: str) -> TurnDecision:
    try:
        obj = parse_json_loose(text)
    except ValueError:
        raise InvalidDecision(["root: reply is not valid JSON"]) from None
    if not isinstance(obj, dict):
        raise InvalidDecision(["root: expected a JSON object"])
    try:
        return TurnDecision.model_validate(obj)
    except ValidationError as e:
        raise InvalidDecision(_errors(e)) from None


@dataclass
class DecisionResult:
    decision: TurnDecision | None
    invalid_outputs: int = 0                    # feed to HardTriggers as facts["invalid_output_count"]
    escalate_reason: str | None = None          # INVALID_OUTPUT after two invalid replies
    outcome_error: str | None = None            # 'UPSTREAM_UNAVAILABLE: llm' when no provider answered (HardTriggers TOOL_FAILURE)
    errors: list[str] = field(default_factory=list)
    responses: list[LLMResponse] = field(default_factory=list)


def decide(client: LLMClient, messages: list[dict], *, case_id: str | None = None, ledger=None) -> DecisionResult:
    """Ask for a decision; one retry on invalid output; never raises for model misbehaviour."""
    res = DecisionResult(None)
    msgs = list(messages)
    for attempt in (1, 2):
        try:
            r = client.complete(msgs, case_id=case_id, purpose="decision", response_format=response_format())
        except LLMUnavailable:
            res.outcome_error = UPSTREAM_UNAVAILABLE
            return res
        res.responses.append(r)
        try:
            res.decision = parse_decision(r.text)
        except InvalidDecision as e:
            res.invalid_outputs += 1
            res.errors = e.errors
            _ledger(ledger, case_id, r, {"valid": False, "attempt": attempt, "errors": e.errors})
            # the retry carries the error locations only; the invalid reply itself is not repeated back to the model
            msgs = list(messages) + [{"role": "user", "content": "INVALID_OUTPUT: " + "; ".join(e.errors) + ". Reply again with exactly one JSON object that follows the schema."}]
            continue
        _ledger(ledger, case_id, r, {"valid": True, "attempt": attempt, "decision": res.decision.model_dump(mode="json")})
        return res
    res.escalate_reason = INVALID_OUTPUT
    return res


def _ledger(ledger, case_id, r: LLMResponse, payload: dict) -> None:
    if ledger is not None:
        ledger.append(kind="DECISION", actor="SYSTEM", case_id=case_id, model_id=r.model_id, prompt_hash=r.prompt_hash, payload=payload)


def to_engine_decision(d: TurnDecision) -> EngineDecision:
    """The engine's own Decision: action only. There is no message here by design; the reply is rendered later from verified claims."""
    return EngineDecision(action=(d.next_action.name, dict(d.next_action.args)) if d.next_action else None)
