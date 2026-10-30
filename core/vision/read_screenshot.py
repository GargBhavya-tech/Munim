"""Reading a payment screenshot (#86, Bible section 23): fields only, never a verdict.

A vision-capable model extracts ``{amount, time, reference, app}`` with a confidence per field. The result is a DRAFT claim: code checks it
against the merchant's own transactions, and only code decides real / fake. The schema forbids extra keys, so a model that adds "verdict",
"looks_fake" or any opinion is rejected outright (the named mistake: letting the vision model say "this looks fake").
The image is never stored: only the extracted fields leave this module.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MIN_FIELD_CONFIDENCE = 0.8          # [ASSUMPTION] below this a field counts as unread; the merchant is asked instead


class Confidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: float = Field(ge=0.0, le=1.0, default=0.0)
    time: float = Field(ge=0.0, le=1.0, default=0.0)
    reference: float = Field(ge=0.0, le=1.0, default=0.0)
    app: float = Field(ge=0.0, le=1.0, default=0.0)


class ScreenshotFields(BaseModel):
    """What the model may return. Amount in integer paise; time as HH:MM (24h) as printed; nothing else."""
    model_config = ConfigDict(extra="forbid")
    amount_paise: Optional[int] = Field(default=None, gt=0)
    time_hhmm: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    reference: Optional[str] = Field(default=None, min_length=4, max_length=40)
    app: Optional[str] = Field(default=None, max_length=30)
    confidence: Confidence = Field(default_factory=Confidence)


@dataclass(frozen=True)
class DraftReading:
    """A DRAFT claim about a screenshot: fields below the confidence floor are None (unread)."""
    amount_paise: int | None = None
    time_hhmm: str | None = None
    reference: str | None = None
    app: str | None = None
    raw_confidence: dict = field(default_factory=dict)
    source: str = "vision"

    @property
    def low_confidence(self) -> list[str]:
        return [k for k, v in self.raw_confidence.items() if v < MIN_FIELD_CONFIDENCE]

    def as_payload(self) -> dict:
        return {"amount_paise": self.amount_paise, "time_hhmm": self.time_hhmm, "reference": self.reference, "app": self.app,
                "confidence": dict(self.raw_confidence), "source": self.source}


class InvalidReading(ValueError):
    pass


def to_draft(fields: ScreenshotFields, source: str = "vision") -> DraftReading:
    c = fields.confidence.model_dump()
    keep = lambda name, v: v if c.get(name, 0.0) >= MIN_FIELD_CONFIDENCE else None      # noqa: E731
    return DraftReading(keep("amount", fields.amount_paise), keep("time", fields.time_hhmm), keep("reference", fields.reference),
                        keep("app", fields.app), c, source)


def parse_reading(obj: dict, source: str = "vision") -> DraftReading:
    try:
        return to_draft(ScreenshotFields.model_validate(obj), source)
    except ValidationError as e:
        raise InvalidReading("; ".join(f"{'.'.join(map(str, x['loc'])) or 'root'}: {x['msg']}" for x in e.errors())[:300]) from None


class ScreenshotReader(Protocol):
    def read(self, image: bytes, mime: str = "image/png") -> DraftReading: ...


PROMPT = ("Read this payment screenshot. Return ONLY a JSON object with keys amount_paise (integer, rupees x 100), time_hhmm (HH:MM, 24h), "
          "reference (the UPI reference / UTR exactly as printed), app (the app name) and confidence (an object with a 0..1 number for each of "
          "amount, time, reference, app). Use null for anything you cannot read. Do not judge whether the payment is real.")


class LLMVisionReader:
    """Any OpenAI-compatible vision chat endpoint behind core.llm.client. One retry on invalid output, then nothing is read (the caller asks)."""

    def __init__(self, client, *, case_id: str | None = None):
        self.client, self.case_id = client, case_id

    def read(self, image: bytes, mime: str = "image/png") -> DraftReading:
        from core.llm.sarvam_http import parse_json_loose
        url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
        msgs = [{"role": "user", "content": [{"type": "text", "text": PROMPT}, {"type": "image_url", "image_url": {"url": url}}]}]
        for _ in (1, 2):
            r = self.client.complete(msgs, case_id=self.case_id, purpose="vision")
            try:
                return parse_reading(parse_json_loose(r.text))
            except (InvalidReading, ValueError):
                continue
        return DraftReading(raw_confidence={"amount": 0.0, "time": 0.0, "reference": 0.0, "app": 0.0}, source="vision_invalid")


_SVG_TEXT = re.compile(r"<text[^>]*\bid=\"(amount|time|reference|app)\"[^>]*>([^<]*)</text>")
_BLUR = re.compile(r"\bdata-blur=\"(amount|time|reference|app)\"")


class SyntheticSvgReader:
    """Reads the pack's SYNTHETIC screenshots (SVG with labelled text). It stands in for the vision model in tests and the offline demo;
    a ``data-blur`` attribute lowers that field's confidence, the way a smudged photo would."""

    def read(self, image: bytes, mime: str = "image/svg+xml") -> DraftReading:
        svg = image.decode("utf-8")
        found = dict(_SVG_TEXT.findall(svg))
        blurred = set(_BLUR.findall(svg))
        amount = None
        if "amount" in found:
            digits = re.sub(r"[^\d.]", "", found["amount"])
            if digits:
                amount = int(round(float(digits) * 100))
        conf = {k: (0.4 if k in blurred else 0.97) if k in found else 0.0 for k in ("amount", "time", "reference", "app")}
        return to_draft(ScreenshotFields(amount_paise=amount, time_hhmm=found.get("time"), reference=found.get("reference"), app=found.get("app"),
                                         confidence=Confidence(**conf)), source="synthetic_svg")
