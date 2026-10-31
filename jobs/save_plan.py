"""The save: one attempt to keep a merchant who is leaving, fitted to the person, and then we let them go.

Only a persistent dip with no service explanation is a save (``jobs/priority.RETENTION_DIAGNOSES``). For those, Munim chooses how to try:

  * a REP VISIT with a briefing, when the merchant's monthly volume is large enough to be worth a rep's day (the rep may carry an offer inside the approved band);
  * an OFFER (a fee waiver) otherwise, by the executor, inside the approved band for the merchant's segment (``offer_bands``).

An offer inside the band needs no one's sign-off. Anything bigger (a further month on a very large merchant) is PROPOSED and waits for a sales lead or ops admin, and nothing is
applied until one signs. Policy still has the last word when the waiver runs (``WAIVER`` rules), and a refusal is recorded, never worked around.

There is exactly ONE attempt per merchant: ``save_attempts.merchant_id`` is UNIQUE, so a second attempt cannot be created, and once a person records LET_GO the merchant is left
alone: a ``save_closed`` fact says so and ``may_try_to_save`` is false from the first attempt on. The numbers in a briefing are the health job's own (code-computed ratios) or the
band's, each with a source label; no model writes any of it.
"""
from __future__ import annotations

import sqlite3
import zlib
from dataclasses import asdict, dataclass, field

from core.actions.types import ActionIntent, Principal
from core.memory.profile import ProfileStore
from core.memory.store import next_id, write_txn
from jobs.competitor_sightings import RIVALS
from jobs.priority import RETENTION_DIAGNOSES

PRINCIPAL = Principal("AGENT", "munim")
PLAYBOOK = "SAVE_PLAN"
SCHEMA = ("CREATE TABLE IF NOT EXISTS save_attempts (attempt_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL UNIQUE, approach TEXT NOT NULL, offer_months INTEGER, "
          "signoff TEXT NOT NULL, signed_by TEXT, status TEXT NOT NULL, diagnosis TEXT NOT NULL, created_at TEXT NOT NULL, closed_at TEXT, outcome TEXT)")
REP_VISIT, OFFER = "rep_visit", "offer"
REP_MONTHLY_PAISE = 30_000_000          # [ASSUMPTION] about 3 lakh rupees a month: worth a rep's day
BIG_MONTHLY_PAISE = 100_000_000         # [ASSUMPTION] about 10 lakh rupees a month: worth asking for one more month than the band, with a sign-off
SIGNOFF_ROLES = frozenset({"sales_lead", "ops_admin"})
NONE_NEEDED, SALES_LEAD = "NONE_NEEDED", "SALES_LEAD"
PROPOSED, APPROVED, DONE = "PROPOSED", "APPROVED", "DONE"
SAVED, LET_GO = "SAVED", "LET_GO"


class NotEligible(ValueError):
    pass


class AlreadyAttempted(ValueError):
    pass


class NotAllowed(PermissionError):
    pass


@dataclass
class SavePlan:
    merchant_id: str
    eligible: bool
    refusal: str | None = None
    approach: str | None = None
    offer_months: int = 0
    band_months: int = 0
    signoff: str = NONE_NEEDED
    reasons: list[str] = field(default_factory=list)
    briefing: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)


def get_attempt(conn: sqlite3.Connection, merchant_id: str) -> dict | None:
    try:
        r = conn.execute("SELECT * FROM save_attempts WHERE merchant_id = ?", (merchant_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(r) if r else None


def may_try_to_save(conn: sqlite3.Connection, merchant_id: str) -> bool:
    return get_attempt(conn, merchant_id) is None


def _band(conn: sqlite3.Connection, merchant_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT b.* FROM offer_bands b JOIN merchants m ON m.segment = b.segment WHERE m.merchant_id = ?", (merchant_id,)).fetchone()


def briefing_points(conn: sqlite3.Connection, merchant_id: str, detail: dict, band_months: int) -> list[dict]:
    pts = []
    ratios = detail.get("ratios") or []
    if ratios:
        pts.append({"text": f"Payments were {', '.join(str(r) for r in ratios)} of this merchant's own normal on the last {len(ratios)} completed days (1 is normal)", "source": "health:ratios"})
    if detail.get("area_median_ratio") is not None:
        pts.append({"text": f"Neighbours in the same pincode were at {detail['area_median_ratio']} of their normal, so this is not an area-wide dip", "source": "health:area"})
    try:
        s = conn.execute("SELECT sighting_id, network FROM competitor_sightings WHERE merchant_id = ? AND status = 'CONFIRMED' ORDER BY sighting_id DESC LIMIT 1", (merchant_id,)).fetchone()
    except sqlite3.OperationalError:
        s = None
    if s:
        pts.append({"text": f"A rival QR ({RIVALS.get(s['network'], s['network'])}) was seen at the shop front in a rep's photo", "source": f"sighting:{s['sighting_id']}"})
    if band_months:
        pts.append({"text": f"You may offer up to {band_months} month(s) of fee waiver without anyone's sign-off", "source": "policy:offer_band"})
    pts.append({"text": "This is the only save attempt. If they still want to leave, thank them and let them go", "source": "policy:one_attempt"})
    return pts


def plan_save(conn: sqlite3.Connection, merchant_id: str, diagnosis: str, detail: dict, monthly_paise: int) -> SavePlan:
    if diagnosis not in RETENTION_DIAGNOSES:
        return SavePlan(merchant_id, False, f"{diagnosis.replace('_', ' ').lower()} is a service, support or area matter, not a reason to run a save")
    prior = get_attempt(conn, merchant_id)
    if prior is not None:
        word = "they were let go" if prior["outcome"] == LET_GO else "it is " + (prior["outcome"] or prior["status"]).lower()
        return SavePlan(merchant_id, False, f"A save was already attempted ({word}). There is only one attempt per merchant")
    band = _band(conn, merchant_id)
    band_months = int(band["max_months"]) if band else 0
    reasons = []
    approach = REP_VISIT if monthly_paise >= REP_MONTHLY_PAISE else OFFER
    reasons.append("The volume at stake is large enough for a rep's day, so a rep goes with a briefing" if approach == REP_VISIT else
                   "The volume at stake does not justify a visit, so the offer is made without one")
    months = band_months
    signoff = NONE_NEEDED
    if monthly_paise >= BIG_MONTHLY_PAISE and band_months:
        months, signoff = band_months + 1, SALES_LEAD
        reasons.append(f"A merchant this large is worth asking for {months} months, which is one more than the approved {band_months}, so a sales lead must sign it off")
    elif band_months:
        reasons.append(f"The offer stays inside the approved limit of {band_months} month(s), so no sign-off is needed")
    else:
        reasons.append("There is no approved offer band for this merchant's segment, so no offer can be made")
    return SavePlan(merchant_id, True, None, approach, months, band_months, signoff, reasons, briefing_points(conn, merchant_id, detail, band_months))


def _led(ledger, what: str, attempt_id: str, merchant_id: str) -> None:
    if ledger is not None:
        ledger.append(kind="STATE", actor="SYSTEM", payload={"save_plan": what, "attempt_id": attempt_id, "merchant_id": merchant_id})


def start_attempt(conn: sqlite3.Connection, clock, ledger, plan: SavePlan, diagnosis: str) -> dict:
    """Record the one attempt. Raises NotEligible for a plan that is not a save, AlreadyAttempted if the merchant has had theirs."""
    if not plan.eligible:
        raise NotEligible(plan.refusal)
    ensure_schema(conn)
    status = PROPOSED if plan.signoff != NONE_NEEDED else APPROVED
    try:
        with write_txn(conn):
            aid = next_id(conn, "SV")
            conn.execute("INSERT INTO save_attempts(attempt_id, merchant_id, approach, offer_months, signoff, status, diagnosis, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (aid, plan.merchant_id, plan.approach, plan.offer_months, plan.signoff, status, diagnosis, clock.now_iso()))
            _led(ledger, "started", aid, plan.merchant_id)
    except sqlite3.IntegrityError:
        raise AlreadyAttempted(f"{plan.merchant_id} has already had a save attempt")
    return get_attempt(conn, plan.merchant_id)


def sign_off(conn: sqlite3.Connection, clock, ledger, merchant_id: str, role: str, who: str) -> dict:
    if role not in SIGNOFF_ROLES:
        raise NotAllowed(f"role {role!r} may not sign off an offer")
    a = get_attempt(conn, merchant_id)
    if a is None:
        raise KeyError(merchant_id)
    if a["status"] != PROPOSED:
        raise ValueError(f"the attempt is {a['status']}: only a proposed one waits for a sign-off")
    with write_txn(conn):
        conn.execute("UPDATE save_attempts SET status = ?, signed_by = ? WHERE merchant_id = ?", (APPROVED, f"{role}:{who}", merchant_id))
        _led(ledger, "signed_off", a["attempt_id"], merchant_id)
    return get_attempt(conn, merchant_id)


def execute_offer(executor, conn: sqlite3.Connection, merchant_id: str):
    """Apply the approved fee waiver through the executor (policy, ledger, an idempotency key). A refusal comes back as the outcome, not an exception."""
    a = get_attempt(conn, merchant_id)
    if a is None:
        raise KeyError(merchant_id)
    if a["status"] != APPROVED:
        raise ValueError(f"the attempt is {a['status']}: an offer is applied only once it is approved")
    if not a["offer_months"]:
        raise ValueError("this attempt has no offer to apply")
    band = _band(conn, merchant_id)
    args = {"merchant_id": merchant_id, "months": int(a["offer_months"]), "offer_id": band["offer_id"] if band else "FEE_WAIVER_1M"}
    return executor.submit(ActionIntent("apply_fee_waiver", args, step_seq=zlib.crc32(a["attempt_id"].encode()) % 1_000_000, principal=PRINCIPAL, playbook_id=PLAYBOOK,
                                        step="offer", allowlist=("apply_fee_waiver",)))


def close_attempt(conn: sqlite3.Connection, clock, ledger, merchant_id: str, outcome: str) -> dict:
    if outcome not in (SAVED, LET_GO):
        raise ValueError(f"outcome is {SAVED} or {LET_GO}")
    a = get_attempt(conn, merchant_id)
    if a is None:
        raise KeyError(merchant_id)
    if a["status"] == DONE:
        raise ValueError("this attempt is already closed")
    with write_txn(conn):
        conn.execute("UPDATE save_attempts SET status = ?, outcome = ?, closed_at = ? WHERE merchant_id = ?", (DONE, outcome, clock.now_iso(), merchant_id))
        if outcome == LET_GO:
            ProfileStore(conn, clock).set_fact(merchant_id, "save_closed", "let_go", "HUMAN_AGENT")
        _led(ledger, f"closed_{outcome.lower()}", a["attempt_id"], merchant_id)
    return get_attempt(conn, merchant_id)
