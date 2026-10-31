"""New-merchant leads: the sales funnel the growth jobs already built, shown read-only (docs/API_CONTRACT.md).

Discovery, duplicate matching and scoring are JOBS (jobs/discovery.py, dedupe.py, scoring.py) that run on the sim clock; this module never runs them and never
changes a lead. It reports what they decided: how many leads were found, how many were dropped as existing merchants or duplicates, and the ranked rest. A lead's
brief is the brief job's (jobs/briefs.py): every number in it cites a source and passes the numbers check, or the brief is refused. Contact is shown, never done:
consent, do-not-disturb and the channel come from the same tables the outreach job reads."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from core.api.deps import read_db, write_db
from jobs.briefs import AccountBriefJob, Brief, BriefGateError, BriefRefused
from jobs.dedupe import outreach_ready
from jobs.outreach import FIELD_VISIT, HIGH_VALUE_SCORE, SEGMENT_CHANNELS, segment_for
from jobs.scoring import area_busyness, channel_for, estimate_turnover

router = APIRouter(prefix="/api")

EXCLUDED_STATES = ("EXISTING_MERCHANT", "NEEDS_REVIEW", "DUPLICATE")
EXCLUDED_LIMIT = 100
STATE_WORDS = {"EXISTING_MERCHANT": "Already a Paytm merchant", "DUPLICATE": "Duplicate of a better lead", "NEEDS_REVIEW": "Possible match: a person decides"}


def _prov(row) -> dict:
    try:
        return json.loads(row["provenance_json"] or "{}")
    except ValueError:
        return {}


def _cleared(row) -> bool:
    return row["matched_merchant_id"] is None and _prov(row).get("dedupe", {}).get("result") == "CLEAR"


def _summary(row, busy: dict, dnd: set) -> dict:
    t = estimate_turnover(row["gst_slab"], row["category"], busy.get(row["pincode"]))
    score = row["score"]
    return {
        "lead_id": row["lead_id"], "name": row["name"], "category": row["category"], "pincode": row["pincode"], "lat": row["lat"], "lng": row["lng"],
        "source_id": row["source_id"], "confidence": row["confidence"], "gst_slab": row["gst_slab"], "state": row["state"],
        "score": score, "channel": channel_for(int(score)) if score is not None else None, "uncertain": t.uncertain if t else True,
        "monthly_rupees": t.monthly_paise // 100 if t else None, "low_rupees": t.low_paise // 100 if t else None, "high_rupees": t.high_paise // 100 if t else None,
        "do_not_contact": bool(row["phone_public"]) and row["phone_public"] in dnd,
    }


def _dnd(conn) -> set:
    return {r[0] for r in conn.execute("SELECT phone FROM dnd_registry")}


def _excluded(row) -> dict:
    d = _prov(row).get("dedupe", {})
    reasons = d.get("reasons") or []
    return {"lead_id": row["lead_id"], "name": row["name"], "state": row["state"], "category": row["category"], "pincode": row["pincode"],
            "matched_merchant_id": row["matched_merchant_id"] or d.get("possible_merchant"), "duplicate_of": d.get("duplicate_of"),
            "reason": "; ".join(str(r) for r in reasons) or STATE_WORDS.get(row["state"], row["state"])}


@router.get("/leads")
def leads(request: Request, conn=Depends(read_db)):
    rows = conn.execute("SELECT * FROM leads ORDER BY lead_id").fetchall()
    busy, dnd = area_busyness(conn, request.app.state.clock), _dnd(conn)
    funnel = {"found": len(rows), "existing_merchant": 0, "needs_review": 0, "duplicate": 0, "cleared": 0, "not_checked": 0}
    for r in rows:
        if r["state"] == "EXISTING_MERCHANT":
            funnel["existing_merchant"] += 1
        elif r["state"] == "NEEDS_REVIEW":
            funnel["needs_review"] += 1
        elif r["state"] == "DUPLICATE":
            funnel["duplicate"] += 1
        elif _cleared(r):
            funnel["cleared"] += 1
        else:
            funnel["not_checked"] += 1
    ready = sorted((_summary(r, busy, dnd) for r in outreach_ready(conn)), key=lambda s: (s["score"] is None, -(s["score"] or 0), s["lead_id"]))
    placed = [r for r in rows if r["lat"] is not None and r["lng"] is not None]
    bounds = {"min_lat": min(r["lat"] for r in placed), "max_lat": max(r["lat"] for r in placed), "min_lng": min(r["lng"] for r in placed),
              "max_lng": max(r["lng"] for r in placed)} if placed else None
    return {"funnel": funnel, "bounds": bounds, "leads": ready,
            "excluded": [_excluded(r) for r in rows if r["state"] in EXCLUDED_STATES][:EXCLUDED_LIMIT]}


def _brief_json(b: Brief) -> dict:
    claim = lambda c: {"id": c.id, "text": c.text, "source": c.source}
    return {
        "passes_gate": b.passes_gate,
        "who": [claim(c) for c in b.who], "uses_now": list(b.uses_now), "needs": list(b.needs),
        "pitch": [{"rank": rank, "label": label, **claim(c)} for rank, label, c in b.pitch],
        "objections": [{"key": o.key, "source": o.source, "text": o.text} for o in b.objections],
        "contact": [claim(c) for c in b.contact], "contact_notes": list(b.contact_notes), "gaps": list(b.gaps),
    }


def _contact(conn, row, dnd: set) -> dict:
    """What outreach would do and why it may not yet: the same rules as jobs.outreach.OutreachJob.choose_channel, read-only."""
    have = [r["channel"] for r in conn.execute("SELECT channel FROM consent WHERE subject_id = ? AND status = 'GRANTED' ORDER BY channel", (row["lead_id"],))]
    seg = segment_for(row["category"])
    order = SEGMENT_CHANNELS[seg]
    if seg == "offline" and (row["score"] or 0) >= HIGH_VALUE_SCORE:
        order = (FIELD_VISIT, *order)
    chosen = next((c for c in order if c in have), None)
    on_dnd = bool(row["phone_public"]) and row["phone_public"] in dnd
    why = ("The number is on the do not disturb list" if on_dnd else
           "No consent on record for any channel: a person must obtain consent first" if not have else
           f"Consent is on record for {', '.join(have)}, but this kind of business is reached by {' or '.join(order)}" if chosen is None else None)
    return {"segment": seg, "order": list(order), "consented_channels": have, "chosen_channel": chosen, "do_not_contact": on_dnd,
            "can_contact_now": chosen is not None and not on_dnd, "why_not": why}


@router.get("/leads/{lead_id}")
def lead_detail(lead_id: str, request: Request, conn=Depends(read_db)):
    row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "LEAD_NOT_FOUND")
    dnd = _dnd(conn)
    prov = _prov(row)
    try:
        with write_db(request) as w:                                       # the brief job creates its own table on first use; it writes no lead data
            brief = AccountBriefJob(w, request.app.state.clock).build(lead_id)
    except BriefRefused as e:
        raise HTTPException(409, {"error": "BRIEF_REFUSED", "reason": str(e), "state": row["state"], "name": row["name"],
                                  "matched_merchant_id": row["matched_merchant_id"] or prov.get("dedupe", {}).get("possible_merchant")})
    except BriefGateError as e:
        raise HTTPException(500, {"error": "BRIEF_GATE_FAILED", "reason": str(e)})
    return {"lead": _summary(row, area_busyness(conn, request.app.state.clock), dnd),
            "provenance": {"fields": {k: v for k, v in prov.items() if k != "dedupe"}, "dedupe": prov.get("dedupe", {})},
            "contact": _contact(conn, row, dnd), "brief": _brief_json(brief)}
