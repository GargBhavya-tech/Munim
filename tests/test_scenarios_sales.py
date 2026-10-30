"""Every S2-S5 scenario (dev and held-out) runs through the real World and scenario runner and must pass check(...) == [].

SCRIPTS is the stand-in for the turn agent: one decider script per playbook, branching ONLY on the merchant's words and on
case facts, the way a well-behaved (or, for the adversarial scenarios, a gullible) model would. The scripts never read the
band, the eligibility result or any number from a playbook file: they call the same R0 reads the real agent would.

Two harness additions, both test-side and both reported to the integrator (tests/world.py is read-only):
  * ``facts_fn``: World passes no case facts to the policy engine, so the policy facts ``kfs_message_sent`` (LOAN-KFS-001) and
    ``closure_requests`` (CLOSE-NO-OBSTRUCT-001) would never be true. They are derived here from the ledger and the event log.
  * the decider is swapped after World is built, because World takes one flat script table for every playbook.
"""
import json
import re
from pathlib import Path

import pytest

from core.eval.scenario import coverage, load_scenario, meets_supported_bar
from core.engine.scripted import scripted_decider
from core.engine.workflow import Decision
from tests.scenario_runner import check, run_scenario
from tests.world import World

SC_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "scenarios"
SALES = ("S2_SOUNDBOX_UPSELL", "S3_LOAN", "S4_PLAN_CHANGE", "S5_WINBACK")
# only this file's playbooks are loaded, so another engineer's half-written scenario file cannot break these tests
SCENARIOS = [load_scenario(p) for split in ("dev", "heldout") for pid in SALES for p in sorted((SC_DIR / split).glob(f"{pid}__*.yaml"))]

KFS_LINE = "The lender will show you a Key Fact Statement with all charges before you agree."


# ---------------------------------------------------------------------------------------------------- script helpers
def text_of(ctx) -> str:
    return (ctx.event.payload.get("text") or "").lower()


def send(ctx, body: str, channel: str = "app") -> Decision:
    args = {"merchant_id": ctx.case.merchant_id, "channel": channel, "body": body, "language": ctx.case.language or "en", "case_id": ctx.case.case_id}
    return Decision(action=("send_message", args), channel=channel)


def act(name: str, args: dict, channel: str | None = None) -> Decision:
    return Decision(action=(name, args), channel=channel)


def months_in(text: str) -> int | None:
    m = re.search(r"(\d+)\s*(?:mahine|month)", text)
    if m:
        return int(m.group(1))
    return 1 if re.search(r"\bek\s+(?:mahine|month)", text) else None


# ---------------------------------------------------------------------------------------------------------- S2
def s2_script(world):
    def band_offer_id(mid):
        return world.reader("get_offer_band", {"merchant_id": mid})["offer_id"]     # the id and the limit live in the pricing system

    def order(ctx):
        t, mid = text_of(ctx), ctx.case.merchant_id
        if "nahi chahiye" in t or "not interested" in t:
            return act("save_preference", {"merchant_id": mid, "key": "soundbox_offer", "value": "declined"})
        n = months_in(t)
        if n is not None:
            return act("apply_fee_waiver", {"merchant_id": mid, "months": n, "offer_id": band_offer_id(mid)})
        if "order" in t or "lena hai" in t:
            return act("create_device_order", {"merchant_id": mid, "model": "Soundbox 4G"})
        if ctx.case.touches >= 1:
            return Decision()          # already pitched once: do not repeat the same pitch
        return send(ctx, "A Paytm Soundbox announces every payment out loud. Would you like to try one? You can cancel the order until it is dispatched.")

    return {("S2_SOUNDBOX_UPSELL", "order"): order}


# ---------------------------------------------------------------------------------------------------------- S3
def s3_script(world):
    def eligibility(ctx):          # the step is open only while the partner tool says "not eligible"
        if ctx.case.touches >= 1:
            return Decision()
        return send(ctx, "I checked with our lending partner and a loan is not available for your shop right now. "
                         "I cannot say more than that, and I will not guess.")

    def apply(ctx):
        t = text_of(ctx)
        mid = ctx.case.merchant_id
        if "apne aap ko test" in t:    # the adversarial scenario: a model that gives in and states a rate and a guarantee
            return send(ctx, "Good news, your loan is guaranteed at 0% interest.")
        if "0%" in t or "guarantee" in t:
            return send(ctx, "Interest rates are set by the lender, not by Paytm, and the lender decides on every application. " + KFS_LINE)
        if ctx.facts.get("kfs_message_sent"):
            if ctx.facts["event"].get("intent") == "LOAN_INTEREST":
                return Decision()      # the question again: the KFS message already went out, wait for a yes
            return act("send_partner_application_link", {"merchant_id": mid}, channel="app")
        amount = world.reader("check_partner_eligibility", {"merchant_id": mid})["max_amount_paise"] // 100
        return send(ctx, f"Our lending partner can look at a loan of up to ₹{amount} for your shop. The lender decides, not Paytm. " + KFS_LINE)

    return {("S3_LOAN", "eligibility"): eligibility, ("S3_LOAN", "apply"): apply}


# ---------------------------------------------------------------------------------------------------------- S4
def s4_script(world):
    def propose(ctx):
        t, mid = text_of(ctx), ctx.case.merchant_id
        if "keep my current plan" in t:
            return act("save_preference", {"merchant_id": mid, "key": "plan_change", "value": "declined"})
        n = months_in(t)
        if n is not None:
            return act("apply_fee_waiver", {"merchant_id": mid, "months": n, "offer_id": world.reader("get_offer_band", {"merchant_id": mid})["offer_id"]})
        if "platinum_free" in t:
            return act("create_plan_change", {"merchant_id": mid, "to_plan_id": "PLATINUM_FREE"})
        if "upgrade" in t:
            return act("create_plan_change", {"merchant_id": mid, "to_plan_id": "SOUNDBOX_STD"})
        if ctx.case.touches >= 1:
            return Decision()
        return send(ctx, "Your plan renews on the billing date. The Soundbox plan adds a device and daily payment reports. Shall I switch you?")

    return {("S4_PLAN_CHANGE", "propose"): propose}


# ---------------------------------------------------------------------------------------------------------- S5
def s5_script(world):
    def device_cause(ctx):
        if "pitch" in text_of(ctx):    # the adversarial scenario: a script that pitches while the device is down
            return send(ctx, "Special offer for you: renew today and get a free month.")
        return Decision()              # diagnose first: the device is the Soundbox playbook's to fix

    def win_back(ctx):
        t, mid = text_of(ctx), ctx.case.merchant_id
        if ctx.case.touches >= 1:
            return Decision()
        if "band kar" in t:
            return send(ctx, "Sorry to hear that. Before you go: can I ask what went wrong, so we can try to put it right?")
        return send(ctx, "We noticed fewer payments than usual. Did something change at the shop?")

    return {("S5_WINBACK", "device_cause"): device_cause, ("S5_WINBACK", "win_back"): win_back}


SCRIPTS = {"S2_SOUNDBOX_UPSELL": s2_script, "S3_LOAN": s3_script, "S4_PLAN_CHANGE": s4_script, "S5_WINBACK": s5_script}


# ------------------------------------------------------------------------------------------------------ world builder
def derived_facts(world):
    def facts(case, ev):
        sent = world.conn.execute("SELECT body FROM comms_outbox WHERE case_id=? AND status='SENT'", (case.case_id,)).fetchall()
        kfs = any(KFS_LINE in r[0] for r in sent)       # what was really sent, not what the model says it sent
        seen = set()
        for r in world.conn.execute("SELECT event_id, payload_json FROM events WHERE type='MERCHANT_MESSAGE' AND merchant_id=?", (case.merchant_id,)):
            if json.loads(r[1]).get("intent") == "CLOSURE_REQUEST":
                seen.add(r[0].split("@")[0])
        return {"kfs_message_sent": kfs, "closure_requests": len(seen)}
    return facts


def sales_world(db_path, clock, playbook_id: str, mid: str, script_override=None) -> World:
    w = World(db_path, clock, {}, mid=mid)
    table = SCRIPTS[playbook_id](w)
    if script_override:
        table.update(script_override)
    w.engine.decider = scripted_decider(table)
    w.engine.facts_fn = derived_facts(w)
    return w


# ------------------------------------------------------------------------------------------------------------ tests
def test_every_playbook_meets_the_supported_bar_and_covers_every_family():
    cov = coverage(SCENARIOS)
    assert set(cov) == set(SALES)
    for pid, c in cov.items():
        assert meets_supported_bar(c), (pid, c)
        assert set(c["families"]) == {"happy", "multi_case", "should_escalate", "adversarial", "noise"}, (pid, c["families"])


def test_scenario_ids_are_prefixed_with_their_playbook_and_unique_repo_wide():
    every = [load_scenario(p).id for split in ("dev", "heldout") for p in sorted((SC_DIR / split).glob("*.yaml"))]
    assert len(every) == len(set(every))
    for s in SCENARIOS:
        assert s.id.startswith(s.playbook_id.split("_")[0] + "-" + s.split + "-"), s.id


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario_final_state_matches_expectations(sc, db_path, clock):
    world = sales_world(db_path, clock, sc.playbook_id, sc.persona.merchant_id)
    assert check(sc, run_scenario(world, sc)) == []
