"""#138 labelled claim set: every label is re-derived from the KB documents and the seeded database, and the
directory is frozen by a sha256 manifest. Independent of the model layer, the gate and any prompt."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLAIMS_DIR = ROOT / "eval" / "frozen" / "claims"
KB_DIR = ROOT / "packs" / "paytm_merchant" / "kb"
LANGS = ("en", "hi", "kn", "hinglish")
LABELS = ("true", "false", "unsupported")
DV = str.maketrans("०१२३४५६७८९", "0123456789")
KD = str.maketrans("೦೧೨೩೪೫೬೭೮೯", "0123456789")
MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    "hi": ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"],
    "kn": ["ಜನವರಿ", "ಫೆಬ್ರವರಿ", "ಮಾರ್ಚ್", "ಏಪ್ರಿಲ್", "ಮೇ", "ಜೂನ್", "ಜುಲೈ", "ಆಗಸ್ಟ್", "ಸೆಪ್ಟೆಂಬರ್", "ಅಕ್ಟೋಬರ್", "ನವೆಂಬರ್", "ಡಿಸೆಂಬರ್"],
}
MONTHS["hinglish"] = MONTHS["en"]


def load_items() -> list[dict]:
    with open(CLAIMS_DIR / "claims.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def parse_kb() -> dict[str, dict[str, str]]:
    docs: dict[str, dict[str, str]] = {}
    for p in sorted(KB_DIR.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        secs: dict[str, str] = {}
        parts = re.split(r"^## .*?\{#([a-z_0-9]+)\}\s*$", text, flags=re.M)
        for i in range(1, len(parts), 2):
            secs[parts[i]] = " ".join(parts[i + 1].split())
        docs[p.stem] = secs
    return docs


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    sys.path.insert(0, str(ROOT))
    from data.seed import seed

    path = tmp_path_factory.mktemp("claimdb") / "m.db"
    seed(path, 42)
    c = sqlite3.connect(path)
    yield c
    c.close()


@pytest.fixture(scope="module")
def kb():
    return parse_kb()


def numbers_in(text: str) -> set[str]:
    t = text.translate(DV).translate(KD)
    t = re.sub(r"(?<=\d),(?=\d)", "", t)
    return set(re.findall(r"\d+(?:\.\d+)?", t))


def truth_value(item: dict, conn, kb) -> tuple[bool, str | None, list[str]]:
    """Returns (source_found, value, errors)."""
    tr = item["truth"]
    errs: list[str] = []
    if tr["kind"] == "kb":
        sec = kb.get(tr["doc"], {}).get(tr["section"])
        if sec is None:
            return False, None, [f"missing KB section {tr['doc']}#{tr['section']}"]
        m = re.search(tr["regex"], sec)
        if not m:
            return False, None, [f"regex does not match {tr['doc']}#{tr['section']}"]
        if m.re.groups != 1:
            errs.append("kb regex must have exactly one capture group")
        return True, (m.group(1) if m.re.groups else m.group(0)), errs
    if tr["kind"] == "db":
        row = conn.execute(tr["sql"]).fetchone()
        if row is None:
            return False, None, ["sql returned no row"]
        return True, str(row[0]), errs
    return False, None, errs


def check_item(item: dict, conn, kb) -> list[str]:
    """Every reason this item's label or text is wrong against its source. Empty list = consistent."""
    errs: list[str] = []
    label, tr = item["label"], item["truth"]
    if label not in LABELS:
        return [f"bad label {label}"]
    if label == "unsupported":
        if tr["kind"] != "none":
            errs.append("unsupported claim must have truth.kind == none")
        allkb = " ".join(" ".join(s.values()) for s in kb.values())
        for rx in tr.get("kb_absent", []):
            if re.search(rx, allkb, flags=re.I):
                errs.append(f"KB says something matching {rx!r}; claim is not unsupported")
        for sql in tr.get("db_absent", []):
            if conn.execute(sql).fetchall():
                errs.append(f"db has rows for {sql!r}; claim is not unsupported")
        if not tr.get("kb_absent") and not tr.get("db_absent"):
            errs.append("unsupported claim names nothing that must be absent")
    else:
        found, val, e = truth_value(item, conn, kb)
        errs += e
        if not found:
            errs.append("true/false claim has no source value")
        elif label == "true" and str(item["claimed"]) != val:
            errs.append(f"label true but claimed {item['claimed']!r} != source {val!r}")
        elif label == "false" and str(item["claimed"]) == val:
            errs.append(f"label false but claimed equals source {val!r}")
    # the sentence must actually assert the claimed value
    text, kind, claimed = item["text"], item["claimed_kind"], str(item["claimed"])
    for tok in item["must_contain"]:
        if tok not in text:
            errs.append(f"text lacks key phrase {tok!r}")
    nums = numbers_in(text)
    if kind == "number" and claimed not in nums:
        errs.append(f"text does not state number {claimed}")
    elif kind == "paise":
        if int(claimed) % 100:
            errs.append("paise claim with fractional rupees")
        elif str(int(claimed) // 100) not in nums:
            errs.append(f"text does not state {int(claimed) // 100} rupees for {claimed} paise")
    elif kind == "date":
        y, m, d = claimed.split("-")
        if str(int(d)) not in nums or y not in nums or MONTHS[item["lang"]][int(m) - 1] not in text:
            errs.append(f"text does not state date {claimed}")
    elif kind == "word" and not item["must_contain"]:
        errs.append("word claim needs must_contain")
    return errs


# ------------------------------------------------------------------ shape of the set
def test_size_and_balance():
    items = load_items()
    assert len(items) >= 200
    by_lang = Counter(i["lang"] for i in items)
    assert set(by_lang) == set(LANGS)
    assert all(n >= 50 for n in by_lang.values()), by_lang
    by_label = Counter(i["label"] for i in items)
    assert set(by_label) == set(LABELS)
    assert all(by_label[l] / len(items) >= 0.15 for l in LABELS), by_label
    cats = {i["category"] for i in items}
    assert {"fee", "date", "amount", "kyc_status", "settlement_status", "bank_status", "device", "promise", "compliance", "dispute"} <= cats


def test_ids_unique_and_groups_parallel_across_languages():
    items = load_items()
    assert len({i["id"] for i in items}) == len(items)
    groups = defaultdict(list)
    for i in items:
        groups[i["group"]].append(i)
    for g, rows in groups.items():
        assert sorted(r["lang"] for r in rows) == sorted(LANGS), g
        for field in ("label", "category", "truth", "claimed", "claimed_kind"):
            assert len({json.dumps(r[field], sort_keys=True) for r in rows}) == 1, (g, field)
        assert len({r["text"] for r in rows}) == 4


def test_money_is_integer_paise_and_ints_only():
    for i in load_items():
        if i["claimed_kind"] == "paise":
            assert re.fullmatch(r"\d+", str(i["claimed"])), i["id"]
    assert sum(1 for i in load_items() if i["claimed_kind"] == "paise") >= 20
    assert sum(1 for i in load_items() if i["claimed_kind"] == "date") >= 10


def test_scripts_match_language():
    dev, kan = re.compile("[ऀ-ॿ]"), re.compile("[ಀ-೿]")
    for i in load_items():
        t = i["text"]
        if i["lang"] == "hi":
            assert dev.search(t) and not kan.search(t), i["id"]
        elif i["lang"] == "kn":
            assert kan.search(t) and not dev.search(t), i["id"]
        else:
            assert not dev.search(t) and not kan.search(t), i["id"]
        assert "‌" not in t and "‍" not in t, i["id"]
        assert "{" not in t and "[[" not in t and t == t.strip()


def test_indic_digits_are_present_for_normalisation_coverage():
    items = load_items()
    assert any(re.search("[०-९]", i["text"]) for i in items if i["lang"] == "hi")
    assert any(re.search("[೦-೯]", i["text"]) for i in items if i["lang"] == "kn")


def test_native_review_file_and_excluded_phrases():
    nr = json.loads((CLAIMS_DIR / "NATIVE_REVIEW.json").read_text(encoding="utf-8"))
    assert nr["excluded"] and nr["requested_review"]
    texts = "\n".join(i["text"] for i in load_items()).lower()
    for ph in nr["excluded_phrases_that_must_not_appear"]:
        assert ph.lower() not in texts
    for r in nr["requested_review"]:
        assert any(r["phrase"] in i["text"] for i in load_items() if i["lang"] == r["lang"]), r


def test_set_is_written_independently_of_model_layer():
    # Watch out: the prompt author writing the labels. The set's own files must not touch the model layer.
    for p in [CLAIMS_DIR / "build_claims.py", CLAIMS_DIR / "freeze.py"]:
        src = p.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+core\.(llm|gate|engine)", src, flags=re.M), p
    src = Path(__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+core\.(llm|gate|engine)", src, flags=re.M)


# ------------------------------------------------------------------ every label against its source
def test_every_label_matches_the_kb_or_the_seed_data(conn, kb):
    bad = {i["id"]: errs for i in load_items() if (errs := check_item(i, conn, kb))}
    assert not bad, json.dumps(bad, ensure_ascii=False, indent=1)[:3000]


def test_every_kb_source_exists_and_every_true_item_cites_a_real_value(conn, kb):
    kinds = Counter(i["truth"]["kind"] for i in load_items())
    assert kinds["kb"] > 40 and kinds["db"] > 40 and kinds["none"] > 30
    for i in load_items():
        if i["truth"]["kind"] == "kb":
            assert i["truth"]["section"] in kb[i["truth"]["doc"]]


# ------------------------------------------------------------------ the checker itself can fail
def _first(label, kind=None):
    return next(i for i in load_items() if i["label"] == label and i["lang"] == "en" and (kind is None or i["truth"]["kind"] == kind))


@pytest.mark.parametrize("src_kind", ["kb", "db"])
def test_checker_rejects_a_true_label_on_a_wrong_value(conn, kb, src_kind):
    item = dict(_first("true", src_kind))
    assert check_item(item, conn, kb) == []
    item["claimed"] = "999"
    assert any("label true" in e for e in check_item(item, conn, kb))


@pytest.mark.parametrize("src_kind", ["kb", "db"])
def test_checker_rejects_a_false_label_on_a_matching_value(conn, kb, src_kind):
    f = _first("false", src_kind)
    t = next(i for i in load_items() if i["truth"] == f["truth"] and i["label"] == "true" and i["lang"] == "en") if any(
        i["truth"] == f["truth"] and i["label"] == "true" for i in load_items()) else None
    item = dict(f)
    assert check_item(item, conn, kb) == []
    _, val, _ = truth_value(item, conn, kb)
    item["claimed"] = val
    assert any("label false" in e for e in check_item(item, conn, kb))
    del t


def test_checker_rejects_unsupported_label_when_the_kb_does_say_it(conn, kb):
    item = dict(_first("unsupported"))
    assert check_item(item, conn, kb) == []
    item["truth"] = {"kind": "none", "kb_absent": [r"149 rupees"], "db_absent": []}
    assert any("not unsupported" in e for e in check_item(item, conn, kb))
    item["truth"] = {"kind": "none", "kb_absent": [], "db_absent": ["SELECT 1 FROM merchants WHERE merchant_id='M-0001'"]}
    assert any("db has rows" in e for e in check_item(item, conn, kb))


def test_checker_rejects_text_that_does_not_state_the_claimed_amount(conn, kb):
    item = dict(next(i for i in load_items() if i["claimed_kind"] == "paise" and i["lang"] == "en" and i["claimed"] != "0"))
    assert check_item(item, conn, kb) == []
    item["text"] = "Your settlement is ₹1."
    assert any("does not state" in e for e in check_item(item, conn, kb))
    item = dict(next(i for i in load_items() if i["claimed_kind"] == "date" and i["lang"] == "kn"))
    item["text"] = item["text"].replace(MONTHS["kn"][10], MONTHS["kn"][0])
    assert any("does not state date" in e for e in check_item(item, conn, kb))


def test_checker_rejects_a_missing_section_or_a_drifted_seed_value(conn, kb):
    item = dict(_first("true", "kb"))
    item["truth"] = dict(item["truth"], section="no_such_section")
    assert any("missing KB section" in e for e in check_item(item, conn, kb))
    item = dict(_first("true", "db"))
    item["claimed"] = str(int(item["claimed"]) + 100) if item["claimed"].isdigit() else "X"
    assert check_item(item, conn, kb)


def test_known_seed_facts_are_what_the_set_assumes(conn):
    assert conn.execute("SELECT amount_paise FROM settlements WHERE merchant_id='M-0002' AND status='HELD_RISK'").fetchone()[0] == 1240000
    assert conn.execute("SELECT billing_date FROM plans WHERE merchant_id='M-0009'").fetchone()[0] == "2026-11-03"
    assert conn.execute("SELECT monthly_fee_paise FROM plans WHERE merchant_id='M-0001'").fetchone()[0] == 14900


# ------------------------------------------------------------------ freeze
def _manifest_files():
    return sorted(p for p in CLAIMS_DIR.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256" and "__pycache__" not in p.parts and p.suffix != ".pyc")


def test_frozen_manifest_matches_every_file():
    mf = CLAIMS_DIR / "MANIFEST.sha256"
    assert mf.exists(), "run: python eval/frozen/claims/freeze.py (only when the set is final)"
    recorded = {}
    for line in mf.read_text(encoding="utf-8").splitlines():
        h, name = line.split("  ", 1)
        recorded[name] = h
    actual = {p.relative_to(CLAIMS_DIR).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in _manifest_files()}
    assert set(recorded) == set(actual), (set(recorded) ^ set(actual))
    changed = [n for n in actual if actual[n] != recorded[n]]
    assert not changed, f"frozen claim set changed after freezing: {changed}"
    assert {"claims.jsonl", "NATIVE_REVIEW.json", "README.md", "build_claims.py", "freeze.py"} <= set(recorded)
