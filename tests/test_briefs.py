"""#100 account briefs. Gate G2 for briefs: every number in a brief has a source it matches; turnover is never invented."""
import json

import pytest

from core.audit.ledger import Ledger
from core.gate.numbers import extract_numbers
from core.gate.sources import Claim, SourceBook, check_claim
from jobs import briefs as briefs_mod
from jobs.briefs import AccountBriefJob, BriefGateError, BriefRefused, rupees
from jobs.dedupe import DedupeJob, outreach_ready
from jobs.scoring import estimate_turnover


@pytest.fixture
def job(conn, clock):
    ledger = Ledger(conn, clock)
    DedupeJob(conn, clock, ledger).run()
    return AccountBriefJob(conn, clock, ledger)


def lead(conn, **where):
    cond = " AND ".join(f"{k} = ?" for k in where) or "1=1"
    ids = {r["lead_id"] for r in outreach_ready(conn)}
    for r in conn.execute(f"SELECT * FROM leads WHERE {cond} ORDER BY lead_id", tuple(where.values())):
        if r["lead_id"] in ids:
            return r
    raise AssertionError(where)


def test_rupees_uses_indian_grouping_and_integer_paise():
    assert [rupees(p) for p in (0, 99_900, 150_000_00, 600_000_00, 8_100_000_00, 123_456_700)] == ["0", "999", "1,50,000", "6,00,000", "81,00,000", "12,34,567"]


def test_every_claim_in_a_brief_has_an_existing_source_and_its_numbers_match(conn, job):
    for r in outreach_ready(conn)[:25]:
        b = job.build(r["lead_id"])
        checks = b.check()
        assert checks and all(c.ok for c in checks), (r["lead_id"], [c for c in checks if not c.ok])
        assert b.passes_gate and b.unsourced_numbers() == []
        # every source a claim cites really is in the brief's source book
        assert all(b.book.exists(c.source) for c in b.claims())


def test_every_number_anywhere_in_the_rendered_brief_belongs_to_a_cited_claim(conn, job):
    """G2 from the outside: scan ALL the rendered text, not just the claims the builder says carry numbers."""
    b = job.build(lead(conn).__getitem__("lead_id"))
    import re
    body = "\n".join(re.sub(r"\s*\[[^\]]*\]", "", ln) for ln in b.to_text().splitlines()[1:])        # drop the header and the [source-id] tags
    in_claims = {n.raw for c in b.claims() for n in extract_numbers(c.text, english_one=False)}
    in_text = {n.raw for n in extract_numbers(body, english_one=False)}
    assert in_text, "a brief with no number at all would test nothing"
    assert in_text <= in_claims


def test_the_turnover_figure_is_exactly_the_scoring_jobs_estimate_in_rupees(conn, clock, job):
    r = lead(conn, gst_slab="1.5Cr-5Cr")
    b = job.build(r["lead_id"])
    from jobs.scoring import area_busyness
    t = estimate_turnover(r["gst_slab"], r["category"], area_busyness(conn, clock).get(r["pincode"]))
    claim = next(c for c in b.who if c.id == "w4")
    assert f"Rs {rupees(t.monthly_paise)}" in claim.text and f"Rs {rupees(t.low_paise)}" in claim.text and f"Rs {rupees(t.high_paise)}" in claim.text
    assert claim.source == f"tool:turnover_estimate#{r['lead_id']}" and "not a reported figure" in claim.text
    assert b.book.get(claim.source)[1]["monthly_paise"] == t.monthly_paise


def test_a_lead_with_no_turnover_signal_gets_no_turnover_figure_and_says_so(conn, clock, job, monkeypatch):
    r = lead(conn)
    monkeypatch.setattr(briefs_mod, "estimate_turnover", lambda *a, **k: None)                  # no signal at all
    b = job.build(r["lead_id"])
    assert not any(c.id == "w4" for c in b.who)
    assert "Monthly turnover cannot be estimated: no signal on record" in b.gaps
    assert "Estimated monthly" not in b.to_text()


def test_a_single_signal_estimate_is_flagged_uncertain_in_the_brief(conn, job, monkeypatch):
    r = lead(conn, gst_slab="40L-1.5Cr")
    monkeypatch.setattr(briefs_mod, "estimate_turnover", lambda slab, cat, busy: estimate_turnover(slab, None, None))      # one signal: the slab
    b = job.build(r["lead_id"])
    w4 = next(c for c in b.who if c.id == "w4")
    assert "wide and uncertain" in w4.text and "Turnover estimate rests on one signal only" in b.gaps


# ---------------------------------------------------------------------------------------------------------- WATCH OUT: invented turnover numbers
def test_the_gate_catches_an_invented_turnover_number(conn, job, monkeypatch):
    """The named mistake, made on purpose: the claim text states a different figure than the estimate in its source."""
    r = lead(conn, gst_slab="40L-1.5Cr")
    real = briefs_mod.rupees
    calls = {"n": 0}

    def inflated(p):
        calls["n"] += 1
        return real(int(p * 1.5)) if calls["n"] == 1 else real(p)                                # the headline figure is silently 50% larger than the source's
    monkeypatch.setattr(briefs_mod, "rupees", inflated)
    with pytest.raises(BriefGateError) as e:
        job.build(r["lead_id"])
    assert "w4" in str(e.value) and "numbers_not_in_source" in str(e.value)


def test_a_claim_citing_a_missing_source_or_another_sources_number_fails_the_check():
    book = SourceBook()
    book.add_tool_result("tool:a#1", {"monthly_paise": 600_000_00})
    book.add_tool_result("tool:b#1", {"monthly_paise": 900_000_00})
    assert check_claim(Claim("x", "about Rs 6,00,000", "tool:a#1"), book).ok
    assert check_claim(Claim("x", "about Rs 6,00,000", "tool:b#1"), book).reason == "numbers_not_in_source"      # right number, wrong source
    assert check_claim(Claim("x", "about Rs 6,00,000", "tool:zzz#9"), book).reason == "source_not_found"


def test_an_objection_answer_that_is_not_the_kb_text_fails_the_gate(conn, job):
    b = job.build(lead(conn)["lead_id"])
    assert b.quote_errors() == []
    o = b.objections[0]
    b.objections[0] = type(o)(o.key, o.source, o.text.replace("Do not promise a discount", "Promise a discount of 20 percent"))
    assert b.quote_errors() == ["charges_too_high"] and not b.passes_gate


def test_free_text_may_not_smuggle_in_a_number(conn, job):
    b = job.build(lead(conn)["lead_id"])
    b.needs.append("They probably turn over 12 lakh a month.")                                   # a hypothesis with a number and no source
    assert b.unsourced_numbers() and not b.passes_gate


# ---------------------------------------------------------------------------------------------------------- content
def test_the_brief_has_every_section_one_pitch_with_two_fallbacks_and_approved_objection_answers(conn, job):
    r = lead(conn, gst_slab="1.5Cr-5Cr")
    b = job.build(r["lead_id"])
    assert [p[0] for p in b.pitch] == ["main", "first fallback", "second fallback"] and len({p[1] for p in b.pitch}) == 3
    assert b.pitch[0][2].text == "Soundbox Plus costs 249 rupees a month." and b.pitch[0][2].source == "kb:fee_schedule#soundbox_plans@v1"
    assert [o.key for o in b.objections] == ["charges_too_high", "already_using_other", "device_rent", "trust"]
    assert all(o.source.startswith("kb:objection_answers#") for o in b.objections)
    txt = b.to_text()
    for heading in ("Who they are", "What they use now", "What they probably need", "Pitch hypothesis", "Likely objections", "Best channel, language and time"):
        assert heading in txt
    assert "SIMULATED" in txt


def test_the_pitch_order_follows_the_slab(conn, job):
    small = job.build(lead(conn, gst_slab="<40L")["lead_id"])
    assert small.pitch[0][1].startswith("Start with the free QR")
    mid = job.build(lead(conn, gst_slab="40L-1.5Cr")["lead_id"])
    assert mid.pitch[0][1].startswith("Soundbox Standard")


def test_what_the_data_does_not_hold_is_listed_not_guessed(conn, job):
    b = job.build(lead(conn)["lead_id"])
    assert "Competitor QR or machine in use is not on record" in b.gaps and "Years in business and number of outlets are not on record" in b.gaps
    assert b.uses_now == ["No competitor or current payment setup is on record for this lead."]


def test_best_channel_comes_from_consent_and_segment_never_a_guess(conn, job):
    r = lead(conn)
    assert "none yet" in " ".join(job.build(r["lead_id"]).contact_notes) and "No consented channel on record" in job.build(r["lead_id"]).gaps
    conn.execute("INSERT INTO consent VALUES (?,?,?,?,?)", (r["lead_id"], "whatsapp", "GRANTED", "opt_in", "2026-10-31T09:00:00+05:30"))
    b = job.build(r["lead_id"])
    assert "First contact channel: whatsapp" in b.contact_notes[0] and "Do not guess a language from the region" in b.contact_notes[1]
    assert "between 21 and 9 hours" in b.contact[0].text


def test_only_cleared_new_leads_are_briefed(conn, job):
    held = conn.execute("SELECT lead_id FROM leads WHERE state IN ('EXISTING_MERCHANT', 'NEEDS_REVIEW')").fetchall()
    assert held
    for h in held[:5]:
        with pytest.raises(BriefRefused):
            job.build(h["lead_id"])


def test_store_persists_the_brief_and_ledgers_the_gate_result(conn, job):
    r = lead(conn)
    b = job.store(r["lead_id"])
    stored = json.loads(conn.execute("SELECT body_json FROM lead_briefs WHERE lead_id = ?", (r["lead_id"],)).fetchone()[0])
    assert stored["lead_id"] == r["lead_id"] and len(stored["claims"]) == len(b.claims())
    last = json.loads(conn.execute("SELECT payload_json FROM ledger WHERE kind = 'STATE' ORDER BY seq DESC LIMIT 1").fetchone()[0])
    assert last == {"job": "brief", "lead_id": r["lead_id"], "claims": len(b.claims()), "gate": "PASS"}
