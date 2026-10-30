"""Evidence pack (#136): everything an auditor needs about one case, in one file, that checks itself.

Contents: transcript (re-redacted), claims with sources and levels, rules fired, actions with idempotency keys and compensations, human
actions with roles, the case's ledger slice with each entry's prev_hash/hash, the replayed state and the whole-chain verification.
``verify_pack`` recomputes every slice entry's hash from its own fields and its stated prev_hash, so a pack edited after export fails on
its own, without the database. The slice is not contiguous (other cases' entries sit between), so ``chain_ok`` and ``head_hash``
anchor it to the full chain at export time.
Merchant text is redacted at the gateway; the transcript is redacted AGAIN here, because an export must never be the place that leaks.
Ledger payloads are never altered: they hold no merchant text (EVENT entries carry type and id only) and altering them would break the hash.
"""
from __future__ import annotations

import html
import json
import sqlite3

from core.audit.ledger import Ledger, chain_hash, hashed_view, verify_chain
from core.audit.replay import replay_case
from core.engine.clock import SimClock
from core.memory.redact import redact_or_drop

PACK_VERSION = 1


def _redacted(text: str | None) -> str:
    if not text:
        return ""
    r = redact_or_drop(text)
    return r.text if r is not None else "[unreadable: dropped]"


def build_pack(conn: sqlite3.Connection, case_id: str) -> dict:
    case = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if case is None:
        raise KeyError(case_id)
    entries = Ledger(conn, SimClock()).entries(case_id)
    rep = replay_case(conn, case_id)
    chain = verify_chain(conn)
    head = conn.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()

    transcript = [{"ts_sim": e.ts_sim, "direction": "OUT", "language": e.payload.get("language"), "text": _redacted(e.payload.get("text"))}
                  for e in entries if e.kind == "MESSAGE_SENT"]
    for r in conn.execute("SELECT e.ts_sim, e.payload_json FROM events e WHERE e.type IN ('MERCHANT_MESSAGE', 'LEAD_REPLY') AND "
                          "(e.case_id = ? OR e.event_id IN (SELECT event_id FROM case_events WHERE case_id = ?))", (case_id, case_id)):
        p = json.loads(r["payload_json"])
        transcript.append({"ts_sim": r["ts_sim"], "direction": "IN", "language": p.get("language"), "text": _redacted(p.get("text"))})
    transcript.sort(key=lambda m: m["ts_sim"])

    claims = [{**c, "verify_seq": e.seq} for e in entries if e.kind == "VERIFY" for c in e.payload.get("claims") or []]
    rules = [{"seq": e.seq, "action": e.payload.get("action"), "allow": e.payload.get("allow"), "rule_ids": e.payload.get("rule_ids") or [],
              "reason": e.payload.get("reason") or e.payload.get("reasons")} for e in entries if e.kind == "POLICY" and e.payload.get("allow") is False]
    actions = [{"action_id": a["action_id"], "name": a["name"], "cls": a["cls"], "status": a["status"], "idempotency_key": a["idempotency_key"],
                "compensation": a["cstatus"], "compensable_until": a["cu"]}
               for a in conn.execute("SELECT a.*, c.status AS cstatus, c.compensable_until AS cu FROM actions a "
                                     "LEFT JOIN compensations c ON c.action_id = a.action_id WHERE a.case_id = ? ORDER BY a.rowid", (case_id,))]
    humans = [{"seq": e.seq, "actor": e.actor, "role": e.role, "action": e.payload.get("action") or e.payload.get("op")}
              for e in entries if e.actor.startswith("HUMAN:")]
    slice_ = [{"seq": e.seq, "case_id": e.case_id, "ts_sim": e.ts_sim, "actor": e.actor, "role": e.role, "kind": e.kind, "payload": e.payload,
               "policy_hash": e.policy_hash, "model_id": e.model_id, "prompt_hash": e.prompt_hash, "prev_hash": e.prev_hash, "hash": e.hash}
              for e in entries]
    return {"pack_version": PACK_VERSION, "simulated": True, "case": dict(case), "transcript": transcript, "claims": claims, "rules_fired": rules,
            "actions": actions, "human_actions": humans, "ledger_slice": slice_,
            "replay": {"state": rep.state, "state_hash": rep.state_hash, "steps": rep.steps},
            "chain": {"ok": chain.ok, "entries": chain.entries, "first_mismatch": chain.first_mismatch, "reason": chain.reason,
                      "head_seq": head["seq"] if head else 0, "head_hash": head["hash"] if head else None}}


def verify_pack(pack: dict) -> tuple[bool, int | None]:
    """(ok, first bad seq). Every slice entry must hash to its stored hash from its own fields and its stated prev_hash."""
    for e in pack.get("ledger_slice") or []:
        view = hashed_view(seq=e["seq"], case_id=e["case_id"], ts_sim=e["ts_sim"], actor=e["actor"], role=e["role"], kind=e["kind"],
                           payload=e["payload"], policy_hash=e["policy_hash"], model_id=e["model_id"], prompt_hash=e["prompt_hash"])
        if chain_hash(e["prev_hash"], view) != e["hash"]:
            return False, e["seq"]
    return bool(pack.get("chain", {}).get("ok")), None


def _table(rows: list[dict], cols: list[str]) -> str:
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(r.get(c, '')) if not isinstance(r.get(c), (dict, list)) else json.dumps(r.get(c), ensure_ascii=False))}</td>"
                                    for c in cols) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>" if rows else "<p>None.</p>"


def render_html(pack: dict) -> str:
    c = pack["case"]
    ok, bad = verify_pack(pack)
    status = "VERIFIED" if ok else f"FAILED at seq {bad}" if bad else "CHAIN NOT OK"
    parts = [
        f"<!doctype html><html><head><meta charset='utf-8'><title>Evidence pack {html.escape(c['case_id'])}</title>",
        "<style>body{font-family:system-ui,sans-serif;margin:24px;max-width:1100px}table{border-collapse:collapse;width:100%;font-size:13px}"
        "td,th{border:1px solid #ccc;padding:4px;vertical-align:top;text-align:left}h2{margin-top:28px}.sim{background:#fde68a;padding:2px 6px}</style>",
        "</head><body>",
        f"<h1>Evidence pack: {html.escape(c['case_id'])} <span class='sim'>SIMULATED</span></h1>",
        f"<p>{html.escape(c['playbook_id'])} · state {html.escape(c['state'])} · merchant {html.escape(str(c['merchant_id']))} · "
        f"ledger slice: <b>{status}</b> · replay state hash <code>{pack['replay']['state_hash'][:16]}</code> · "
        f"chain head #{pack['chain']['head_seq']} <code>{str(pack['chain']['head_hash'])[:16]}</code></p>",
        "<h2>Transcript</h2>", _table(pack["transcript"], ["ts_sim", "direction", "language", "text"]),
        "<h2>Claims, sources and levels</h2>", _table(pack["claims"], ["verify_seq", "id", "text", "source", "level", "color", "reason", "rule_id"]),
        "<h2>Rules fired (refusals)</h2>", _table(pack["rules_fired"], ["seq", "action", "rule_ids", "reason"]),
        "<h2>Actions, keys and compensations</h2>", _table(pack["actions"], ["action_id", "name", "cls", "status", "idempotency_key", "compensation", "compensable_until"]),
        "<h2>Human actions</h2>", _table(pack["human_actions"], ["seq", "actor", "role", "action"]),
        "<h2>Ledger slice</h2>", _table(pack["ledger_slice"], ["seq", "ts_sim", "actor", "kind", "payload", "prev_hash", "hash"]),
        "<script type='application/json' id='pack'>" + html.escape(json.dumps(pack, ensure_ascii=False)) + "</script>",
        "</body></html>",
    ]
    return "".join(parts)
