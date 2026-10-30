"""Visit logging and re-routing (#111).

A voice note becomes structured fields: the extraction (a model, injected as ``extract``) proposes them, and code VALIDATES them: the outcome must be one
of a fixed set, and any number (order value, units) must appear literally in the transcript. Nothing is computed. The visit then needs three proofs
before it counts:
  * the MERCHANT confirms it (a message back from the merchant's side; the rep cannot confirm their own visit),
  * a GEOTAG near the lead (recorded only inside the shift with an active route, via ``FieldPrivacy``; outside that the point is refused and not
    stored, so the visit is flagged NO_GEOTAG rather than tracked),
  * a PHOTO reference.
Without the merchant's confirmation inside ``CONFIRM_WINDOW_H`` simulated hours the visit is FLAGGED_UNCONFIRMED and never counted (``verified_visits``).
Visits feed win rates and nothing else; they carry no coaching content. Re-routing after a visit calls ``routing.replan`` from where the person is now.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import timedelta
from typing import Callable

from core.actions.types import ActionIntent, Principal
from core.engine.clock import SimClock, parse_iso
from field.privacy import FieldPrivacy, LocationRejected
from field.profiles import NotAllowed
from field.routing import Plan, Stop, Vehicle, replan

OUTCOMES = ("CONVERTED", "NOT_INTERESTED", "SHOP_CLOSED", "RESCHEDULED", "NEEDS_FOLLOW_UP")
DROP_OUTCOMES = frozenset({"CONVERTED", "NOT_INTERESTED", "SHOP_CLOSED"})
NUMERIC_FIELDS = ("order_value_paise", "units")
CONFIRM_WINDOW_H = 24
GEO_RADIUS_M = 200
LOGGED, CONFIRMED, FLAGGED = "LOGGED", "CONFIRMED", "FLAGGED_UNCONFIRMED"
PRINCIPAL = Principal("AGENT", "munim")
SCHEMA_SQL = ("CREATE TABLE IF NOT EXISTS field_visits (visit_id TEXT PRIMARY KEY, rep_id TEXT NOT NULL, lead_id TEXT NOT NULL, logged_at TEXT NOT NULL, "
              "fields_json TEXT NOT NULL, transcript TEXT NOT NULL, geo_ok INTEGER NOT NULL, photo_ref TEXT, status TEXT NOT NULL, flags_json TEXT NOT NULL, "
              "confirmed_at TEXT)",)


class VisitFieldsInvalid(ValueError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    for s in SCHEMA_SQL:
        conn.execute(s)


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine. Used only to check a geotag against the lead's own location (not for routing)."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def validate_fields(transcript: str, fields: dict) -> dict:
    out = dict(fields)
    if out.get("outcome") not in OUTCOMES:
        raise VisitFieldsInvalid(f"outcome must be one of {OUTCOMES}, got {out.get('outcome')!r}")
    digits = {int(x) for x in re.findall(r"\d+", transcript.replace(",", ""))}
    for k in NUMERIC_FIELDS:
        if k in out:
            v = out[k]
            shown = v // 100 if k.endswith("_paise") and isinstance(v, int) and v % 100 == 0 else v
            if not isinstance(v, int) or isinstance(v, bool) or shown not in digits:
                raise VisitFieldsInvalid(f"{k}={v!r} is not stated in the voice note; numbers are never worked out")
    return out


class VisitLog:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, privacy: FieldPrivacy, ledger=None):
        self.conn, self.clock, self.privacy, self.ledger = conn, clock, privacy, ledger
        ensure_schema(conn)

    def _next_id(self) -> str:
        return f"V-{self.conn.execute('SELECT COUNT(*) FROM field_visits').fetchone()[0] + 1:05d}"

    def log_visit(self, rep_id: str, lead_id: str, lead_point: tuple[float, float], transcript: str, extract: Callable[[str], dict], *,
                  geotag: tuple[float, float] | None, photo_ref: str | None) -> dict:
        fields = validate_fields(transcript, extract(transcript))
        flags = []
        geo_ok = 0
        if geotag is None:
            flags.append("NO_GEOTAG")
        else:
            try:
                self.privacy.record_location(rep_id, "rep", *geotag)
                if metres(geotag, lead_point) <= GEO_RADIUS_M:
                    geo_ok = 1
                else:
                    flags.append("GEO_MISMATCH")
            except LocationRejected as e:
                flags.append("NO_GEOTAG:" + e.reason)
        if not photo_ref:
            flags.append("NO_PHOTO")
        vid = self._next_id()
        self.conn.execute("INSERT INTO field_visits VALUES (?,?,?,?,?,?,?,?,?,?,NULL)", (vid, rep_id, lead_id, self.clock.now_iso(), json.dumps(fields, sort_keys=True),
                                                                                       transcript, geo_ok, photo_ref, LOGGED, json.dumps(flags)))
        self._led("visit_logged", vid, rep_id)
        return self.get(vid)

    def request_confirmation(self, visit_id: str, gated_text: str, submit) -> object:
        """Ask the merchant to confirm. ``gated_text`` must already have passed the grounding gate; the send is an executor intent."""
        v = self.get(visit_id)
        intent = ActionIntent("send_message", {"merchant_id": v["lead_id"], "channel": "whatsapp", "body": gated_text, "language": "hi-IN"}, step_seq=0,
                              principal=PRINCIPAL, playbook_id="FIELD_VISIT", step="confirm_visit", proactive=True, channel="whatsapp",
                              allowlist=("send_message",), facts={"visit_id": visit_id})
        return submit(intent)

    def confirm(self, visit_id: str, from_party: str) -> dict:
        v = self.get(visit_id)
        if from_party != f"merchant:{v['lead_id']}":
            raise NotAllowed("only the merchant of the visit can confirm it; a rep cannot confirm their own visit")
        if v["status"] == CONFIRMED:
            return v
        self.conn.execute("UPDATE field_visits SET status = ?, confirmed_at = ? WHERE visit_id = ?", (CONFIRMED, self.clock.now_iso(), visit_id))
        self._led("visit_confirmed", visit_id, v["rep_id"])
        return self.get(visit_id)

    def sweep_unconfirmed(self) -> list[str]:
        """Flag every LOGGED visit older than the confirmation window. Run from a SimClock timer."""
        cutoff = self.clock.now() - timedelta(hours=CONFIRM_WINDOW_H)
        flagged = []
        for r in self.conn.execute("SELECT visit_id, logged_at, rep_id FROM field_visits WHERE status = ?", (LOGGED,)).fetchall():
            if parse_iso(r["logged_at"]) <= cutoff:
                self.conn.execute("UPDATE field_visits SET status = ? WHERE visit_id = ?", (FLAGGED, r["visit_id"]))
                self._led("visit_unconfirmed", r["visit_id"], r["rep_id"])
                flagged.append(r["visit_id"])
        return flagged

    def verified_visits(self, rep_id: str) -> list[dict]:
        """The only visits that may count toward anything: merchant-confirmed, geotag near the lead, photo present."""
        return [v for v in self.all_for(rep_id) if v["status"] == CONFIRMED and v["geo_ok"] and v["photo_ref"]]

    def all_for(self, rep_id: str) -> list[dict]:
        return [self._row(r) for r in self.conn.execute("SELECT * FROM field_visits WHERE rep_id = ? ORDER BY visit_id", (rep_id,))]

    def get(self, visit_id: str) -> dict:
        r = self.conn.execute("SELECT * FROM field_visits WHERE visit_id = ?", (visit_id,)).fetchone()
        if r is None:
            raise KeyError(visit_id)
        return self._row(r)

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["fields"], d["flags"] = json.loads(d.pop("fields_json")), json.loads(d.pop("flags_json"))
        return d

    def _led(self, what: str, visit_id: str, rep_id: str) -> None:
        if self.ledger is not None:
            self.ledger.append(kind="STATE", actor="SYSTEM", payload={"field_visit": what, "visit_id": visit_id, "rep": rep_id})


def reroute_after_visit(matrix, stops: list[Stop], vehicles: list[Vehicle], *, stop_id: str, outcome: str, now: int, position_node: int, vehicle_id: str,
                        new_earliest: int | None = None, mode: str = "sales") -> Plan:
    """Re-plan from where the person now is. A finished or dead-end stop leaves the plan; a RESCHEDULED stop comes back no earlier than ``new_earliest``."""
    if outcome not in OUTCOMES:
        raise ValueError(outcome)
    rest = list(stops)
    done = []
    if outcome in DROP_OUTCOMES:
        done.append(stop_id)
    elif outcome == "RESCHEDULED":
        if new_earliest is None:
            raise ValueError("RESCHEDULED needs new_earliest")
        rest = []
        for s in stops:
            if s.id != stop_id:
                rest.append(s)
                continue
            node = s.node
            if node == position_node:                       # the rep is standing at the shop: the later visit needs its own copy of that place in the matrix
                matrix = [list(r) + [r[node]] for r in matrix]
                matrix.append(list(matrix[node][:-1]) + [0])
                node = len(matrix) - 1
            rest.append(Stop(s.id, node, s.value, s.service, max(new_earliest, s.earliest), max(s.latest, new_earliest)))
    else:                                   # NEEDS_FOLLOW_UP: this visit is over, a later one is another stop
        done.append(stop_id)
    return replan(matrix, rest, vehicles, done=done, now=now, positions={vehicle_id: position_node}, mode=mode)
