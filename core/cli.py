"""``munim`` command line: replay a case, verify the ledger, evaluate the dev split, run the held-out split once (Gate 4). Usage: python -m core.cli <command>."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from core.audit.ledger import verify_chain
from core.audit.replay import ReplayError, replay_case
from core.memory.store import connect

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "munim.db"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="munim")
    ap.add_argument("--db", default=os.environ.get("MUNIM_DB", str(DEFAULT_DB)))
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("replay", help="rebuild a case from the ledger, offline")
    rp.add_argument("case_id")
    sub.add_parser("verify-ledger", help="recompute the hash chain and report the first mismatch")
    sub.add_parser("eval", help="metrics with intervals from recorded scenario results (munim eval --split dev)")
    sub.add_parser("heldout", help="the one frozen held-out run for this configuration, Gate 4 (munim heldout --confirm)")
    argv = list(sys.argv[1:] if argv is None else argv)
    rest: list[str] = []
    passthrough = next((c for c in ("eval", "heldout") if c in argv), None)
    if passthrough:                                          # argparse's REMAINDER drops a leading option, so split the arguments by hand
        i = argv.index(passthrough)
        argv, rest = argv[:i + 1], argv[i + 1:]
    args = ap.parse_args(argv)
    if args.cmd == "eval":                                   # needs no database
        from core.eval.report_cli import main as eval_main
        return eval_main(rest)
    if args.cmd == "heldout":                                # takes its own --db
        from core.eval.heldout import main as heldout_main
        return heldout_main(["--db", args.db, *rest])

    conn = connect(args.db)
    if args.cmd == "verify-ledger":
        r = verify_chain(conn)
        print(json.dumps({"ok": r.ok, "entries": r.entries, "first_mismatch": r.first_mismatch, "reason": r.reason}))
        return 0 if r.ok else 1
    try:
        res = replay_case(conn, args.case_id)
    except ReplayError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for s in res.steps:
        print(f"{s['seq']:>5}  {s['ts_sim']}  {s['kind']:<14} {s['summary']}")
    print(json.dumps({"case_id": res.case_id, "chain_ok": res.chain.ok, "state_hash": res.state_hash, "final_status": res.state["status"]}))
    return 0 if res.chain.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
