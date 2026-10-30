#!/usr/bin/env python3
"""Offline verifier for a Munim session evidence pack (#136). Standard library only: it imports nothing from this repository.

    python scripts/verify_pack.py pack.json      (or the exported .html: the pack is read from its embedded JSON)

Exit 0 and "VERIFIED" only if: every entry's hash recomputes from its own fields and the previous hash, the entries are contiguous
and linked, the first entry's prev_hash is the stated anchor, the window head matches the stated head, and the scoreboard counts
derivable from the entries (attempts, blocked claims, refused actions, escalations, fine marks) equal the stated scoreboard.
Any other result exits 1 and says where. Hash rule: h_n = SHA256(h_{n-1} + canonical_json(entry minus volatile fields)).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

GENESIS_HASH = hashlib.sha256(b"MUNIM-GENESIS").hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _volatile(k: str) -> bool:
    return k == "ts_real" or k.endswith("latency_ms")


def strip_volatile(o):
    if isinstance(o, dict):
        return {k: strip_volatile(v) for k, v in o.items() if not _volatile(k)}
    if isinstance(o, list):
        return [strip_volatile(v) for v in o]
    return o


def entry_hash(prev: str, e: dict) -> str:
    view = {"seq": e["seq"], "case_id": e["case_id"], "ts_sim": e["ts_sim"], "actor": e["actor"], "role": e["role"], "kind": e["kind"],
            "payload": strip_volatile(e["payload"]), "policy_hash": e["policy_hash"], "model_id": e["model_id"], "prompt_hash": e["prompt_hash"]}
    return hashlib.sha256((prev + canonical_json(view)).encode("utf-8")).hexdigest()


def recompute_scoreboard(pack: dict) -> dict:
    """The counts that follow from the entries alone. Merchant of a case comes from the pack's case map."""
    s = pack.get("session") or {}
    merchant, start, sid = s.get("merchant_id"), s.get("start_seq"), s.get("session_id")
    cm = pack.get("case_merchants") or {}
    out = {"attempts": 0, "blocked_claims": 0, "refused_actions": 0, "escalations": 0, "fine_marks": 0}
    for e in pack["ledger"]:
        if e["seq"] <= start:
            continue
        p = e["payload"]
        if e["kind"] == "HUMAN_ACTION" and e["actor"] == "JUDGE" and p.get("session_id") == sid:
            out["attempts"] += p.get("op") == "JUDGE_ATTEMPT"
            out["fine_marks"] += p.get("op") == "JUDGE_FEEDBACK"
        if cm.get(e["case_id"]) != merchant:
            continue
        if e["kind"] == "VERIFY":
            out["blocked_claims"] += sum(1 for c in p.get("claims") or [] if not c.get("sent"))
        elif e["kind"] == "POLICY":
            out["refused_actions"] += p.get("allow") is False
        elif e["kind"] == "ESCALATION":
            out["escalations"] += 1
    return out


def verify(pack: dict) -> tuple[bool, str]:
    try:
        ledger = pack["ledger"]
        if not ledger:
            return False, "empty ledger window"
        prev = pack["window"]["anchor_prev_hash"]
        if ledger[0]["seq"] == 1 and prev != GENESIS_HASH:
            return False, "entry #1 does not start at the genesis hash"
        if ledger[0]["prev_hash"] != prev:
            return False, f"entry #{ledger[0]['seq']}: prev_hash is not the stated anchor"
        seq = ledger[0]["seq"]
        for e in ledger:
            if e["seq"] != seq:
                return False, f"sequence gap before #{e['seq']} (expected #{seq})"
            if e["prev_hash"] != prev:
                return False, f"entry #{e['seq']}: prev_hash does not match the previous entry"
            if entry_hash(prev, e) != e["hash"]:
                return False, f"entry #{e['seq']}: hash does not match its contents"
            prev, seq = e["hash"], seq + 1
        if pack["chain"]["head_hash"] != prev or pack["window"]["end_seq"] != ledger[-1]["seq"] or pack["window"]["entries"] != len(ledger):
            return False, "window head does not match the last entry"
        if pack.get("scoreboard") is not None and pack.get("session"):
            live = recompute_scoreboard(pack)
            for k, v in live.items():
                if pack["scoreboard"].get(k) != v:
                    return False, f"scoreboard {k} is {pack['scoreboard'].get(k)} but the entries give {v}"
        return True, f"{len(ledger)} entries, #{ledger[0]['seq']} to #{ledger[-1]['seq']}, chain intact"
    except (KeyError, TypeError, IndexError) as exc:
        return False, f"malformed pack ({type(exc).__name__}: {exc})"


def load(text: str) -> dict:
    m = re.search(r"<script type='application/json' id='pack'>(.*?)</script>", text, re.S)
    return json.loads(m.group(1) if m else text)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: verify_pack.py <pack.json|pack.html>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as f:
            pack = load(f.read())
    except (OSError, ValueError) as exc:
        print(f"FAILED: cannot read pack ({exc})")
        return 1
    ok, why = verify(pack)
    print(("VERIFIED: " if ok else "FAILED: ") + why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
