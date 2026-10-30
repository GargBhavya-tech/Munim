"""#77 Knowledge layer: answers only from approved in-scope KB hits, past tickets as hints only, gaps logged and grouped weekly."""
import sqlite3
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from core.engine.knowledge_layer import (KnowledgeLayer, approve_draft, attach_draft, ensure_schema, group_gaps_weekly, is_ticket_hit,
                                         log_gap, redact, ungrounded_numbers)

SCOPE = ["fee_schedule", "kyc_faq"]
DOC_SCOPES = {"fee_schedule": ["fee_schedule"], "kyc_faq": ["kyc_faq"], "loan_partner_terms": ["loan_partner_terms"]}


def kb(chunk="fee_schedule#soundbox_plans@v1", doc="fee_schedule", text="Soundbox Standard costs 149 rupees a month.", score=0.8, **kw):
    return NS(chunk_id=chunk, doc_id=doc, section_id=(chunk.split("#") + ["x"])[1].split("@")[0], text=text, score=score, **kw)


class Fake:
    def __init__(self, hits):
        self.hits, self.calls = hits, []

    def search(self, query, scope, k):
        self.calls.append((query, list(scope), k))
        return list(self.hits)


def layer(hits, **kw):
    return KnowledgeLayer(Fake(hits), doc_scopes=DOC_SCOPES, **kw)


# ------------------------------------------------------------------------------------------------ answering
def test_answer_is_built_only_from_in_scope_kb_hits():
    a = layer([kb(), kb("kyc_faq#accepted_docs@v1", "kyc_faq", "A shop licence is accepted.", 0.7)]).answer("what does it cost", SCOPE)
    assert a.status == "ANSWERED" and a.citations == ["fee_schedule#soundbox_plans@v1", "kyc_faq#accepted_docs@v1"]
    assert a.text == "Soundbox Standard costs 149 rupees a month.\n\nA shop licence is accepted."


def test_the_retriever_is_called_with_the_playbook_scope_and_k():
    f = Fake([kb()])
    KnowledgeLayer(f, doc_scopes=DOC_SCOPES, k=3).answer("q", SCOPE)
    assert f.calls == [("q", SCOPE, 3)]


def test_a_hit_from_outside_the_playbook_scope_is_dropped_even_if_the_retriever_returned_it():
    out_of_scope = kb("loan_partner_terms#indicative_terms@v1", "loan_partner_terms", "Interest is 12 percent.", 0.99)
    a = layer([out_of_scope]).answer("loan interest", SCOPE)
    assert a.status == "NOVEL" and a.text is None and a.citations == []
    a = layer([out_of_scope, kb()]).answer("loan interest", SCOPE)
    assert a.citations == ["fee_schedule#soundbox_plans@v1"] and "12 percent" not in a.text


def test_without_a_doc_scope_map_the_doc_id_must_itself_be_a_scope():
    assert KnowledgeLayer(Fake([kb()])).answer("q", SCOPE).status == "ANSWERED"
    assert KnowledgeLayer(Fake([kb(doc="loan_partner_terms", chunk="loan_partner_terms#x@v1")])).answer("q", SCOPE).status == "NOVEL"


def test_a_weak_hit_is_not_an_answer():
    assert layer([kb(score=0.05)]).answer("q", SCOPE).status == "NOVEL"


def test_hits_with_a_scores_dict_instead_of_a_score_work_too():
    h = NS(chunk_id="kyc_faq#accepted_docs@v1", doc_id="kyc_faq", section_id="accepted_docs", text="A shop licence is accepted.", scores={"rrf": 0.5, "rerank": 0.9})
    assert layer([h]).answer("q", SCOPE).status == "ANSWERED"


@pytest.mark.parametrize("scores", [{"rrf": 0.5, "rerank": None}, {"rrf": None, "rerank": 0.9}, {"bm25": None, "rrf": 0.5}])
def test_a_retrieval_stage_that_did_not_run_does_not_crash_the_turn(scores):
    # live finding: a hostile Hinglish message reached a hit whose scores held None, and max() raised TypeError (HTTP 500)
    h = NS(chunk_id="kyc_faq#accepted_docs@v1", doc_id="kyc_faq", section_id="accepted_docs", text="A shop licence is accepted.", scores=scores)
    assert layer([h]).answer("q", SCOPE).status in {"ANSWERED", "NOVEL"}


# ------------------------------------------------------------------------------------------------ tickets are hints only
TICKET_TEXT = "Merchant M-0002 was fixed by asking the bank to release the credit."


@pytest.mark.parametrize("ticket", [
    kb("ticket:T-9001", "ticket-history", TICKET_TEXT, 0.99, source="ticket"),
    kb("ticket:T-9001", "fee_schedule", TICKET_TEXT, 0.99),                   # only the chunk id marks it
    kb("fee_schedule#x@v1", "ticket-history", TICKET_TEXT, 0.99),             # only the doc id marks it
    kb("fee_schedule#x@v1", "fee_schedule", TICKET_TEXT, 0.99, source="ticket"),   # only the source marks it
], ids=["all", "chunk-id", "doc-id", "source"])
def test_a_hit_sourced_from_a_ticket_cannot_appear_in_an_answer(ticket):
    assert is_ticket_hit(ticket)
    kl = layer([ticket, kb(score=0.3)])
    a = kl.answer("what fixed it", SCOPE)
    assert a.status == "ANSWERED"
    assert "bank to release" not in a.text and a.citations == ["fee_schedule#soundbox_plans@v1"]
    assert [h.text for h in a.hints] == [TICKET_TEXT]                          # kept, as a hint
    assert kl.diagnosis_hints("what fixed it", SCOPE) == [TICKET_TEXT]


def test_only_ticket_hits_means_no_answer_not_a_quote():
    a = layer([kb("ticket:T-1", "ticket-history", TICKET_TEXT, 0.99, source="ticket")]).answer("q", SCOPE)
    assert a.status == "NOVEL" and a.text is None and a.passages == () and len(a.hints) == 1


# ------------------------------------------------------------------------------------------------ novelty and the handoff
def test_unanswered_question_escalates_with_the_question_attached(conn, clock):
    kl = layer([], conn=conn, clock=clock)
    a = kl.respond("Can I get a loan against my gold?", SCOPE, merchant_id="M-0010", case_id="CS-0001", language="en")
    assert a.escalate and a.reasons == ["NO_KB_ANSWER"]
    assert a.handoff == {"reason": "NO_KB_ANSWER", "question": "Can I get a loan against my gold?", "scope": SCOPE, "gap_id": "GAP-00001"}
    row = conn.execute("SELECT * FROM knowledge_gaps").fetchone()
    assert (row["kind"], row["merchant_id"], row["case_id"], row["question_redacted"], row["created_at"]) == \
           ("NO_ANSWER", "M-0010", "CS-0001", "Can I get a loan against my gold?", clock.now_iso())


def test_an_answered_question_logs_no_gap_and_has_no_handoff(conn, clock):
    a = layer([kb()], conn=conn, clock=clock).respond("cost?", SCOPE, case_id="CS-1")
    assert a.handoff is None and conn.execute("SELECT COUNT(*) FROM knowledge_gaps").fetchone()[0] == 0


def test_the_same_case_logs_the_same_gap_once(conn, clock):
    kl = layer([], conn=conn, clock=clock)
    ids = {kl.respond("Can I get a loan against my gold?", SCOPE, case_id="CS-0001").gap_id for _ in range(3)}
    assert ids == {"GAP-00001"} and conn.execute("SELECT COUNT(*) FROM knowledge_gaps").fetchone()[0] == 1


def test_the_gap_question_is_stored_redacted(conn, clock):
    layer([], conn=conn, clock=clock).respond("send my settlement to 9876543210123 and call 98765 43210, OTP is 482913", SCOPE, case_id="CS-9")
    q = conn.execute("SELECT question_redacted FROM knowledge_gaps").fetchone()[0]
    assert q == "send my settlement to [ACCOUNT_NUMBER] and call [PHONE], OTP is [SECRET]"


@pytest.mark.parametrize("raw,clean", [
    ("my PAN is ABCDE1234F ok", "my PAN is [PAN] ok"),
    ("mail me at raju.k@example.com", "mail me at [EMAIL]"),
    ("aadhaar 1234 5678 9012", "aadhaar [ID_NUMBER]"),
    ("call +91 98765 43210", "call [PHONE]"),
    ("my pin 4821", "my pin [SECRET]"),
    ("book me a train ticket to Mumbai", "book me a train ticket to Mumbai"),      # everything else stays verbatim
    ("amount 50000 rupees", "amount 50000 rupees"),
])
def test_redact(raw, clean):
    assert redact(raw) == clean


# ------------------------------------------------------------------------------------------------ the gaps table and the weekly grouping
def test_ensure_schema_is_idempotent_and_creates_the_table():
    c = sqlite3.connect(":memory:")
    ensure_schema(c)
    ensure_schema(c)
    assert c.execute("SELECT name FROM sqlite_master WHERE name='knowledge_gaps'").fetchone()


def test_unknown_gap_kind_is_rejected(conn, clock):
    with pytest.raises(ValueError):
        log_gap(conn, clock, kind="WHATEVER", text="x")


def test_weekly_grouping(conn, clock):
    for case, text in (("A", "Book me a train ticket 123"), ("B", "book me a train ticket!"), ("C", "order a pizza")):
        log_gap(conn, clock, kind="UNSUPPORTED", text=text, merchant_id=f"M-{case}", case_id=case)
    log_gap(conn, clock, kind="NO_ANSWER", text="book me a train ticket", case_id="D")          # same words, other kind: its own group
    clock.advance(timedelta(days=8))
    log_gap(conn, clock, kind="UNSUPPORTED", text="Book me a train ticket", case_id="E")        # next ISO week
    groups = group_gaps_weekly(conn)
    summary = [(g["week"], g["kind"], g["count"]) for g in groups]
    assert len(groups) == 4 and len({g["week"] for g in groups}) == 2
    assert sorted(summary) == sorted([(groups[0]["week"], "NO_ANSWER", 1), (groups[0]["week"], "UNSUPPORTED", 2), (groups[0]["week"], "UNSUPPORTED", 1), (groups[-1]["week"], "UNSUPPORTED", 1)])
    top = max(groups, key=lambda g: g["count"])
    assert top["count"] == 2 and top["merchants"] == ["M-A", "M-B"] and top["gap_ids"] == ["GAP-00001", "GAP-00002"]


def test_a_draft_is_never_auto_enabled(conn, clock):
    gid = log_gap(conn, clock, kind="UNSUPPORTED", text="order a pizza", case_id="A")
    attach_draft(conn, clock, [gid], "We cannot order food. Use a food app.")
    row = conn.execute("SELECT * FROM knowledge_gaps WHERE gap_id=?", (gid,)).fetchone()
    assert (row["status"], row["enabled"], row["approved_by"]) == ("DRAFTED", 0, None)
    with pytest.raises(sqlite3.IntegrityError):                                   # not even raw SQL can enable it without a person
        conn.execute("UPDATE knowledge_gaps SET enabled = 1 WHERE gap_id=?", (gid,))
    with pytest.raises(ValueError):
        approve_draft(conn, [gid], "  ")
    approve_draft(conn, [gid], "kb_owner")
    row = conn.execute("SELECT * FROM knowledge_gaps WHERE gap_id=?", (gid,)).fetchone()
    assert (row["status"], row["enabled"], row["approved_by"]) == ("PUBLISHED", 1, "kb_owner")
    assert group_gaps_weekly(conn) == []                                          # published gaps leave the weekly list


def test_approving_a_gap_that_has_no_draft_enables_nothing(conn, clock):
    gid = log_gap(conn, clock, kind="UNSUPPORTED", text="order a pizza", case_id="A")
    approve_draft(conn, [gid], "kb_owner")
    assert conn.execute("SELECT enabled FROM knowledge_gaps").fetchone()[0] == 0


# ------------------------------------------------------------------------------------------------ grounding
def test_ungrounded_numbers_in_a_draft_reply_are_found():
    a = layer([kb()]).answer("cost", SCOPE)
    assert ungrounded_numbers("Soundbox Standard costs 149 rupees a month.", a) == []
    assert ungrounded_numbers("It costs 199 rupees and 149 rupees", a) == ["199"]
