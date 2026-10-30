import pytest

SYSTEM_READS = [
    "/mock/merchants/M-0001", "/mock/kyc/M-0001", "/mock/bank/M-0001", "/mock/devices/M-0001",
    "/mock/payments?mid=M-0003", "/mock/settlements?mid=M-0003", "/mock/offers/band?mid=M-0003",
    "/mock/plans/M-0003", "/mock/risk/M-0002", "/mock/tickets?mid=M-0002", "/mock/schedule?mid=M-0003",
    "/mock/comms?mid=M-0003", "/mock/lender/eligibility?mid=M-0003", "/mock/refunds?mid=M-0003", "/mock/closures?mid=M-0003",
]


@pytest.mark.parametrize("path", SYSTEM_READS)
def test_all_fifteen_systems_answer_reads(http, path):
    r = http.get(path)
    assert r.status_code == 200, r.text


def test_rejected_kyc_returns_exactly_the_seeded_state(http):
    assert http.get("/mock/kyc/M-0001").json() | {"updated_at": None} == {
        "merchant_id": "M-0001", "status": "REJECTED", "reason": "DOC_BLURRY", "attempts": 1, "updated_at": None}
    assert http.get("/mock/bank/M-0001").json()["reason"] == "TIMEOUT"
    assert http.get("/mock/devices/M-0001").json()["status"] == "OFFLINE"


def test_reads_come_from_the_database_not_hardcoded(http, conn):
    assert http.get("/mock/kyc/M-0004").json()["status"] == "APPROVED"
    conn.execute("UPDATE kyc SET status='PENDING', reason='MANUAL' WHERE merchant_id='M-0004'")
    assert http.get("/mock/kyc/M-0004").json()["status"] == "PENDING"


def test_ten_seeded_merchants_match_the_database(http, conn):
    for mid in [f"M-{i:04d}" for i in range(1, 11)]:
        row = conn.execute("SELECT * FROM merchants WHERE merchant_id=?", (mid,)).fetchone()
        got = http.get(f"/mock/merchants/{mid}").json()
        assert got["name"] == row["name"] and got["language"] == row["language"] and got["city"] == row["city"]


def test_scripted_situations(http):
    held = [s for s in http.get("/mock/settlements?mid=M-0002").json()["settlements"] if s["status"] == "HELD_RISK"]
    assert len(held) == 1 and held[0]["amount_paise"] == 1_240_000
    assert http.get("/mock/risk/M-0002").json()["has_open_hold"] is True
    assert http.get("/mock/devices/M-0012").json()["replacements_last_30d"] == 2
    assert http.get("/mock/devices/M-0007").json()["status"] == "NONE"
    assert http.get("/mock/payments?mid=M-0008").json()["payments"] == []  # dormant
    assert http.get("/mock/payments?mid=M-0010&limit=1").json()["payments"][0]["status"] == "FAILED"


def test_unknown_merchant_is_404(http):
    assert http.get("/mock/kyc/M-9999").status_code == 404
    assert http.get("/mock/devices/M-9999").status_code == 404


def test_offer_band_comes_from_the_pricing_system(http, conn):
    assert http.get("/mock/offers/band?mid=M-0003").json()["max_months"] == 1
    conn.execute("UPDATE offer_bands SET max_months = 3 WHERE segment='restaurant'")
    assert http.get("/mock/offers/band?mid=M-0003").json()["max_months"] == 3


def test_lender_eligibility_is_deterministic(http):
    ok = http.get("/mock/lender/eligibility?mid=M-0003").json()
    assert ok["eligible"] is True and ok["max_amount_paise"] > 0 and ok["indicative_terms_ref"].startswith("kb:")
    bad = http.get("/mock/lender/eligibility?mid=M-0001").json()
    assert bad["eligible"] is False and "KYC_NOT_APPROVED" in bad["reasons"] and bad["max_amount_paise"] == 0
    assert http.get("/mock/lender/eligibility?mid=M-0003").json() == ok
