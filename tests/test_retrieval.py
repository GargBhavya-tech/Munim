"""Tests for hybrid retrieval (#44): RRF numbers, FTS5 sanitising, scope, versions, multilingual quality."""
from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from core.gate.embedder import DEFAULT_MODEL, HashEmbedder, SentenceTransformerEmbedder
from core.gate.retrieval import (DEFAULT_RERANKER, CrossEncoderReranker, Retriever, fts_query, index_vectors, rrf_score)
from core.memory.knowledge import KBDoc, Section, index_docs, load_kb_dir
from core.memory.store import connect, init_schema

KB_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "kb"

# ---------------------------------------------------------------------------------------------------------------
# Hand-labelled retrieval set: (language, query, acceptable "doc#section" answers). 6 per language.
# Hindi / Kannada phrasing is deliberately simple; a native-speaker review is expected.
# ---------------------------------------------------------------------------------------------------------------
LABELLED = [
    ("en", "What is the interest rate on the loan?", {"loan_partner_terms#indicative_terms"}),
    ("en", "Which documents are accepted for KYC?", {"kyc_faq#accepted_docs"}),
    ("en", "Can I get a replacement for my faulty Soundbox?", {"device_replacement_policy#when_allowed", "device_replacement_policy#cost"}),
    ("en", "Why did bank verification fail with NAME_MISMATCH?", {"bank_verification#failure_reasons"}),
    ("en", "Why is my settlement held for review?", {"settlement_timelines#held_review", "risk_hold_policy#what_it_is"}),
    ("en", "For how long can I cancel a closure request?", {"closure_policy#cancel_window"}),
    ("hi", "पैसा बैंक में नहीं आया", {"settlement_timelines#normal_timing", "settlement_timelines#delayed", "settlement_timelines#bank_failure"}),
    ("hi", "KYC के लिए कौन से दस्तावेज़ चाहिए?", {"kyc_faq#accepted_docs"}),
    ("hi", "मेरा साउंडबॉक्स बंद हो गया है", {"device_troubleshooting#first_checks", "device_troubleshooting#remote_steps"}),
    ("hi", "लोन पर ब्याज कितना लगेगा?", {"loan_partner_terms#indicative_terms"}),
    ("hi", "ग्राहक को रिफंड कैसे किया जाता है?", {"refund_policy#how_it_works"}),
    ("hi", "मुझे अपना खाता बंद करना है", {"closure_policy#closing"}),
    ("hinglish", "paisa bank nahi pahuncha", {"settlement_timelines#normal_timing", "settlement_timelines#delayed", "settlement_timelines#bank_failure"}),
    ("hinglish", "Soundbox band hai", {"device_troubleshooting#first_checks", "device_troubleshooting#remote_steps"}),
    ("hinglish", "KYC reject kyun hua", {"kyc_faq#rejection_reasons"}),
    ("hinglish", "Soundbox ka mahine ka charge kitna hai", {"fee_schedule#soundbox_plans"}),
    ("hinglish", "loan ka rate kitna hoga", {"loan_partner_terms#indicative_terms"}),
    ("hinglish", "bank verification fail ho gaya", {"bank_verification#failure_reasons"}),
    ("kn", "KYC ಗೆ ಯಾವ ದಾಖಲೆಗಳು ಬೇಕು?", {"kyc_faq#accepted_docs"}),
    ("kn", "ನನ್ನ ಹಣ ಬ್ಯಾಂಕ್ ಖಾತೆಗೆ ಬಂದಿಲ್ಲ", {"settlement_timelines#normal_timing", "settlement_timelines#delayed", "settlement_timelines#bank_failure"}),
    ("kn", "ಸೌಂಡ್‌ಬಾಕ್ಸ್ ಕೆಲಸ ಮಾಡುತ್ತಿಲ್ಲ", {"device_troubleshooting#first_checks", "device_troubleshooting#remote_steps"}),
    ("kn", "ಸಾಲದ ಬಡ್ಡಿ ದರ ಎಷ್ಟು?", {"loan_partner_terms#indicative_terms"}),
    ("kn", "ರೀಫಂಡ್ ಹೇಗೆ ಮಾಡುವುದು?", {"refund_policy#how_it_works"}),
    ("kn", "ನನ್ನ ಖಾತೆಯನ್ನು ಮುಚ್ಚಬೇಕು", {"closure_policy#closing"}),
]


def _ref(hit) -> str:
    return f"{hit.doc_id}#{hit.section_id}"


@pytest.fixture(scope="module")
def kb_conn(tmp_path_factory):
    conn = connect(tmp_path_factory.mktemp("kb") / "kb.db")
    init_schema(conn)
    index_docs(conn, load_kb_dir(KB_DIR))
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def hash_retriever(kb_conn):
    emb = HashEmbedder()
    index_vectors(kb_conn, emb)
    return Retriever(kb_conn, emb)


# ---------------------------------------------------------------------------------------------------------------
# Fast tests (no model download)
# ---------------------------------------------------------------------------------------------------------------
def test_rrf_worked_example_from_bible():
    # Bible: 0.01639 + 0.01587 = 0.03226 (it sums the rounded terms; the exact sum is 0.0322665, so tolerance is 1e-5)
    assert rrf_score([1, 3]) == pytest.approx(0.03226, abs=1e-5)
    assert rrf_score([None, 2]) == pytest.approx(0.01613, abs=1e-5)  # 1/62, dense only
    assert rrf_score([1, 3]) == pytest.approx(1 / 61 + 1 / 63)
    assert rrf_score([None, None]) == 0.0


def test_hit_scores_use_the_rrf_formula(hash_retriever):
    hits = hash_retriever.search("GST certificate Udyam", k=5)
    for h in hits:
        s = h.scores
        expected = sum(1 / (60 + r) for r in (s["bm25_rank"], s["dense_rank"]) if r is not None)
        assert s["rrf"] == pytest.approx(expected)
    assert [h.scores["rrf"] for h in hits] == sorted((h.scores["rrf"] for h in hits), reverse=True)  # no reranker: RRF order


def test_exact_terms_found_by_bm25(hash_retriever):
    hits = hash_retriever.search("GST certificate", k=3)
    assert _ref(hits[0]) == "kyc_faq#accepted_docs"
    assert hits[0].scores["bm25_rank"] == 1
    assert hash_retriever.search("DOC_BLURRY", k=3)[0].scores["bm25_rank"] == 1
    assert _ref(hash_retriever.search("DOC_BLURRY", k=1)[0]) == "kyc_faq#rejection_reasons"


def test_ifsc_exact_term_via_bm25_even_when_dense_is_blind():
    """IFSC is not in the shipped KB; add a doc containing it and show BM25 puts it first (HashEmbedder has no idea about IFSC)."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    docs = load_kb_dir(KB_DIR)
    docs.append(KBDoc("ifsc_help", 1, "IFSC help", ("ifsc_help",), "DRAFT_UNREVIEWED",
                      (Section("what_is_ifsc", "Bank branch code", "The IFSC code is eleven characters long and identifies the bank branch."),), "x"))
    index_docs(conn, docs)
    emb = HashEmbedder()
    index_vectors(conn, emb)
    r = Retriever(conn, emb)
    top = r.search("IFSC", k=3)[0]
    assert _ref(top) == "ifsc_help#what_is_ifsc" and top.scores["bm25_rank"] == 1 and top.method in ("bm25", "hybrid")


def test_hits_carry_provenance_and_parent_text(hash_retriever):
    hits = hash_retriever.search("Which documents are accepted for KYC", k=5)
    h = hits[0]
    assert (h.doc_id, h.version, h.section_id) == ("kyc_faq", 1, "accepted_docs")
    assert h.chunk_id == "kyc_faq#accepted_docs@v1" and h.heading == "Accepted documents"
    assert set(h.scores) >= {"bm25_rank", "dense_rank", "rrf", "rerank"} and h.method in ("bm25", "dense", "hybrid")
    assert h.text in h.parent_text and "## Why a KYC can be rejected" in h.parent_text  # parent = the whole doc
    assert all(x.parent_text for x in hits)


def test_scope_restriction_never_returns_out_of_scope_docs(hash_retriever):
    for q in ("GST certificate", "loan interest rate", "Soundbox band hai", "refund", "KYC"):
        hits = hash_retriever.search(q, scope=["loan_partner_terms"], k=5)
        assert hits and {h.doc_id for h in hits} == {"loan_partner_terms"}
    hits = hash_retriever.search("GST certificate", scope=["fee_schedule", "refund_policy"], k=5)
    assert {h.doc_id for h in hits} <= {"fee_schedule", "refund_policy"}
    assert "kyc_faq" not in {h.doc_id for h in hash_retriever.search("GST certificate", scope=["fee_schedule"], k=10)}
    assert hash_retriever.search("GST certificate", scope=[], k=5) == []
    assert hash_retriever.search("GST certificate", scope=["no_such_scope"], k=5) == []
    assert hash_retriever.best_score("GST certificate", scope=["no_such_scope"]) == 0.0


def test_scope_uses_each_comma_separated_scope_of_a_doc(tmp_path):
    conn = connect(tmp_path / "s.db")
    init_schema(conn)
    docs = [KBDoc("multi", 1, "Multi", ("alpha", "beta"), "DRAFT_UNREVIEWED", (Section("s", "Cats", "Cats purr softly."),), "h1"),
            KBDoc("other", 1, "Other", ("alphabet",), "DRAFT_UNREVIEWED", (Section("s", "Cats too", "Cats purr loudly."),), "h2")]
    index_docs(conn, docs)
    emb = HashEmbedder()
    index_vectors(conn, emb)
    r = Retriever(conn, emb)
    assert {h.doc_id for h in r.search("cats purr", scope=["beta"])} == {"multi"}
    assert {h.doc_id for h in r.search("cats purr", scope=["alpha"])} == {"multi"}  # 'alphabet' must not match 'alpha'
    assert {h.doc_id for h in r.search("cats purr", scope=["alpha", "alphabet"])} == {"multi", "other"}


def test_old_doc_versions_are_never_returned(tmp_path):
    """Watch out: indexing old versions alongside current. Stale chunk rows in kb_chunks/FTS/vectors must stay invisible."""
    conn = connect(tmp_path / "v.db")
    init_schema(conn)
    v1 = KBDoc("policy", 1, "Policy", ("policy",), "DRAFT_UNREVIEWED", (Section("fee", "Fee", "The old fee is ninety nine rupees."),), "h1")
    index_docs(conn, [v1])
    emb = HashEmbedder()
    index_vectors(conn, emb)
    assert Retriever(conn, emb).search("old fee ninety nine rupees")[0].version == 1
    # the doc moves to v2 but a stale v1 chunk (plus its FTS row and vector) is left behind by a sloppy migration
    stale_vec = conn.execute("SELECT model, dim, vector FROM kb_vectors").fetchone()
    v2 = KBDoc("policy", 2, "Policy", ("policy",), "DRAFT_UNREVIEWED", (Section("fee", "Fee", "The new fee is one hundred forty nine rupees."),), "h2")
    index_docs(conn, [v2])
    conn.execute("INSERT INTO kb_chunks VALUES ('policy#fee@v1','policy',1,'fee','Fee','The old fee is ninety nine rupees.')")
    conn.execute("INSERT INTO kb_chunks_fts VALUES ('policy#fee@v1','Fee','The old fee is ninety nine rupees.')")
    index_vectors(conn, emb)
    conn.execute("INSERT OR REPLACE INTO kb_vectors VALUES ('policy#fee@v1',?,?,?)", tuple(stale_vec))
    r = Retriever(conn, emb)
    for q in ("old fee ninety nine rupees", "what is the fee", "fee"):
        hits = r.search(q, k=10)
        assert hits and {h.chunk_id for h in hits} == {"policy#fee@v2"}, q
    assert conn.execute("SELECT COUNT(*) FROM kb_vectors WHERE chunk_id LIKE '%@v1'").fetchone()[0] == 1  # stale row exists, still invisible
    index_vectors(conn, emb)  # re-indexing drops it
    assert conn.execute("SELECT COUNT(*) FROM kb_vectors WHERE chunk_id LIKE '%@v1'").fetchone()[0] == 0


def test_index_vectors_fills_one_vector_per_current_chunk(kb_conn):
    emb = HashEmbedder()
    n = index_vectors(kb_conn, emb)
    chunks = kb_conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
    assert n == chunks > 50
    row = kb_conn.execute("SELECT model, dim, length(vector) AS n FROM kb_vectors LIMIT 1").fetchone()
    assert (row["model"], row["dim"], row["n"]) == ("hash-256", 256, 256 * 4)
    assert Retriever(kb_conn, emb).search("x") is not None


def test_dense_ignores_vectors_of_a_different_model(kb_conn):
    index_vectors(kb_conn, HashEmbedder(256))
    r = Retriever(kb_conn, HashEmbedder(128))   # no vectors for hash-128 -> dense contributes nothing, bm25 still works
    hits = r.search("GST certificate")
    assert hits and all(h.scores["dense_rank"] is None for h in hits)
    index_vectors(kb_conn, HashEmbedder(256))


def test_fts_query_examples():
    assert fts_query("what's the IFSC?") == '"what" OR "s" OR "the" OR "IFSC"'
    assert fts_query('NEAR(a b) AND "x" OR -y*') == '"NEAR" OR "a" OR "b" OR "AND" OR "x" OR "OR" OR "y"'
    assert fts_query("   ... !!! ") == ""
    assert fts_query("पैसा बैंक") == '"पैसा" OR "बैंक"'   # matras survive


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(max_size=80))
def test_arbitrary_text_never_raises(hash_retriever, text):
    hits = hash_retriever.search(text, k=3)
    assert isinstance(hits, list)
    assert 0.0 <= hash_retriever.best_score(text) <= 1.0


@pytest.mark.parametrize("q", ["don't", '"unbalanced', "a AND", "NOT", "(", "*", "col:val", "x' OR '1'='1", "hello -- world", "\x00", "ಹಣ'", "NEAR/2"])
def test_nasty_queries_do_not_raise(hash_retriever, q):
    assert isinstance(hash_retriever.search(q), list)


# ---------------------------------------------------------------------------------------------------------------
# Real models (first run downloads them from Hugging Face; set MUNIM_SKIP_MODEL_TESTS=1 to skip)
# ---------------------------------------------------------------------------------------------------------------
needs_models = pytest.mark.skipif(os.environ.get("MUNIM_SKIP_MODEL_TESTS") == "1", reason="model tests disabled")


@pytest.fixture(scope="module")
def real_retriever(kb_conn):
    emb = SentenceTransformerEmbedder(DEFAULT_MODEL)
    index_vectors(kb_conn, emb)
    return Retriever(kb_conn, emb, CrossEncoderReranker(DEFAULT_RERANKER))


@pytest.fixture(scope="module")
def real_retriever_no_rerank(kb_conn, real_retriever):
    return Retriever(kb_conn, real_retriever.embedder, None)


@needs_models
@pytest.mark.parametrize("query,expected", [
    ("paisa bank nahi pahuncha", {"settlement_timelines"}),
    ("ಕೆವೈಸಿಗೆ ಯಾವ ದಾಖಲೆಗಳು ಬೇಕು?", {"kyc_faq"}),
    ("KYC ಗೆ ಯಾವ ದಾಖಲೆಗಳು ಬೇಕು?", {"kyc_faq"}),
    ("Soundbox band hai", {"device_troubleshooting"}),
    ("What interest rate will the loan partner charge?", {"loan_partner_terms"}),
])
def test_ticket_queries_land_in_top_3(real_retriever, query, expected):
    top3 = real_retriever.search(query, k=3)
    assert expected & {h.doc_id for h in top3}, [(_ref(h), h.method) for h in top3]


@needs_models
def test_kannada_kyc_documents_hits_accepted_docs_section(real_retriever):
    refs = [_ref(h) for h in real_retriever.search("KYC ಗೆ ಯಾವ ದಾಖಲೆಗಳು ಬೇಕು?", k=3)]
    assert "kyc_faq#accepted_docs" in refs


@needs_models
def test_real_scope_restriction(real_retriever):
    hits = real_retriever.search("paisa bank nahi pahuncha", scope=["fee_schedule", "kyc_faq"], k=5)
    assert hits and {h.doc_id for h in hits} <= {"fee_schedule", "kyc_faq"}


@needs_models
def test_hit_rate_on_labelled_set(real_retriever, real_retriever_no_rerank, capsys):
    assert len(LABELLED) >= 24 and all(sum(1 for x in LABELLED if x[0] == l) == 6 for l in ("en", "hi", "hinglish", "kn"))
    res = {}
    no_gloss = Retriever(real_retriever.conn, real_retriever.embedder, real_retriever.reranker, glossary={})  # ablation, report only
    for name, r in (("dense+bm25+rrf", real_retriever_no_rerank), ("+rerank", real_retriever), ("+rerank, NO glossary (ablation)", no_gloss)):
        per = defaultdict(lambda: [0, 0, 0])  # top1, top3, top5 hits
        misses = []
        for lang, q, ok in LABELLED:
            refs = [_ref(h) for h in r.search(q, k=5)]
            pos = next((i for i, x in enumerate(refs) if x in ok), None)
            per[lang][0] += pos == 0
            per[lang][1] += pos is not None and pos < 3
            per[lang][2] += pos is not None
            if pos is None:
                misses.append((lang, q, refs[:3]))
        res[name] = (per, misses)
    with capsys.disabled():
        for name, (per, misses) in res.items():
            tot = [sum(v[i] for v in per.values()) for i in range(3)]
            print(f"\n[retrieval {name}] embedder={DEFAULT_MODEL}")
            for lang in ("en", "hi", "hinglish", "kn"):
                print(f"  {lang:9s} top1={per[lang][0]}/6 top3={per[lang][1]}/6 top5={per[lang][2]}/6")
            print(f"  ALL       top1={tot[0]}/24 top3={tot[1]}/24 top5={tot[2]}/24   misses={misses}")
    per, _ = res["+rerank"]
    total5 = sum(v[2] for v in per.values())
    assert total5 >= 20, f"top-5 hit rate {total5}/24"
    for lang in ("en", "hi", "hinglish", "kn"):
        assert per[lang][2] >= 5, f"{lang}: {per[lang][2]}/6"


OFF_TOPIC = ["what is the weather today in Bengaluru", "who won the cricket world cup", "recipe for chicken biryani", "आज मौसम कैसा है",
             "ಇಂದು ಹವಾಮಾನ ಹೇಗಿದೆ", "meri shaadi ke liye gift batao", "best stocks to buy this month", "how do I learn to play the guitar",
             "mujhe naya phone kharidna hai", "tell me a joke"]


@needs_models
def test_rerank_scores_present_and_best_score_separates_known_from_novel(real_retriever, capsys):
    import statistics
    hits = real_retriever.search("Which documents are accepted for KYC?", k=3)
    assert all(h.scores["rerank"] is not None and h.method.endswith("+rerank") for h in hits)
    assert [h.scores["final"] for h in hits] == sorted((h.scores["final"] for h in hits), reverse=True)
    known = [real_retriever.best_score(q) for _, q, _ in LABELLED]
    novel = [real_retriever.best_score(q) for q in OFF_TOPIC]
    above = sum(k > max(novel) for k in known) / len(known)
    with capsys.disabled():
        print(f"\n[best_score] in-KB median={statistics.median(known):.3f} min={min(known):.3f}; off-topic max={max(novel):.3f} "
              f"median={statistics.median(novel):.3f}; in-KB above all off-topic: {above:.0%}")
    assert all(0.0 <= x <= 1.0 for x in known + novel)
    assert statistics.median(known) > 5 * max(novel)
    assert above >= 0.7


@needs_models
def test_latency_report(real_retriever, capsys):
    t0 = time.perf_counter()
    real_retriever.search("Soundbox band hai")
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(5):
        real_retriever.search("loan ka rate kitna hoga")
    warm = (time.perf_counter() - t0) / 5
    with capsys.disabled():
        print(f"\n[latency] search first call (models already loaded by fixture)={first * 1000:.0f} ms, warm={warm * 1000:.0f} ms")
    assert warm < 5.0
