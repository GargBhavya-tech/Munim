"""Account briefs (#100; Bible section 10): what a rep or the outreach job reads about a lead BEFORE any contact.

Sections: who they are, what they use now, what they probably need, a pitch hypothesis (one main, two fallbacks), likely objections with the
approved answers, and the best channel / language / time.

The rule that matters: EVERY NUMBER IN A BRIEF CITES A SOURCE AND PASSES THE GATE'S NUMBERS CHECK.
* A statement that carries a number is a ``Claim`` (core.gate.sources) with a source id: ``tool:lead_record#<lead>`` (the lead's stored record),
  ``tool:turnover_estimate#<lead>`` (jobs.scoring.estimate_turnover, code), ``kb:<doc>#<section>@v<n>`` (approved KB text) or ``policy:<RULE>``.
  ``Brief.check()`` runs core.gate.sources.check_claims on all of them; ``build`` refuses to return a brief that fails.
* Everything else (needs, pitch labels, notes) is free text that may contain NO number at all (``extract_numbers`` finds none), so a number can
  never ride in on a hypothesis. Turnover is never invented: with no signal the brief says "unknown" and shows no figure; with one signal it
  is shown as a wide, uncertain band; a figure that is shown is exactly the scoring job's, in rupees converted from integer paise.
* What the data does not hold (competitor QR in use, years in business, outlets) is listed under ``gaps``, not guessed.
The grounding gate's entailment step needs the NLI model and is not run here; this is the source-exists and numbers-match check (G2).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from core.engine.clock import SimClock
from core.engine.contact_budget import QUIET_END_H, QUIET_START_H
from core.gate.numbers import extract_numbers
from core.gate.sources import Claim, ClaimCheck, SourceBook, check_claims
from core.memory.knowledge import parse_doc
from core.memory.store import write_txn
from jobs.outreach import SEGMENT_CHANNELS, segment_for
from jobs.scoring import SLAB_ORDINAL, area_busyness, estimate_turnover

KB_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "kb"
OBJECTION_IDS = ("charges_too_high", "already_using_other", "device_rent", "trust")
PITCHES = {                                              # id -> (label with no number, KB doc, KB section, sentence index that is quoted)
    "qr_basic": ("Start with the free QR code plan", "fee_schedule", "soundbox_plans", 2),
    "soundbox_standard": ("Soundbox Standard, announcing every payment aloud", "fee_schedule", "soundbox_plans", 0),
    "soundbox_plus": ("Soundbox Plus for a busier counter", "fee_schedule", "soundbox_plans", 1),
}
PITCH_ORDER = {0: ("qr_basic", "soundbox_standard", "soundbox_plus"), 1: ("soundbox_standard", "qr_basic", "soundbox_plus"),
               2: ("soundbox_plus", "soundbox_standard", "qr_basic"), 3: ("soundbox_plus", "soundbox_standard", "qr_basic")}   # by GST slab ordinal [ASSUMPTION]
NEEDS = {                                                # category -> hypotheses (no numbers allowed)
    "kirana": "Many small payments through the day; a Soundbox lets the counter confirm each one without looking at a phone.",
    "restaurant": "Busy counter and table payments; confirming each payment aloud saves staff time.",
    "pharmacy": "Steady walk-in payments of mixed size; clear confirmation reduces disputes at the counter.",
    "salon": "Fewer, larger payments; a QR code that works at the chair is the first need.",
    "electronics": "Larger tickets; a reliable confirmation and settlement history matter more than speed.",
    "bakery": "Morning rush of small payments; an audible confirmation keeps the queue moving.",
    "d2c": "Online checkout success and settlement reliability; this is a conversation for a person, not a shop pitch.",
}
CATEGORY_LABEL = {"d2c": "direct-to-consumer online"}      # "d2c" would read as the number 2
SEGMENT_LABEL = {"d2c": "online business", "offline": "offline shop"}
SLAB_LABEL = {"<40L": "under 40L", "40L-1.5Cr": "40L to 1.5Cr", "1.5Cr-5Cr": "1.5Cr to 5Cr", ">5Cr": "over 5Cr"}

SCHEMA = "CREATE TABLE IF NOT EXISTS lead_briefs (lead_id TEXT PRIMARY KEY, body_json TEXT NOT NULL, built_at TEXT NOT NULL)"


class BriefRefused(ValueError):
    """The lead may not be briefed for outreach (an existing merchant, a duplicate, or not cleared)."""


class BriefGateError(ValueError):
    """A claim in the brief has no source or a number that is not in its source. A brief like that is never returned."""


def rupees(paise: int) -> str:
    """Integer paise to rupees in Indian digit grouping (a unit conversion, not arithmetic on a business figure)."""
    r = int(paise) // 100
    s = str(r)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


@dataclass(frozen=True)
class Objection:
    """An approved answer, quoted VERBATIM from its KB section. It is checked by exact equality with that section (stronger than the numbers
    check), so it needs no claim; the quote is the source."""
    key: str
    source: str
    text: str


@dataclass
class Brief:
    lead_id: str
    name: str
    who: list[Claim]
    uses_now: list[str]
    needs: list[str]
    pitch: list[tuple[str, str, Claim]]          # (rank, label, KB claim); rank is "main" / "fallback 1" / "fallback 2"
    objections: list[Objection]
    contact: list[Claim]
    contact_notes: list[str]
    gaps: list[str]
    book: SourceBook = field(repr=False, default_factory=SourceBook)

    def claims(self) -> list[Claim]:
        return [*self.who, *(p[2] for p in self.pitch), *self.contact]

    def free_text(self) -> list[str]:
        return [*self.uses_now, *self.needs, *(f"{r}: {l}" for r, l, _ in self.pitch), *self.contact_notes, *self.gaps]

    def check(self) -> list[ClaimCheck]:
        return check_claims(self.claims(), self.book)

    def quote_errors(self) -> list[str]:
        """Objection answers that are not exactly the text of the KB section they cite."""
        out = []
        for o in self.objections:
            ok, body = self.book.get(o.source)
            if not ok or body != o.text:
                out.append(o.key)
        return out

    def unsourced_numbers(self) -> list[str]:
        """Numbers that appear in free text (they may not), plus numbers in a claim that its source does not hold."""
        bad = [n.raw for t in self.free_text() for n in extract_numbers(t, english_one=False)]
        for c in self.check():
            bad += c.unmatched_numbers
        return bad

    @property
    def passes_gate(self) -> bool:
        return all(c.ok for c in self.check()) and not self.unsourced_numbers() and not self.quote_errors()

    def to_text(self) -> str:
        lines = [f"ACCOUNT BRIEF  {self.lead_id}  {self.name}  (SIMULATED)", "", "Who they are"]
        lines += [f"  - {c.text}  [{c.source}]" for c in self.who]
        lines += ["What they use now", *[f"  - {t}" for t in self.uses_now], "What they probably need (hypothesis)", *[f"  - {t}" for t in self.needs]]
        lines += ["Pitch hypothesis", *[f"  - {r}: {l}. {c.text}  [{c.source}]" for r, l, c in self.pitch]]
        lines += ["Likely objections and approved answers", *[f"  - {o.key}: {o.text}  [{o.source}]" for o in self.objections]]
        lines += ["Best channel, language and time", *[f"  - {c.text}  [{c.source}]" for c in self.contact], *[f"  - {t}" for t in self.contact_notes]]
        if self.gaps:
            lines += ["Not known (not in the data)", *[f"  - {t}" for t in self.gaps]]
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({"lead_id": self.lead_id, "text": self.to_text(), "claims": [{"id": c.id, "text": c.text, "source": c.source} for c in self.claims()],
                           "gaps": self.gaps}, sort_keys=True, ensure_ascii=False)


def _sentences(text: str) -> list[str]:
    return [s.strip() + "." for s in text.replace("\n", " ").split(".") if s.strip()]


class AccountBriefJob:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger=None, kb_dir: Path = KB_DIR):
        self.conn, self.clock, self.ledger, self.kb_dir = conn, clock, ledger, kb_dir
        conn.execute(SCHEMA)
        self._kb: dict[str, object] = {}

    def _section(self, doc: str, section: str) -> tuple[str, str]:
        if doc not in self._kb:
            self._kb[doc] = parse_doc(self.kb_dir / f"{doc}.md")
        d = self._kb[doc]
        s = next(x for x in d.sections if x.section_id == section)
        return f"kb:{d.chunk_id(section)}", s.text

    def build(self, lead_id: str) -> Brief:
        row = self.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        if row is None:
            raise KeyError(lead_id)
        if row["matched_merchant_id"] is not None or '"result": "CLEAR"' not in (row["provenance_json"] or ""):
            raise BriefRefused(f"{lead_id} is not a cleared new-merchant lead")
        book, gaps = SourceBook(), []
        rec_id = f"tool:lead_record#{lead_id}"
        prov = json.loads(row["provenance_json"] or "{}")
        slab = row["gst_slab"]
        book.add_tool_result(rec_id, {"name": row["name"], "category": row["category"], "gst_slab": SLAB_LABEL.get(slab, ""), "pincode": row["pincode"],
                                      "data_source": row["source_id"], "fetched_at": row["fetched_at"]})
        cat = CATEGORY_LABEL.get(row["category"], row["category"])
        who = [Claim("w1", f"{row['name']} is a {cat} business in pincode {row['pincode']}." if row["pincode"] else f"{row['name']} is a {cat} business.", rec_id)]
        if slab in SLAB_LABEL:
            who.append(Claim("w2", f"Reported annual turnover slab (GST): {SLAB_LABEL[slab]}.", rec_id))
        else:
            gaps.append("GST turnover slab is not on record")
        who.append(Claim("w3", f"Record taken from {row['source_id']} on {row['fetched_at'][:10]}.", rec_id))
        busy = area_busyness(self.conn, self.clock).get(row["pincode"])
        t = estimate_turnover(slab, row["category"], busy)
        if t is None:
            gaps.append("Monthly turnover cannot be estimated: no signal on record")
        else:
            tid = f"tool:turnover_estimate#{lead_id}"
            book.add_tool_result(tid, {"monthly_paise": t.monthly_paise, "low_paise": t.low_paise, "high_paise": t.high_paise,
                                       "monthly_rupees": t.monthly_paise // 100, "low_rupees": t.low_paise // 100, "high_rupees": t.high_paise // 100, "signals": " ".join(t.signals),
                                       "signal_count": len(t.signals)})
            note = "a single signal, so the band is wide and uncertain" if t.uncertain else f"from {len(t.signals)} signals"
            who.append(Claim("w4", f"Estimated monthly card and UPI volume about Rs {rupees(t.monthly_paise)} (range Rs {rupees(t.low_paise)} to Rs {rupees(t.high_paise)}), {note}. "
                                   f"This is an estimate, not a reported figure.", tid))
            if t.uncertain:
                gaps.append("Turnover estimate rests on one signal only")
        gaps += ["Competitor QR or machine in use is not on record", "Years in business and number of outlets are not on record"]

        order = PITCH_ORDER[SLAB_ORDINAL.get(slab, 0)]
        pitch = []
        for rank, pid in zip(("main", "first fallback", "second fallback"), order):
            label, doc, sec, idx = PITCHES[pid]
            sid, text = self._section(doc, sec)
            book.add_kb(sid, text)
            pitch.append((rank, label, Claim(f"p-{pid}", _sentences(text)[idx], sid)))
        objs = []
        for key in OBJECTION_IDS:
            sid, text = self._section("objection_answers", key)
            book.add_kb(sid, text)
            objs.append(Objection(key, sid, text))

        seg = segment_for(row["category"])
        have = {r["channel"] for r in self.conn.execute("SELECT channel FROM consent WHERE subject_id = ? AND status = 'GRANTED'", (lead_id,))}
        usable = [c for c in SEGMENT_CHANNELS[seg] if c in have]
        notes = [f"Segment: {SEGMENT_LABEL[seg]}. First contact channel: " + (usable[0] if usable else "none yet, there is no consented channel; a person must obtain consent first"),
                 "Language: English by default; there is no stated preference on record. Do not guess a language from the region."]
        if not usable:
            gaps.append("No consented channel on record")
        pid = "policy:CONTACT-CAP-001"
        book.add_policy(pid, {"quiet_hours_start": QUIET_START_H, "quiet_hours_end": QUIET_END_H})
        contact = [Claim("c1", f"Do not contact between {QUIET_START_H} and {QUIET_END_H} hours (quiet hours); a later preference from the lead overrides.", pid)]

        brief = Brief(lead_id, row["name"], who, ["No competitor or current payment setup is on record for this lead."],
                      [NEEDS[row["category"]]] if row["category"] in NEEDS else [], pitch, objs, contact, notes, gaps, book)
        if not brief.passes_gate:
            raise BriefGateError("; ".join(f"{c.claim_id}: {c.reason} {c.unmatched_numbers}" for c in brief.check() if not c.ok) or f"unsourced numbers: {brief.unsourced_numbers()}; altered quotes: {brief.quote_errors()}")
        return brief

    def store(self, lead_id: str) -> Brief:
        b = self.build(lead_id)
        with write_txn(self.conn):
            self.conn.execute("INSERT INTO lead_briefs(lead_id, body_json, built_at) VALUES (?,?,?) ON CONFLICT(lead_id) DO UPDATE SET body_json = excluded.body_json, "
                              "built_at = excluded.built_at", (lead_id, b.to_json(), self.clock.now_iso()))
            if self.ledger is not None:
                self.ledger.append(kind="STATE", actor="SYSTEM", payload={"job": "brief", "lead_id": lead_id, "claims": len(b.claims()), "gate": "PASS"})
        return b


__all__ = ["AccountBriefJob", "Brief", "BriefRefused", "BriefGateError", "rupees"]
