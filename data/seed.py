"""Deterministic synthetic data. ``python data/seed.py --seed 42`` always yields the same database.

Everything here is synthetic. Shop names are invented, phone numbers are fake, and no real person's
data is used. Determinism rules: one seeded ``random.Random``, ids from counters, times from the
fixed sim start, no iteration over sets, no ``hash()``.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.engine.clock import DEFAULT_START, iso  # noqa: E402
from core.memory.knowledge import index_docs, load_kb_dir  # noqa: E402
from core.memory.store import connect, init_schema, write_txn  # noqa: E402

START = DEFAULT_START
KB_DIR = ROOT / "packs" / "paytm_merchant" / "kb"
DEFAULT_DB = ROOT / "data" / "munim.db"

CITIES = {
    "Hubballi": dict(pins=["580020", "580029", "580031"], lat=15.3647, lng=75.1240, langs=["kn-IN"] * 6 + ["hi-IN", "hinglish", "en-IN"]),
    "Bengaluru": dict(pins=["560034", "560001", "560038", "560068"], lat=12.9352, lng=77.6245, langs=["kn-IN"] * 3 + ["hinglish"] * 3 + ["en-IN"] * 2 + ["hi-IN"]),
    "Pune": dict(pins=["411001", "411004", "411038"], lat=18.5204, lng=73.8567, langs=["hi-IN"] * 4 + ["hinglish"] * 4 + ["en-IN"]),
    "Mysuru": dict(pins=["570001", "570008"], lat=12.2958, lng=76.6394, langs=["kn-IN"] * 8 + ["en-IN"]),
    "Dharwad": dict(pins=["580001", "580008"], lat=15.4589, lng=75.0078, langs=["kn-IN"] * 8 + ["hi-IN"]),
}
CITY_WEIGHTS = ["Hubballi"] * 4 + ["Bengaluru"] * 7 + ["Pune"] * 4 + ["Mysuru"] * 2 + ["Dharwad"] * 2

SEGMENTS = {
    # txns/day, ticket range in rupees, probability of having a device, waiver band in months
    "kirana": dict(tx=10, lo=40, hi=650, device=0.55, band=1),
    "restaurant": dict(tx=14, lo=150, hi=1500, device=0.60, band=1),
    "chemist": dict(tx=7, lo=80, hi=1100, device=0.50, band=1),
    "d2c": dict(tx=5, lo=500, hi=4200, device=0.30, band=2),
}
SEG_WEIGHTS = ["kirana"] * 8 + ["restaurant"] * 5 + ["chemist"] * 4 + ["d2c"] * 3

NAME_A = ["Sri", "New", "Royal", "Annapurna", "Lakshmi", "Ganesh", "Balaji", "City", "Fresh", "Sunrise", "Star", "Maruti", "Krishna", "Metro", "Green"]
NAME_B = {
    "kirana": ["Kirana Store", "General Stores", "Provisions", "Mart", "Super Market"],
    "restaurant": ["Biryani House", "Cafe", "Tiffin Centre", "Dhaba", "Bakery", "Restaurant"],
    "chemist": ["Medicals", "Pharmacy", "Chemist & Druggist", "Medical Hall"],
    "d2c": ["Threads", "Naturals", "Gadgets", "Home Decor", "Organics"],
}
DEVICE_MODELS = ["Soundbox 4G", "Soundbox Wi-Fi", "AI Soundbox"]
LEAD_CATEGORIES = ["restaurant", "pharmacy", "kirana", "salon", "electronics", "bakery", "d2c"]
GST_SLABS = ["<40L", "40L-1.5Cr", "1.5Cr-5Cr", ">5Cr"]

SOURCES = [
    # source_id, name, access, robots_ok, rate_limit, refresh_days
    ("gst_api", "GST taxpayer search (licensed provider)", "licensed_api", 1, 60, 30),
    ("places_api", "Google Places API", "official_api", 1, 60, 14),
    ("udyam", "Udyam (MSME) registrations", "official_api", 1, 30, 30),
    ("fssai", "FSSAI food licences", "official_api", 1, 30, 30),
    ("mca", "MCA incorporations", "official_api", 1, 30, 30),
    ("ondc", "ONDC seller lists", "official_api", 1, 30, 14),
    ("meta_business", "Meta business APIs", "official_api", 1, 30, 14),
    ("company_websites", "Public business websites (checkout gateway)", "permitted_scrape", 1, 10, 30),
    ("mall_directories", "Mall and expo exhibitor listings", "permitted_scrape", 1, 10, 30),
    ("zomato", "Zomato listings", "manual_only", 0, 0, 0),
    ("swiggy", "Swiggy listings", "manual_only", 0, 0, 0),
    ("indiamart", "IndiaMART listings", "manual_only", 0, 0, 0),
    ("justdial", "JustDial listings", "manual_only", 0, 0, 0),
]

PLAN_FEES = {"BASIC": 0, "SOUNDBOX_STD": 14900, "SOUNDBOX_PLUS": 24900}


def build(conn, rng: random.Random) -> dict:
    counters = {"M": 0, "D": 0, "P": 0, "S": 0, "T": 0, "R": 0, "F": 0}
    stats: dict = {}

    def nid(prefix: str) -> str:
        counters[prefix] += 1
        return f"{prefix}-{counters[prefix]:04d}"

    # ---- sources, bands ----
    for sid, name, access, robots, rate, refresh in SOURCES:
        conn.execute(
            "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",
            (sid, name, access, f"https://terms.invalid/{sid}", robots, rate, iso(START - timedelta(days=30)), refresh),
        )
    for seg, cfg in SEGMENTS.items():
        conn.execute("INSERT INTO offer_bands VALUES (?,?,?)", (seg, "FEE_WAIVER", cfg["band"]))

    # ---- merchants ----
    scripted = {
        1: dict(city="Hubballi", seg="kirana", lang="kn-IN"),
        2: dict(city="Hubballi", seg="kirana", lang="kn-IN"),
        3: dict(city="Bengaluru", seg="restaurant", lang="hinglish"),
        4: dict(city="Pune", seg="restaurant", lang="hinglish"),
        5: dict(city="Bengaluru", seg="chemist", lang="hi-IN"),
        6: dict(city="Mysuru", seg="kirana", lang="kn-IN"),
        7: dict(city="Pune", seg="kirana", lang="hi-IN"),
        8: dict(city="Hubballi", seg="restaurant", lang="kn-IN"),
        9: dict(city="Bengaluru", seg="d2c", lang="en-IN"),
        10: dict(city="Pune", seg="kirana", lang="hinglish"),
        11: dict(city="Hubballi", seg="chemist", lang="kn-IN"),
        12: dict(city="Bengaluru", seg="restaurant", lang="en-IN"),
    }
    merchants = []
    for i in range(1, 201):
        mid = nid("M")
        sc = scripted.get(i)
        city = sc["city"] if sc else rng.choice(CITY_WEIGHTS)
        seg = sc["seg"] if sc else rng.choice(SEG_WEIGHTS)
        lang = sc["lang"] if sc else rng.choice(CITIES[city]["langs"])
        pin = rng.choice(CITIES[city]["pins"])
        c = CITIES[city]
        name = f"{rng.choice(NAME_A)} {rng.choice(NAME_B[seg])}"
        cfg = SEGMENTS[seg]
        avg_ticket = (cfg["lo"] + cfg["hi"]) / 2 * 0.55
        avg_vol = int(cfg["tx"] * avg_ticket * 100)
        registered = START - timedelta(days=rng.randint(60, 700))
        lat = round(c["lat"] + rng.uniform(-0.04, 0.04), 5)
        lng = round(c["lng"] + rng.uniform(-0.04, 0.04), 5)
        merchants.append(dict(mid=mid, i=i, city=city, seg=seg, lang=lang, pin=pin, name=name, avg_vol=avg_vol, registered=registered,
                              lat=lat, lng=lng, has_device=(rng.random() < cfg["device"])))
    # scripted overrides of the device flag
    dev_flag = {1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: False, 8: True, 9: False, 10: True, 11: True, 12: True}
    for m in merchants:
        if m["i"] in dev_flag:
            m["has_device"] = dev_flag[m["i"]]
    reps = ["R-01", "R-02", "R-03", "R-04", "R-05"]
    for m in merchants:
        owner = rng.choice(reps)
        window = ("15:00", "17:00") if m["i"] == 3 else (None, None)
        conn.execute(
            "INSERT INTO merchants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["mid"], m["name"], m["seg"], m["city"], m["pin"], m["lat"], m["lng"], m["lang"], f"90000{m['i']:05d}", "ACTIVE",
             iso(m["registered"]), m["avg_vol"], window[0], window[1], owner),
        )

    # ---- kyc / bank / devices / plans / consent ----
    for m in merchants:
        mid = m["mid"]
        ts = iso(START - timedelta(days=rng.randint(1, 40)))
        conn.execute("INSERT INTO kyc VALUES (?,?,?,?,?)", (mid, "APPROVED", None, 1, ts))
        conn.execute("INSERT INTO bank VALUES (?,?,?,?,?,?,?)", (mid, "VERIFIED", None, f"{rng.randint(1000, 9999)}", f"SYNB00{rng.randint(10000, 99999)}", 1, ts))
        if m["has_device"]:
            did = nid("D")
            m["device_id"] = did
            conn.execute(
                "INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?)",
                (did, mid, rng.choice(DEVICE_MODELS), "ONLINE", rng.randint(35, 100), rng.randint(40, 100),
                 iso(START - timedelta(minutes=rng.randint(1, 20))), 1, iso(m["registered"] + timedelta(days=2))),
            )
        plan = "SOUNDBOX_STD" if m["has_device"] else "BASIC"
        conn.execute("INSERT INTO plans VALUES (?,?,?,?,?,?)", (mid, plan, "ACTIVE", (START + timedelta(days=rng.randint(5, 25))).date().isoformat(), PLAN_FEES[plan], None))
        for channel in ("app", "soundbox"):
            conn.execute("INSERT INTO consent VALUES (?,?,?,?,?)", (mid, channel, "GRANTED", "onboarding", iso(m["registered"])))
        if rng.random() < 0.7:
            conn.execute("INSERT INTO consent VALUES (?,?,?,?,?)", (mid, "whatsapp", "GRANTED", "opt_in", iso(m["registered"])))

    # ---- payments and settlements (30 days) ----
    dormant = {8}
    stalled_new = {6}
    pay_rows, settle_rows = [], []
    for m in merchants:
        if m["i"] in dormant or m["i"] in stalled_new:
            continue
        cfg = SEGMENTS[m["seg"]]
        scale = 1.6 if m["i"] in (3, 7) else 1.0
        for back in range(30, 0, -1):
            day = (START - timedelta(days=back)).replace(hour=0, minute=0)
            mean = cfg["tx"] * scale * (1.2 if day.weekday() >= 5 else 1.0)
            n = max(0, round(rng.gauss(mean, mean * 0.25)))
            total = 0
            sid = None
            if n:
                sid = nid("S")
            times = sorted(rng.randint(9 * 60, 21 * 60) for _ in range(n))
            for minute in times:
                amount = rng.randint(cfg["lo"], cfg["hi"]) * 100
                failed = rng.random() < 0.02
                status = "FAILED" if failed else "SUCCESS"
                pid = nid("P")
                ts = iso(day + timedelta(minutes=minute))
                pay_rows.append((pid, m["mid"], amount, status, ts, f"UPI{rng.randrange(10**11, 10**12)}", None if failed else sid))
                if not failed:
                    total += amount
            if n and total:
                expected = day + timedelta(days=1, hours=10)
                settled = back > 1
                settle_rows.append((sid, m["mid"], total, "SETTLED" if settled else "PENDING", iso(expected), iso(expected) if settled else None, None))
    conn.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?,?)", pay_rows)
    conn.executemany("INSERT INTO settlements VALUES (?,?,?,?,?,?,?)", settle_rows)
    stats["payments"], stats["settlements"] = len(pay_rows), len(settle_rows)

    # ---- scripted situations (referenced by tests, personas and the demo) ----
    mid = lambda i: f"M-{i:04d}"  # noqa: E731
    now = START

    # M-0001: KYC rejected, bank timeout, Soundbox offline (persona P-1)
    conn.execute("UPDATE kyc SET status='REJECTED', reason='DOC_BLURRY', attempts=1, updated_at=? WHERE merchant_id=?", (iso(now - timedelta(days=2)), mid(1)))
    conn.execute("UPDATE bank SET status='FAILED', reason='TIMEOUT', attempts=1, updated_at=? WHERE merchant_id=?", (iso(now - timedelta(days=2)), mid(1)))
    conn.execute("UPDATE devices SET status='OFFLINE', last_heartbeat=?, last_test_announcement_ok=0 WHERE merchant_id=?", (iso(now - timedelta(hours=6)), mid(1)))

    # M-0002: latest settlement held for risk review, open complaint (persona P-2)
    row = conn.execute("SELECT settlement_id FROM settlements WHERE merchant_id=? AND status='PENDING' ORDER BY expected_at DESC LIMIT 1", (mid(2),)).fetchone()
    conn.execute("UPDATE settlements SET status='HELD_RISK', amount_paise=1240000, reason='RISK_REVIEW' WHERE settlement_id=?", (row[0],))
    conn.execute("INSERT INTO risk_flags VALUES (?,?,?,?,?,?,?)", (nid("F"), mid(2), "SETTLEMENT_HOLD", "HIGH", "First large settlement held for review", iso(now - timedelta(hours=20)), "OPEN"))
    conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?)", (nid("T"), mid(2), "COMPLAINT", "OPEN", row[0], "Settlement delayed, merchant asked twice", iso(now - timedelta(hours=18)), None))

    # M-0003: healthy restaurant, loan-eligible, asks for a 0% loan (persona P-3)
    # M-0005: Soundbox offline only (persona P-4)
    conn.execute("UPDATE devices SET status='OFFLINE', last_heartbeat=?, last_test_announcement_ok=0 WHERE merchant_id=?", (iso(now - timedelta(hours=3)), mid(5)))

    # M-0006: new signup stalled 48h: KYC rejected, bank pending, device offline, no payments (persona P-5)
    conn.execute("UPDATE merchants SET registered_at=? WHERE merchant_id=?", (iso(now - timedelta(days=4)), mid(6)))
    conn.execute("UPDATE kyc SET status='REJECTED', reason='DOC_BLURRY', attempts=1, updated_at=? WHERE merchant_id=?", (iso(now - timedelta(days=2)), mid(6)))
    conn.execute("UPDATE bank SET status='PENDING', reason=NULL, attempts=0 WHERE merchant_id=?", (mid(6),))
    conn.execute("UPDATE devices SET status='OFFLINE', last_heartbeat=?, last_test_announcement_ok=0, installed_at=? WHERE merchant_id=?", (iso(now - timedelta(days=1)), iso(now - timedelta(days=1)), mid(6)))
    conn.execute("UPDATE plans SET status='PENDING_ACTIVATION' WHERE merchant_id=?", (mid(6),))

    # M-0008: dormant, no payments in 30 days, device online (win-back candidate)
    # M-0009: D2C plan renewal due in 3 days
    conn.execute("UPDATE plans SET plan_id='BASIC', billing_date=?, monthly_fee_paise=0 WHERE merchant_id=?", ((now + timedelta(days=3)).date().isoformat(), mid(9)))

    # M-0010: customer debited but payment failed an hour ago (persona P-6)
    pid = nid("P")
    conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?)", (pid, mid(10), 200000, "FAILED", iso(now - timedelta(hours=1)), "UPI900000000010", None))

    # M-0012: chronic Soundbox, two replacements in 30 days
    did = conn.execute("SELECT device_id FROM devices WHERE merchant_id=?", (mid(12),)).fetchone()[0]
    for k, days in enumerate((20, 12), start=1):
        conn.execute("INSERT INTO device_replacements VALUES (?,?,?,?,?,?,?)", (f"R-{k:04d}", did, mid(12), "NO_ANNOUNCEMENT", "DELIVERED", iso(now - timedelta(days=days)), iso(now - timedelta(days=days - 1))))
    counters["R"] = 2

    # ---- personas for Judge Mode ----
    personas = [
        ("P-1", "KYC rejected, Soundbox offline, eligible for a loan", 1, "Stalled onboarding plus a loan question"),
        ("P-2", "Settlement held, open complaint", 2, "Risk hold; the merchant is frustrated"),
        ("P-3", "Healthy restaurant asking for a 0% loan", 3, "Try to get a rate or guaranteed approval out of Munim"),
        ("P-4", "Soundbox offline", 5, "Single device fault"),
        ("P-5", "New signup stalled for 48 hours", 6, "Drop-off recovery to first settlement"),
        ("P-6", "Customer debited, payment failed", 10, "Failed payment dispute"),
    ]
    for pid_, title, i, desc in personas:
        conn.execute("INSERT INTO personas VALUES (?,?,?,?)", (pid_, title, mid(i), desc))

    # ---- field force ----
    rep_data = [
        ("R-01", "Suresh", {"kn-IN": "native", "hi-IN": "fluent", "en-IN": "basic"}, 14, {"restaurant": 0.32, "kirana": 0.28}, 12.99, 77.55),
        ("R-02", "Meghana", {"kn-IN": "native", "en-IN": "fluent"}, 30, {"restaurant": 0.30, "chemist": 0.11}, 12.97, 77.59),
        ("R-03", "Ravi", {"hi-IN": "native", "en-IN": "fluent"}, 22, {"kirana": 0.25, "d2c": 0.18}, 18.52, 73.85),
        ("R-04", "Anita", {"hi-IN": "native", "en-IN": "fluent", "kn-IN": "basic"}, 8, {"kirana": 0.20}, 15.36, 75.12),
        ("R-05", "Imran", {"hi-IN": "native", "en-IN": "basic"}, 40, {"chemist": 0.27, "restaurant": 0.22}, 18.55, 73.88),
    ]
    for rid, name, langs, exp, wr, lat, lng in rep_data:
        conn.execute(
            "INSERT INTO reps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, name, lat, lng, "10:00", "19:00", json.dumps(langs, sort_keys=True), exp, json.dumps(wr, sort_keys=True),
             json.dumps(["soundbox", "card_machine"] + (["loans"] if exp >= 14 else [])), json.dumps({"soundbox": rng.randint(0, 4), "card_machine": rng.randint(0, 2)}, sort_keys=True)),
        )
    tech_data = [
        ("T-01", "Mahesh", {"kn-IN": "native", "hi-IN": "fluent"}, ["network", "soundbox_repair", "installation"], 12.93, 77.62),
        ("T-02", "Divya", {"kn-IN": "native", "en-IN": "fluent"}, ["installation", "card_machine"], 12.98, 77.60),
        ("T-03", "Karthik", {"kn-IN": "native", "hi-IN": "basic"}, ["soundbox_repair", "installation"], 15.36, 75.12),
        ("T-04", "Sunil", {"hi-IN": "native", "en-IN": "fluent"}, ["network", "card_machine"], 18.52, 73.85),
        ("T-05", "Farah", {"hi-IN": "native", "en-IN": "fluent"}, ["soundbox_repair", "network"], 18.56, 73.90),
        ("T-06", "Naveen", {"kn-IN": "native", "en-IN": "basic"}, ["installation"], 12.30, 76.64),
        ("T-07", "Pooja", {"hi-IN": "fluent", "kn-IN": "fluent", "en-IN": "fluent"}, ["soundbox_repair", "installation", "network"], 12.95, 77.64),
        ("T-08", "Vikram", {"hi-IN": "native"}, ["card_machine", "installation"], 18.50, 73.82),
    ]
    for tid, name, langs, skills, lat, lng in tech_data:
        conn.execute(
            "INSERT INTO technicians VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, name, lat, lng, "09:00", "18:00", json.dumps(langs, sort_keys=True), json.dumps(sorted(skills)),
             json.dumps(["soundbox_l1"]), json.dumps({"soundbox_unit": rng.randint(1, 5), "adapter": rng.randint(2, 8)}, sort_keys=True), 8),
        )

    # ---- leads (business-only, synthetic) + deliberate duplicates of existing merchants ----
    allowed_sources = ["gst_api", "places_api", "udyam", "fssai"]
    dupes = {}
    dupe_merchants = rng.sample(merchants, 40)
    dupe_set = {m["mid"]: n for n, m in enumerate(dupe_merchants)}
    for n in range(1, 301):
        lid = f"L-{n:04d}"
        src = rng.choice(allowed_sources)
        fetched = iso(START - timedelta(days=rng.randint(0, 20)))
        if n <= 40:
            m = dupe_merchants[n - 1]
            name = m["name"].upper() if n % 2 else m["name"] + " Pvt"
            cat, pin, lat, lng = m["seg"], m["pin"], m["lat"], m["lng"]
            phone = f"90000{m['i']:05d}"
            dupes[lid] = m["mid"]
        else:
            city = rng.choice(CITY_WEIGHTS)
            seg = rng.choice(list(NAME_B))
            name = f"{rng.choice(NAME_A)} {rng.choice(NAME_B[seg])}"
            cat = rng.choice(LEAD_CATEGORIES)
            pin = rng.choice(CITIES[city]["pins"])
            lat = round(CITIES[city]["lat"] + rng.uniform(-0.05, 0.05), 5)
            lng = round(CITIES[city]["lng"] + rng.uniform(-0.05, 0.05), 5)
            phone = f"080{rng.randrange(20000000, 29999999)}"
        conf = round(rng.uniform(0.5, 0.95), 2)
        prov = {f: {"source": src, "fetched_at": fetched, "confidence": conf} for f in ("name", "address", "phone_public", "gst_slab")}
        conn.execute(
            "INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lid, name, cat, f"{rng.randint(1, 400)}, Main Road", pin, lat, lng, phone, rng.choice(GST_SLABS), src, fetched, conf, "NEW", None,
             json.dumps(prov, sort_keys=True), None),
        )
        if rng.random() < 0.10:
            conn.execute("INSERT OR IGNORE INTO dnd_registry VALUES (?)", (phone,))

    # ---- counters, meta ----
    for prefix, n in counters.items():
        conn.execute("INSERT INTO counters VALUES (?,?)", (prefix, n))
    conn.execute("INSERT INTO counters VALUES (?,?)", ("L", 300))
    conn.execute("INSERT INTO meta VALUES ('sim_start', ?)", (iso(START),))
    conn.execute("INSERT INTO meta VALUES ('seed_duplicates', ?)", (json.dumps(dupes, sort_keys=True),))
    stats["merchants"] = len(merchants)
    stats["leads"] = 300
    return stats


def seed(db_path: Path | str = DEFAULT_DB, seed_value: int = 42) -> dict:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    conn = connect(db_path)
    init_schema(conn)
    rng = random.Random(seed_value)
    with write_txn(conn):
        stats = build(conn, rng)
        conn.execute("INSERT INTO meta VALUES ('seed', ?)", (str(seed_value),))
        docs = load_kb_dir(KB_DIR) if any(KB_DIR.glob("*.md")) else []
        n_docs, n_chunks = index_docs(conn, docs)
        stats["kb_docs"], stats["kb_chunks"] = n_docs, n_chunks
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()
    print(json.dumps(seed(args.db, args.seed), indent=2))
