from pathlib import Path

import pytest

from core.memory.knowledge import KBError, index_docs, load_kb_dir, parse_doc
from core.memory.store import connect, init_schema

KB_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "kb"
REQUIRED = {"kyc_faq", "bank_verification", "device_troubleshooting", "device_replacement_policy", "fee_schedule", "objection_answers",
            "dispute_tat_faq", "safety_lines", "loan_partner_terms", "refund_policy", "closure_policy", "settlement_timelines", "consumer_faq"}


def test_at_least_fifteen_docs_and_all_required_topics():
    docs = load_kb_dir(KB_DIR)
    assert len(docs) >= 15
    assert REQUIRED <= {d.doc_id for d in docs}


def test_every_doc_parses_with_stable_unique_section_ids():
    for d in load_kb_dir(KB_DIR):
        ids = [s.section_id for s in d.sections]
        assert ids and len(ids) == len(set(ids)), d.doc_id
        assert d.version >= 1 and d.title and d.scopes
        assert all(s.text.strip() for s in d.sections)


def test_citations_used_elsewhere_in_the_repo_resolve():
    chunks = {d.chunk_id(s.section_id) for d in load_kb_dir(KB_DIR) for s in d.sections}
    for cited in ("loan_partner_terms#indicative_terms@v1",   # packs/paytm_merchant/mocks/lender.py
                  "kyc_faq#accepted_docs@v1", "fee_schedule#soundbox_plans@v1"):  # scripts/spike_data.py
        assert cited in chunks


def test_facts_match_what_the_mocks_do():
    text = {d.doc_id: " ".join(s.text for s in d.sections) for d in load_kb_dir(KB_DIR)}
    assert "149 rupees" in text["fee_schedule"] and "249 rupees" in text["fee_schedule"]
    assert "24 hours" in text["device_replacement_policy"]
    assert "48 hours" in text["kyc_faq"] and "48 hours" in text["closure_policy"]
    assert "7 days" in text["fee_schedule"]
    assert "10 am" in text["settlement_timelines"]


def test_regulatory_wording_stays_generic_until_primary_texts_are_read():
    text = " ".join(s.text for d in load_kb_dir(KB_DIR) for s in d.sections)
    for banned in ("T+1", "T+5", "₹100", "100 rupees a day", "RBI circular", "within 5 days", "within 1 day"):
        assert banned not in text, banned
    assert "Key Fact Statement" in text and "Paytm never asks for your PIN or OTP" in text


def test_no_doc_claims_review_it_has_not_had():
    # Sign-off is a human act (#12). Nothing may mark itself reviewed.
    assert {d.review_status for d in load_kb_dir(KB_DIR)} == {"DRAFT_UNREVIEWED"}


def test_indexing_populates_chunks_and_full_text_search(tmp_path):
    conn = connect(tmp_path / "kb.db")
    init_schema(conn)
    docs = load_kb_dir(KB_DIR)
    n_docs, n_chunks = index_docs(conn, docs)
    assert n_docs == len(docs) and n_chunks == sum(len(d.sections) for d in docs)
    hit = conn.execute("SELECT chunk_id FROM kb_chunks_fts WHERE kb_chunks_fts MATCH 'blurry' ").fetchall()
    assert any("kyc_faq" in r[0] for r in hit)
    index_docs(conn, docs)  # re-indexing replaces, never duplicates
    assert conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0] == n_chunks
    assert conn.execute("SELECT COUNT(*) FROM kb_chunks_fts").fetchone()[0] == n_chunks


@pytest.mark.parametrize("body,msg", [
    ("---\nid: x\nversion: 1\ntitle: T\nscopes: [x]\nreview_status: DRAFT_UNREVIEWED\n---\n## No id here\ntext", "stable id"),
    ("---\nid: x\nversion: 1\ntitle: T\nscopes: [x]\nreview_status: DRAFT_UNREVIEWED\n---\n## A {#a}\n\n## B {#b}\ntext", "empty"),
    ("---\nid: x\nversion: 1\ntitle: T\nscopes: [x]\nreview_status: DRAFT_UNREVIEWED\n---\n## A {#a}\nt\n## B {#a}\nt", "duplicate"),
    ("---\nid: wrongname\nversion: 1\ntitle: T\nscopes: [x]\nreview_status: DRAFT_UNREVIEWED\n---\n## A {#a}\nt", "file name"),
    ("---\nid: x\nversion: 0\ntitle: T\nscopes: [x]\nreview_status: DRAFT_UNREVIEWED\n---\n## A {#a}\nt", "version"),
    ("---\nid: x\nversion: 1\ntitle: T\nscopes: [x]\nreview_status: APPROVED_BY_ROBOT\n---\n## A {#a}\nt", "review_status"),
    ("no front matter", "front matter"),
])
def test_malformed_docs_are_rejected(tmp_path, body, msg):
    p = tmp_path / "x.md"
    p.write_text(body)
    with pytest.raises(KBError, match=msg):
        parse_doc(p)
