"""Pre-flight checklist for the morning of the event (Build Map #159): the software half, automated. Prints PASS, FAIL or MANUAL for each item and exits 1 if any FAIL.

    python scripts/preflight.py [--db data/munim.db] [--recordings recordings] [--skip-slow]

Key values are never printed, only whether they are set. Items that need hands (credits, hotspot, prop, prints) are listed as MANUAL: see docs/EVENT_KIT.md.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KEYS = ("SARVAM_API_KEY",)                       # required to run live; the fallback and maps keys are optional
VIDEO = (".mp4", ".mov", ".webm", ".mkv")
MANUAL = ["credits topped up", "phone hotspot working", "Soundbox prop charged", "fake screenshots printed", "persona cards printed",
          "break-it card printed", "QR code printed and tested with the real token", "recordings backed up on a second device"]


def _env_keys(env_path: Path) -> dict[str, bool]:
    found: dict[str, bool] = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                found[k.strip()] = bool(v.strip())
    return found


def check_keys(root: Path) -> tuple[str, str]:
    have = _env_keys(root / ".env")
    missing = [k for k in KEYS if not have.get(k)]
    return ("FAIL", f"not set in .env: {', '.join(missing)}") if missing else ("PASS", "required keys are set (values not shown)")


def check_dashboard(root: Path) -> tuple[str, str]:
    return ("PASS", "dashboard/dist is built") if (root / "dashboard" / "dist" / "index.html").is_file() else ("FAIL", "run: cd dashboard && npm run build")


def check_frozen(root: Path) -> tuple[str, str]:
    from core.eval.metrics import FrozenSetTampered, verify_manifest
    try:
        for name in ("claims", "probes"):
            verify_manifest(root / "eval" / "frozen" / name)
    except FrozenSetTampered as e:
        return "FAIL", str(e)[:300]
    return "PASS", "both frozen sets match their manifests"


def check_heldout(root: Path, db: Path) -> tuple[str, str]:
    from core.eval.gate4 import NoFrozenRun, deck_numbers, freeze_hash
    from core.memory.store import connect
    if not db.is_file():
        return "FAIL", f"no database at {db}"
    try:
        run = deck_numbers(connect(db), freeze_hash(root))
    except NoFrozenRun as e:
        return "FAIL", f"{e} (run `munim heldout --confirm` once the configuration is final)"
    except Exception as e:                       # an uninitialised database has no runs table
        return "FAIL", f"cannot read runs: {e}"
    return "PASS", f"held-out run {run['run_id']} exists for the current configuration"


def check_ledger(db: Path) -> tuple[str, str]:
    from core.audit.ledger import verify_chain
    from core.memory.store import connect
    if not db.is_file():
        return "FAIL", f"no database at {db}"
    try:
        r = verify_chain(connect(db))
    except Exception as e:
        return "FAIL", f"cannot verify: {e}"
    return ("PASS", f"{r.entries} entries verify") if r.ok else ("FAIL", f"first mismatch at entry {r.first_mismatch}: {r.reason}")


def check_kill_switch(root: Path) -> tuple[str, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_api_control.py", "-k", "kill_switch", "-q", "-p", "no:cacheprovider"], cwd=root, capture_output=True, text=True)
    return ("PASS", "kill switch refuses actions end to end") if r.returncode == 0 else ("FAIL", (r.stdout + r.stderr)[-300:])


def check_recordings(rec: Path) -> tuple[str, str]:
    files = [p for p in rec.rglob("*") if p.suffix.lower() in VIDEO] if rec.is_dir() else []
    return ("PASS", f"{len(files)} recording(s) in {rec}") if files else ("FAIL", f"no video files in {rec}")


def check_deck(root: Path) -> tuple[str, str]:
    import yaml
    from core.eval.deck import build
    deck = build(yaml.safe_load((root / "deck" / "slides.yaml").read_text(encoding="utf-8")), json.loads((root / "deck" / "figures.json").read_text(encoding="utf-8")),
                 run=None, current_hash="-", draft=True)
    bad = [p for p in deck["problems"] if "held-out run" not in p]
    return ("FAIL", "; ".join(bad)[:300]) if bad else ("PASS", "every figure on a slide is VERIFIED and no number is typed in")


def run_checks(root: Path = ROOT, db: Path | None = None, recordings: Path | None = None, skip_slow: bool = False) -> list[tuple[str, str, str]]:
    db = db or root / "data" / "munim.db"
    rec = recordings or root / "recordings"
    out = [("keys", *check_keys(root)), ("dashboard build", *check_dashboard(root)), ("frozen sets", *check_frozen(root)),
           ("held-out run for this code", *check_heldout(root, db)), ("ledger verifies", *check_ledger(db)),
           ("deck figures", *check_deck(root)), ("recordings on disk", *check_recordings(rec))]
    out.append(("kill switch", *(("MANUAL", "skipped (--skip-slow); run tests/test_api_control.py -k kill_switch") if skip_slow else check_kill_switch(root))))
    out += [(m, "MANUAL", "tick by hand") for m in MANUAL]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path)
    ap.add_argument("--recordings", type=Path)
    ap.add_argument("--skip-slow", action="store_true")
    args = ap.parse_args(argv)
    results = run_checks(db=args.db, recordings=args.recordings, skip_slow=args.skip_slow)
    for name, status, detail in results:
        print(f"{status:<6} {name}: {detail}")
    fails = sum(1 for _, s, _ in results if s == "FAIL")
    print(f"\n{fails} FAIL, {sum(1 for _, s, _ in results if s == 'PASS')} PASS, {sum(1 for _, s, _ in results if s == 'MANUAL')} MANUAL")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
