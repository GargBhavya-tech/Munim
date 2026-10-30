"""Rep and technician profiles (#105).

Profiles hold what routing, matching and dispatch need: languages with a level, base and shift, experience, win rates by segment (shrunk
toward a prior when the evidence is thin), training and stock for reps; skills, certifications, parts and a daily job cap for technicians.
They NEVER hold religion, caste, community or any other protected trait: ``assert_no_protected_fields`` is run by the tests over the database
columns and over these dataclasses, so a seed or migration that sneaks one in fails.

A person can see and correct their own profile (``ProfileEditor``); a manager role can too. System-derived fields (win rates, experience)
are not editable by the person: a rep cannot raise their own win rate. Every edit is a ledger entry.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass, field

PROTECTED_TRAITS = ("religion", "caste", "community", "ethnicity", "race", "tribe", "sect", "creed", "gotra")
LEVELS = {"basic": 1, "fluent": 2, "native": 3}
MANAGER_ROLES = ("ops_admin", "area_manager")
REP_EDITABLE = frozenset({"languages", "shift_start", "shift_end", "base_lat", "base_lng", "training", "stock"})
TECH_EDITABLE = frozenset({"languages", "shift_start", "shift_end", "base_lat", "base_lng", "certifications", "parts"})
_JSON_FIELDS = {"languages": "languages_json", "training": "training_json", "stock": "stock_json", "certifications": "certifications_json", "parts": "parts_json"}


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@dataclass(frozen=True)
class RepProfile:
    rep_id: str
    name: str
    base: tuple[float, float]
    shift: tuple[int, int]                              # minutes of the day
    languages: dict[str, str]
    experience_months: int
    win_rates: dict[str, float]
    training: tuple[str, ...]
    stock: dict[str, int]

    def speaks(self, language: str, at_least: str = "fluent") -> bool:
        return LEVELS.get(self.languages.get(language, ""), 0) >= LEVELS[at_least]

    def trained_for(self, product: str) -> bool:
        return product in self.training

    def in_stock(self, product: str) -> bool:
        return self.stock.get(product, 0) > 0


@dataclass(frozen=True)
class TechProfile:
    tech_id: str
    name: str
    base: tuple[float, float]
    shift: tuple[int, int]
    languages: dict[str, str]
    skills: frozenset[str]
    certifications: tuple[str, ...]
    parts: dict[str, int]
    max_jobs_per_day: int

    def speaks(self, language: str, at_least: str = "fluent") -> bool:
        return LEVELS.get(self.languages.get(language, ""), 0) >= LEVELS[at_least]

    def has_parts(self, needed: dict[str, int]) -> bool:
        return all(self.parts.get(k, 0) >= n for k, n in needed.items())


def _rep(r: sqlite3.Row) -> RepProfile:
    return RepProfile(r["rep_id"], r["name"], (r["base_lat"], r["base_lng"]), (minutes(r["shift_start"]), minutes(r["shift_end"])),
                      json.loads(r["languages_json"]), r["experience_months"] or 0, json.loads(r["win_rates_json"] or "{}"),
                      tuple(json.loads(r["training_json"] or "[]")), json.loads(r["stock_json"] or "{}"))


def _tech(r: sqlite3.Row) -> TechProfile:
    return TechProfile(r["tech_id"], r["name"], (r["base_lat"], r["base_lng"]), (minutes(r["shift_start"]), minutes(r["shift_end"])),
                       json.loads(r["languages_json"]), frozenset(json.loads(r["skills_json"] or "[]")), tuple(json.loads(r["certifications_json"] or "[]")),
                       json.loads(r["parts_json"] or "{}"), r["max_jobs_per_day"] or 0)


def load_rep(conn: sqlite3.Connection, rep_id: str) -> RepProfile:
    r = conn.execute("SELECT * FROM reps WHERE rep_id = ?", (rep_id,)).fetchone()
    if r is None:
        raise KeyError(rep_id)
    return _rep(r)


def load_reps(conn: sqlite3.Connection) -> list[RepProfile]:
    return [_rep(r) for r in conn.execute("SELECT * FROM reps ORDER BY rep_id")]


def load_tech(conn: sqlite3.Connection, tech_id: str) -> TechProfile:
    r = conn.execute("SELECT * FROM technicians WHERE tech_id = ?", (tech_id,)).fetchone()
    if r is None:
        raise KeyError(tech_id)
    return _tech(r)


def load_techs(conn: sqlite3.Connection) -> list[TechProfile]:
    return [_tech(r) for r in conn.execute("SELECT * FROM technicians ORDER BY tech_id")]


def shrunk_win_rate(raw_rate: float, n_visits: int, prior_rate: float, prior_strength: int = 10) -> float:
    """Bayesian shrinkage: a rate seen in few visits is pulled toward the prior; with many visits it is the raw rate.
    (rate * n + prior * k) / (n + k). With n = 0 it is the prior; with n >> k it is the raw rate."""
    if n_visits < 0 or prior_strength < 0:
        raise ValueError("counts cannot be negative")
    return (raw_rate * n_visits + prior_rate * prior_strength) / (n_visits + prior_strength) if (n_visits + prior_strength) else prior_rate


def team_prior(reps: list[RepProfile], segment: str, default: float = 0.2) -> float:
    rates = [r.win_rates[segment] for r in reps if segment in r.win_rates]
    return sum(rates) / len(rates) if rates else default


def assert_no_protected_fields(conn: sqlite3.Connection) -> None:
    """Raises if any profile table column or profile dataclass field is (or contains) a protected trait."""
    names = [c[1].lower() for t in ("reps", "technicians") for c in conn.execute(f"PRAGMA table_info({t})")]
    names += [f.name.lower() for cls in (RepProfile, TechProfile) for f in dataclasses.fields(cls)]
    bad = sorted({n for n in names for p in PROTECTED_TRAITS if p in n})
    if bad:
        raise AssertionError(f"protected traits must never be stored on a field profile: {bad}")


class NotAllowed(PermissionError):
    pass


@dataclass
class ProfileEditor:
    conn: sqlite3.Connection
    ledger: object = None
    log: list = field(default_factory=list)

    def _kind(self, person_id: str) -> str:
        if self.conn.execute("SELECT 1 FROM reps WHERE rep_id = ?", (person_id,)).fetchone():
            return "rep"
        if self.conn.execute("SELECT 1 FROM technicians WHERE tech_id = ?", (person_id,)).fetchone():
            return "technician"
        raise KeyError(person_id)

    @staticmethod
    def _may(person_id: str, actor_id: str, actor_role: str | None) -> bool:
        return actor_id == person_id or actor_role in MANAGER_ROLES

    def view(self, person_id: str, actor_id: str, actor_role: str | None = None):
        if not self._may(person_id, actor_id, actor_role):
            raise NotAllowed(f"{actor_id} may not view {person_id}'s profile")
        return load_rep(self.conn, person_id) if self._kind(person_id) == "rep" else load_tech(self.conn, person_id)

    def update(self, person_id: str, actor_id: str, changes: dict, actor_role: str | None = None) -> dict:
        kind = self._kind(person_id)
        if not self._may(person_id, actor_id, actor_role):
            raise NotAllowed(f"{actor_id} may not edit {person_id}'s profile")
        editable = REP_EDITABLE if kind == "rep" else TECH_EDITABLE
        bad = sorted(set(changes) - editable)
        if bad:
            raise NotAllowed(f"not editable: {bad} (system-derived or unknown)")
        if any(p in k.lower() for k in changes for p in PROTECTED_TRAITS):
            raise NotAllowed("protected traits are never stored")
        table, key = ("reps", "rep_id") if kind == "rep" else ("technicians", "tech_id")
        for name, value in changes.items():
            col = _JSON_FIELDS.get(name, name)
            self.conn.execute(f"UPDATE {table} SET {col} = ? WHERE {key} = ?",
                              (json.dumps(value, sort_keys=True) if name in _JSON_FIELDS else value, person_id))
        if self.ledger is not None:
            self.ledger.append(kind="HUMAN_ACTION", actor=f"HUMAN:{actor_id}", role=actor_role or kind, payload={"action": "profile_edit", "person": person_id, "fields": sorted(changes)})
        return {"person": person_id, "changed": sorted(changes)}
