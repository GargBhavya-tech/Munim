"""Session-level evidence pack (#136, session half). One self-contained file for a window of the ledger.

A window is the contiguous run of ledger entries ``start_seq .. head`` (a judge session, or the whole demo run with ``start_seq=1``).
Contents: scoreboard, every entry in the window with prev_hash/hash, the whole-chain verification done at export time, the policy
hash(es) the entries were written under, playbook ids/versions/file hashes, and per-claim evidence links (claim -> VERIFY entry -> source).
Because the window is contiguous, an offline verifier (scripts/verify_pack.py, stdlib only) recomputes the chain from the first entry's
stated prev_hash and checks every link; it also recomputes the scoreboard counts it can derive from the entries themselves.

Ledger payloads are never altered (that would break the hash). They hold no merchant text and no OTP/PIN: the gateway redacts before
anything is stored. The export is scanned anyway (``scan_secrets``) and ``build_window_pack`` refuses to produce a pack that trips it.
Everything is labelled SIMULATED. No wall clock is read: ``exported_at_sim`` comes from the caller's SimClock.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import yaml

from core.audit.ledger import Ledger, verify_chain
from core.engine.clock import SimClock

SESSION_PACK_VERSION = 1

# Key-looking strings, and an OTP/PIN/CVV word followed by 4-8 digits. Same key pattern as scripts/check_secrets.sh.
SECRET_PATTERNS = {
    "api_key": re.compile(r"\bsk[-_][A-Za-z0-9]{16,}|api_key\s*[=:]\s*[\"']?[A-Za-z0-9_-]{12,}", re.I),
    "otp_pin_value": re.compile(r"\b(?:otp|pin|cvv|passcode|mpin)\b[^0-9\n]{0,24}\d{4,8}\b", re.I),
    "bearer": re.compile(r"\bbearer\s+[A-Za-z0-9._-]{20,}", re.I),
}


class PackLeak(Exception):
    """The export contains something that looks like a secret. Fail closed: no pack is produced."""


def scan_secrets(obj) -> list[str]:
    """Names of the secret patterns found anywhere in the JSON text of ``obj`` (empty means clean)."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return [name for name, rx in SECRET_PATTERNS.items() if rx.search(text)]


def playbook_versions(playbook_dir: str | Path | None) -> list[dict]:
    out = []
    if playbook_dir is None or not Path(playbook_dir).is_dir():
        return out
    for p in sorted(Path(playbook_dir).glob("*.y*ml")):
        raw = p.read_bytes()
        try:
            doc = yaml.safe_load(raw.decode("utf-8")) or {}
        except yaml.YAMLError:
            doc = {}
        out.append({"file": p.name, "id": doc.get("id"), "version": doc.get("version"), "sha256": hashlib.sha256(raw).hexdigest()})
    return out


def _entry(e) -> dict:
    return {"seq": e.seq, "case_id": e.case_id, "ts_sim": e.ts_sim, "actor": e.actor, "role": e.role, "kind": e.kind, "payload": e.payload,
            "policy_hash": e.policy_hash, "model_id": e.model_id, "prompt_hash": e.prompt_hash, "prev_hash": e.prev_hash, "hash": e.hash}


def claim_links(entries: list[dict]) -> list[dict]:
    """Every claim in every VERIFY entry, with the entry it was judged in (``#seq-N`` in the HTML) and the source it cites."""
    out = []
    for e in entries:
        if e["kind"] != "VERIFY":
            continue
        for c in e["payload"].get("claims") or []:
            out.append({"verify_seq": e["seq"], "case_id": e["case_id"], "claim_id": c.get("id"), "text": c.get("text"), "source": c.get("source"),
                        "level": c.get("level"), "sent": c.get("sent"), "link": f"#seq-{e['seq']}"})
    return out


def build_window_pack(conn: sqlite3.Connection, clock: SimClock, *, start_seq: int = 1, scoreboard: dict | None = None,
                      session: dict | None = None, playbook_dir: str | Path | None = None, title: str = "Demo run") -> dict:
    entries = [_entry(e) for e in Ledger(conn, clock).entries() if e.seq >= start_seq]
    chain = verify_chain(conn)
    case_ids = sorted({e["case_id"] for e in entries if e["case_id"]})
    merchants = {r[0]: r[1] for r in conn.execute(
        f"SELECT case_id, merchant_id FROM cases WHERE case_id IN ({','.join('?' * len(case_ids))})", case_ids)} if case_ids else {}
    hashes = []
    for e in entries:
        if e["policy_hash"] and e["policy_hash"] not in hashes:
            hashes.append(e["policy_hash"])
    pack = {
        "pack_version": SESSION_PACK_VERSION, "simulated": True, "label": "SIMULATED", "title": title,
        "exported_at_sim": clock.now_iso(), "session": session, "scoreboard": scoreboard,
        "window": {"start_seq": start_seq, "end_seq": entries[-1]["seq"] if entries else None, "entries": len(entries),
                   "anchor_prev_hash": entries[0]["prev_hash"] if entries else None},
        "chain": {"ok": chain.ok, "entries": chain.entries, "first_mismatch": chain.first_mismatch, "reason": chain.reason,
                  "head_hash": entries[-1]["hash"] if entries else None},
        "policy": {"hashes": hashes, "current": hashes[-1] if hashes else None},
        "playbooks": playbook_versions(playbook_dir),
        "case_merchants": merchants,
        "claims": claim_links(entries),
        "ledger": entries,
    }
    hits = scan_secrets(pack)
    if hits:
        raise PackLeak(f"export refused: secret-like text found ({', '.join(hits)})")
    return pack


# ---- offline verification (the same logic is duplicated, stdlib only, in scripts/verify_pack.py) -------------------------------
def verify_window_pack(pack: dict) -> tuple[bool, str]:
    from scripts.verify_pack import verify
    return verify(pack)


def render_html(pack: dict) -> str:
    import html
    ok, why = verify_window_pack(pack)
    esc = html.escape

    def cell(v) -> str:
        return esc(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else "" if v is None else str(v))

    def table(rows, cols, id_col=None) -> str:
        if not rows:
            return "<p>None.</p>"
        head = "".join(f"<th>{esc(c)}</th>" for c in cols)
        body = "".join(f"<tr{(' id=\"seq-%s\"' % r['seq']) if id_col else ''}>" + "".join(f"<td>{cell(r.get(c))}</td>" for c in cols) + "</tr>" for r in rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    claims = [{**c, "link": f"<a href=\"{c['link']}\">{c['link']}</a>"} for c in pack["claims"]]
    claim_rows = "".join("<tr>" + "".join(f"<td>{esc(str(c.get(k) if c.get(k) is not None else ''))}</td>" for k in
                                         ("verify_seq", "case_id", "claim_id", "text", "source", "level", "sent")) + f"<td>{c['link']}</td></tr>" for c in claims)
    sb = pack.get("scoreboard") or {}
    sb_html = "".join(f"<tr><th>{esc(k)}</th><td>{esc(str(v))}</td></tr>" for k, v in sb.items()) or "<tr><td>No scoreboard (whole-run export).</td></tr>"
    pbs = table(pack["playbooks"], ["file", "id", "version", "sha256"])
    data = json.dumps(pack, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    w = pack["window"]
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>Evidence pack: {esc(pack['title'])}</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:24px;max-width:1200px}table{border-collapse:collapse;width:100%;font-size:12px}"
        "td,th{border:1px solid #ccc;padding:4px;vertical-align:top;text-align:left;word-break:break-all}h2{margin-top:28px}"
        ".sim{background:#fde68a;padding:2px 6px}.bad{color:#b91c1c;font-weight:bold}.good{color:#166534;font-weight:bold}</style></head><body>"
        f"<h1>Evidence pack: {esc(pack['title'])} <span class='sim'>SIMULATED</span></h1>"
        f"<p>Ledger window #{w['start_seq']} to #{w['end_seq']} ({w['entries']} entries). Exported at sim time {esc(pack['exported_at_sim'])}. "
        f"Offline check: <span class='{'good' if ok else 'bad'}'>{'VERIFIED' if ok else 'FAILED'}</span> ({esc(why)}). "
        f"Whole-chain verification at export: {'ok' if pack['chain']['ok'] else 'NOT OK'} ({pack['chain']['entries']} entries).</p>"
        "<p>To re-check without this software: <code>python scripts/verify_pack.py &lt;this file&gt;</code>.</p>"
        f"<h2>Scoreboard</h2><table>{sb_html}</table>"
        f"<h2>Policy and playbooks</h2><p>Policy hash(es): <code>{esc(', '.join(pack['policy']['hashes']) or 'none')}</code></p>{pbs}"
        f"<h2>Claims, sources and levels</h2><table><thead><tr><th>verify_seq</th><th>case</th><th>claim</th><th>text</th><th>source</th><th>level</th><th>sent</th><th>link</th></tr></thead><tbody>{claim_rows}</tbody></table>"
        f"<h2>Ledger</h2>{table(pack['ledger'], ['seq', 'case_id', 'ts_sim', 'actor', 'role', 'kind', 'payload', 'policy_hash', 'prev_hash', 'hash'], id_col=True)}"
        f"<script type='application/json' id='pack'>{data}</script></body></html>"
    )
