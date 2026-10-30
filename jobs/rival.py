"""Rival-network analysis with a legal flag (#127; Bible sections 9 and 36, assurance row "rival-network targeting").

Consumer-app payment data shows which payment networks the shops in a neighbourhood use. Used at NEIGHBOURHOOD level that is area demand
data. Used to single out one merchant on a rival network it is individual targeting from consumer payee data, which needs legal and NPCI
sign-off. So:

  * ``aggregate`` / ``priority_pincodes`` / ``discover_candidates``  neighbourhood only. They never carry a payee reference. A cell with fewer than
    ``MIN_PAYEES`` distinct payees is suppressed (numbers withheld), so a small cell cannot point at one shop. Shares are integer basis points
    computed by code.
  * ``individual_targets`` / ``score_payee``  the ONLY paths that touch one payee. Each one asks the POLICY ENGINE (rule ``RIVAL-TARGET-001``,
    facts ``targeting=individual_rival``) and acts only on ALLOW. The flag lives in policy limits (the legal-approval flag, off by default); this
    module never reads it itself, so the scorer cannot drift from the rule. A refusal is ledgered.
  * ``discover_candidates`` hands the chosen pincodes to ``jobs.discovery`` (business registries through the source gate), never a payee list.

Input is SYNTHETIC. The module stores only neighbourhood aggregates (``rival_aggregates``), never a payee reference.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.actions.types import ActionIntent, Principal
from core.engine.clock import SimClock
from core.memory.store import write_txn

OWN_NETWORK = "paytm"
MIN_PAYEES = 5                      # k-anonymity floor per (pincode, network) cell [ASSUMPTION]
RULE_ID = "RIVAL-TARGET-001"
SCHEMA = """
CREATE TABLE IF NOT EXISTS rival_aggregates (
  pincode TEXT NOT NULL, rival_volume_paise INTEGER, own_volume_paise INTEGER, rival_txns INTEGER, rival_payees INTEGER NOT NULL,
  rival_share_bp INTEGER, suppressed INTEGER NOT NULL, computed_at TEXT NOT NULL, PRIMARY KEY (pincode)
)
"""


class IndividualTargetingBlocked(Exception):
    def __init__(self, rule_ids: Sequence[str], reason: str):
        super().__init__(reason)
        self.rule_ids, self.reason = tuple(rule_ids), reason


@dataclass(frozen=True)
class Observation:
    """One consumer-app observation: payee_ref paid via ``network`` in ``pincode``. SYNTHETIC; payee_ref is never stored or returned by aggregates."""
    pincode: str
    network: str
    payee_ref: str
    txn_count: int
    volume_paise: int


@dataclass(frozen=True)
class NeighbourhoodRow:
    pincode: str
    rival_payees: int
    suppressed: bool
    rival_volume_paise: int | None = None
    own_volume_paise: int | None = None
    rival_txns: int | None = None
    rival_share_bp: int | None = None          # rival volume / (rival + own volume), in basis points; None when suppressed or no volume


@dataclass(frozen=True)
class IndividualTarget:
    payee_ref: str
    pincode: str
    network: str
    volume_paise: int
    score: int


def synthetic_observations(seed: int = 7, pincodes: Sequence[str] = ("560034", "560038", "560068")) -> list[Observation]:
    """Deterministic SYNTHETIC consumer observations: the first pincode is rival-heavy, the last is thin (a suppressed cell)."""
    rng = random.Random(f"rival:{seed}")
    out: list[Observation] = []
    n = 0
    plan = {pincodes[0]: (14, 4), pincodes[1]: (8, 8), pincodes[2]: (3, 6)}        # pincode -> (rival payees, own payees)
    for pin, (rivals, owns) in plan.items():
        for net, count in (("rivalpay", rivals), (OWN_NETWORK, owns)):
            for _ in range(count):
                n += 1
                out.append(Observation(pin, net, f"RP-{n:04d}", rng.randint(3, 40), rng.randint(5, 80) * 10_000))
    return out


class RivalAnalysis:
    def __init__(self, conn: sqlite3.Connection, clock: SimClock, ledger, engine=None):
        self.conn, self.clock, self.ledger = conn, clock, ledger
        if engine is None:
            from packs.paytm_merchant.policy.rules import build_engine       # default limits: flag off
            engine = build_engine(conn, clock)
        self.engine = engine
        conn.execute(SCHEMA)

    # ------------------------------------------------------------------------------------------------ neighbourhood level
    def aggregate(self, observations: Iterable[Observation], *, own_network: str = OWN_NETWORK) -> list[NeighbourhoodRow]:
        by_pin: dict[str, dict[str, Any]] = {}
        for o in observations:
            cell = by_pin.setdefault(o.pincode, {"rival_payees": set(), "rival_vol": 0, "rival_txns": 0, "own_vol": 0})
            if o.network == own_network:
                cell["own_vol"] += o.volume_paise
            else:
                cell["rival_payees"].add(o.payee_ref)
                cell["rival_vol"] += o.volume_paise
                cell["rival_txns"] += o.txn_count
        rows: list[NeighbourhoodRow] = []
        for pin in sorted(by_pin):
            c = by_pin[pin]
            payees = len(c["rival_payees"])
            if payees < MIN_PAYEES:
                rows.append(NeighbourhoodRow(pin, payees, True))
                continue
            total = c["rival_vol"] + c["own_vol"]
            rows.append(NeighbourhoodRow(pin, payees, False, c["rival_vol"], c["own_vol"], c["rival_txns"], c["rival_vol"] * 10_000 // total if total else None))
        self._store(rows)
        return rows

    def _store(self, rows: Sequence[NeighbourhoodRow]) -> None:
        now = self.clock.now_iso()
        with write_txn(self.conn):
            for r in rows:
                self.conn.execute("INSERT OR REPLACE INTO rival_aggregates VALUES (?,?,?,?,?,?,?,?)",
                                  (r.pincode, r.rival_volume_paise, r.own_volume_paise, r.rival_txns, r.rival_payees, r.rival_share_bp, int(r.suppressed), now))

    @staticmethod
    def priority_pincodes(rows: Sequence[NeighbourhoodRow], n: int = 3) -> list[str]:
        """Neighbourhoods with the most rival volume, suppressed cells excluded. Ties break on pincode so the order is deterministic."""
        usable = [r for r in rows if not r.suppressed and r.rival_volume_paise]
        return [r.pincode for r in sorted(usable, key=lambda r: (-r.rival_volume_paise, r.pincode))[:n]]

    def discover_candidates(self, discovery_job, fetchers: Mapping[str, Callable], rows: Sequence[NeighbourhoodRow], n: int = 3):
        """Area-level targeting: business discovery (source gate, business fields only) for the top rival neighbourhoods. No payee list is passed on."""
        pins = self.priority_pincodes(rows, n)
        self.ledger.append(kind="STATE", actor="SYSTEM", payload={"job": "rival_analysis", "level": "neighbourhood", "pincodes": pins})
        return discovery_job.discover(fetchers, pins) if pins else None

    # ------------------------------------------------------------------------------------------------ individual level (flag-gated)
    def _authorise_individual(self, payee_ref: str, why: str) -> None:
        it = ActionIntent("get_merchant", {"merchant_id": payee_ref}, principal=Principal("AGENT", "munim"), allowlist=("get_merchant",),
                          facts={"targeting": "individual_rival"})
        d = self.engine.evaluate(it)
        if d.outcome != "ALLOW":
            rules = sorted({v.rule_id for v in d.violations})
            self.ledger.append(kind="POLICY", actor="SYSTEM", payload={"action": why, "allow": False, "rule_ids": rules, "level": "individual"})
            raise IndividualTargetingBlocked(rules, f"{why}: individual-merchant targeting from consumer payment data needs legal approval")
        self.ledger.append(kind="POLICY", actor="SYSTEM", payload={"action": why, "allow": True, "rule_ids": [], "level": "individual"})

    @staticmethod
    def _score(o: Observation) -> int:
        return o.volume_paise // 10_000          # whole rupees of rival volume, an integer ordering key

    def score_payee(self, o: Observation) -> int:
        self._authorise_individual(o.payee_ref, "score_payee")
        return self._score(o)

    def individual_targets(self, observations: Iterable[Observation], *, own_network: str = OWN_NETWORK, limit: int = 10) -> list[IndividualTarget]:
        obs = [o for o in observations if o.network != own_network]
        for o in obs[:1]:
            self._authorise_individual(o.payee_ref, "individual_targets")       # one decision covers the batch; refused before any row is built
        if not obs:
            return []
        ranked = sorted(obs, key=lambda o: (-o.volume_paise, o.payee_ref))[:limit]
        return [IndividualTarget(o.payee_ref, o.pincode, o.network, o.volume_paise, self._score(o)) for o in ranked]
