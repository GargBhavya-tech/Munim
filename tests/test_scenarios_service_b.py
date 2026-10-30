"""Every scenario of C6, C7, C8, C9 and X0 (dev and held-out) run through the REAL World and compared with check().

What stands in for the model here, and nothing else:
  * ``SCRIPT`` below is the scripted decider (it plays the turn agent: what to do on each step).
  * ``SvcWorld.inject`` plays the model's intent extractor for a legal / regulatory mention (the rule detector in
    packs/paytm_merchant/intents.yaml has no such intent), setting the intent LEGAL_MENTION.
  * ``SvcWorld`` supplies the case fact ``closure_requests`` (the engine has no facts_fn in World) and the C8 novelty signal.
Everything safety-relevant (policy, executor, playbook limits, escalation) is the real code.
"""
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from core.engine.knowledge_layer import KnowledgeLayer, NO_ANSWER_REASON, ensure_schema, log_gap, redact
from core.engine.intents import top_intent
from core.engine.workflow import Decision
from core.eval.scenario import load_scenario
from core.memory.knowledge import load_kb_dir
from tests.scenario_runner import check, run_scenario
from tests.world import World

ROOT = Path(__file__).resolve().parent.parent
SC_DIR = ROOT / "packs" / "paytm_merchant" / "scenarios"
KB_DIR = ROOT / "packs" / "paytm_merchant" / "kb"
MINE = ("C6_CHARGE_DISPUTE", "C7_RETURN_CLOSURE", "C8_HOWTO", "C9_COMPLAINT", "X0_UNSUPPORTED")

LEGAL = re.compile(r"consumer court|lawyer|advocate|legal notice|police|\brbi\b|ombudsman|regulator|court", re.I)


def my_scenarios():
    out = []
    for split in ("dev", "heldout"):
        for p in sorted((SC_DIR / split).glob("*.yaml")):
            if p.name.startswith(tuple(f"{m}__" for m in MINE)):
                out.append(load_scenario(p))
    return out


SCENARIOS = my_scenarios()


# ------------------------------------------------------------------ a keyword retriever, a TEST DOUBLE for the injected Retriever
_STOP = set("a an the is are am to of on in for and or do does can could you your my me i it this that what how which who with be at as from by"
            " tell please plz hai hain ka ki ke ko se me mein kya kaun kab ho kar karo karna aur ye wo".split())


def _tokens(text):
    out = set()
    for w in re.findall(r"[^\W_]+", text.lower()):
        w = w[:-1] if w.endswith("s") and len(w) > 3 else w
        if len(w) > 2 and w not in _STOP:
            out.add(w)
    return out


class Hit:
    def __init__(self, chunk_id, doc_id, section_id, text, score, source="kb"):
        self.chunk_id, self.doc_id, self.section_id, self.text, self.score, self.source = chunk_id, doc_id, section_id, text, score, source


class FakeRetriever:
    """KB chunks scored by shared tokens (needs two) plus a corpus of past tickets that a careless retriever might also return."""
    TICKETS = [("ticket:T-9001", "ticket-history", "t9001", "M-0002 settlement issue was fixed by asking the bank to release the credit, ticket history note")]

    def __init__(self, scopes_by_doc=None):
        self.chunks = []
        self.doc_scopes = {}
        for d in load_kb_dir(KB_DIR):
            self.doc_scopes[d.doc_id] = list(d.scopes)
            for s in d.sections:
                self.chunks.append((d.chunk_id(s.section_id), d.doc_id, s.section_id, d.title + " " + s.heading + " " + s.text, s.text, s.heading))

    def search(self, query, scope, k):
        q = _tokens(query)
        hits = []
        for cid, doc, sec, blob, text, heading in self.chunks:
            if scope and not set(self.doc_scopes[doc]) & set(scope):
                continue
            m = len(q & _tokens(blob))
            if m >= 2:
                bonus = len(q & _tokens(heading))                      # a heading match counts double
                hits.append(Hit(cid, doc, sec, text, min(1.0, (m + bonus) / (max(1, min(len(q), 3)) + 1))))
        for cid, doc, sec, text in self.TICKETS:
            m = len(q & _tokens(text))
            if m >= 1:
                hits.append(Hit(cid, doc, sec, text, min(1.0, 0.4 + m / 4), source="ticket"))
        return sorted(hits, key=lambda h: -h.score)[:k]


# ------------------------------------------------------------------ the world
class SvcWorld(World):
    def __init__(self, db_path, clock, mid):
        self.retr = FakeRetriever()
        self.script = build_script(self)         # tests may replace an entry to play a different (naive) model
        super().__init__(db_path, clock, self.script, mid=mid)
        self.kl = KnowledgeLayer(self.retr, doc_scopes=self.retr.doc_scopes, conn=self.conn, clock=clock)
        ensure_schema(self.conn)
        base_hard = self.engine.hard_escalation

        def hard(case, ev):
            reasons = list(base_hard(case, ev))
            if case.playbook_id == "C8_HOWTO" and ev.type == "MERCHANT_MESSAGE":
                ans = self.kl.respond(ev.payload.get("text", ""), self.playbooks["C8_HOWTO"].kb_scope, merchant_id=case.merchant_id, case_id=case.case_id)
                reasons += ans.reasons
            return reasons

        self.engine.hard_escalation = hard
        self.engine.facts_fn = self.facts

    def inject(self, event_id, type_, mid=None, payload=None):
        payload = dict(payload or {})
        if type_ == "MERCHANT_MESSAGE" and LEGAL.search(payload.get("text", "")):
            payload["intent"] = "LEGAL_MENTION"       # the model's extractor, which the rule detector cannot be
        super().inject(event_id, type_, mid, payload)

    def closure_requests(self, case):
        """Closure requests still waiting to be processed in this case. Once a closure row exists the request IS being processed."""
        if self.reader("get_closures", {"merchant_id": case.merchant_id}).get("closures"):
            return 0
        rows = self.conn.execute("SELECT e.payload_json FROM engine_progress p JOIN events e ON e.event_id = p.event_id "
                                 "WHERE p.case_id = ? AND e.type = 'MERCHANT_MESSAGE'", (case.case_id,)).fetchall()
        return sum(1 for r in rows if json.loads(r[0]).get("intent") == "CLOSURE_REQUEST")

    def facts(self, case, ev):
        return {"closure_requests": self.closure_requests(case)} if case.playbook_id == "C7_RETURN_CLOSURE" else {}


# ------------------------------------------------------------------ the scripted decider tables (what the model would choose)
def build_script(w):
    mid = lambda ctx: ctx.case.merchant_id
    text = lambda ctx: ctx.event.payload.get("text", "")

    def months(ctx):
        m = re.search(r"(\d+)\s*(?:month|mahine|महीन|ತಿಂಗಳ)", text(ctx), re.I)
        return int(m.group(1)) if m else 1

    def c6_ack(ctx):
        plan = w.reader("get_plan", {"merchant_id": mid(ctx)})
        return Decision(channel="app", message_en=f"I have looked at your plan {plan['plan_id']}; the next billing date in the system is {plan['billing_date']}. Let me see what I can do about the charge.")

    def c6_resolve(ctx):
        band = w.reader("get_offer_band", {"merchant_id": mid(ctx)})["max_months"]
        n = months(ctx)
        if n <= band:
            return Decision(channel="app", action=("apply_fee_waiver", {"merchant_id": mid(ctx), "months": n}),
                            message_en=f"I have applied a {n} month fee waiver. You can see it under Offers in the app.")
        return Decision(channel="app", action=("raise_ticket", {"merchant_id": mid(ctx), "type": "BILLING", "case_id": ctx.case.case_id,
                                                 "summary": redact(text(ctx))[:200]}),
                        message_en="I cannot give that waiver myself. I have raised a billing ticket and the sales lead will look at it.")

    def c7_offer(ctx):
        return Decision(channel="app", message_en="I am sorry things have gone wrong. I can have the problem looked at first. If you still want to close, tell me and I will start it right away.")

    def c7_process(ctx):
        if ctx.facts.get("closure_requests", 0) >= 2:
            return Decision(action=("create_closure_request", {"merchant_id": mid(ctx), "reason": "merchant asked twice"}))
        return Decision()

    def c7_pickup(ctx):
        slot = (w.clock.now() + timedelta(hours=24)).isoformat()
        return Decision(channel="app", action=("book_visit_or_call", {"merchant_id": mid(ctx), "kind": "visit", "slot_at": slot}),
                        message_en="Your closure is requested and a pickup for the Soundbox is booked. You can cancel the closure within 48 hours.")

    def c8_answer(ctx):
        ans = w.kl.answer(text(ctx), ctx.playbook.kb_scope)
        assert not ans.escalate, "novelty must have escalated before the decider"
        return Decision(channel="app", message_en=ans.text)

    def c9_ticket(ctx):
        return Decision(action=("raise_ticket", {"merchant_id": mid(ctx), "type": "COMPLAINT", "case_id": ctx.case.case_id, "summary": redact(text(ctx))[:200]}))

    def c9_tell(ctx):
        t = w.reader("get_tickets", {"merchant_id": mid(ctx)})["tickets"][-1]
        return Decision(channel="app", message_en=f"Your complaint is recorded as ticket {t['ticket_id']}. A person on the team will review it. I cannot give you a resolution date because none has been set.")

    def x0_refuse(ctx):
        if ctx.event.payload.get("intent") != "WANTS_HUMAN":
            log_gap(w.conn, w.clock, kind="UNSUPPORTED", text=text(ctx), merchant_id=mid(ctx), case_id=ctx.case.case_id)
            body = "I can't do that. I can only help with Paytm services. I am passing your request to a person on the team."
        else:
            body = "Of course. I am connecting you to a person and passing on a summary so you do not have to repeat yourself."
        return Decision(channel="app", message_en=body)

    return {("C6_CHARGE_DISPUTE", "acknowledge"): c6_ack, ("C6_CHARGE_DISPUTE", "resolve"): c6_resolve,
            ("C7_RETURN_CLOSURE", "fix_offer"): c7_offer, ("C7_RETURN_CLOSURE", "process_closure"): c7_process, ("C7_RETURN_CLOSURE", "schedule_pickup"): c7_pickup,
            ("C8_HOWTO", "answer"): c8_answer,
            ("C9_COMPLAINT", "file_ticket"): c9_ticket, ("C9_COMPLAINT", "tell_merchant"): c9_tell,
            ("X0_UNSUPPORTED", "refuse_honestly"): x0_refuse}


# ------------------------------------------------------------------ the tests
def test_the_five_playbooks_have_the_required_scenario_coverage():
    from core.eval.scenario import coverage, meets_supported_bar
    cov = coverage(SCENARIOS)
    assert set(cov) == set(MINE)
    for pid, c in cov.items():
        assert meets_supported_bar(c), (pid, c)
        assert set(c["families"]) == {"happy", "multi_case", "should_escalate", "adversarial", "noise"}, (pid, c)
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario_final_state_matches_expectations(sc, db_path, clock):
    world = SvcWorld(db_path, clock, sc.persona.merchant_id)
    assert check(sc, run_scenario(world, sc)) == []
