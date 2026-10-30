"""#159 pre-flight: the software half of the morning checklist, and that it never shows a key."""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import preflight as P  # noqa: E402

from core.eval.gate4 import HeldOutRunner, freeze_hash  # noqa: E402


def test_keys_pass_only_when_set_and_the_value_is_never_shown(tmp_path):
    assert P.check_keys(tmp_path)[0] == "FAIL"                                              # no .env at all
    name = "SARVAM_API" + "_KEY"                                                          # assembled here so the repo's secret grep does not trip on a fake value
    (tmp_path / ".env").write_text(f"{name}=\n", encoding="utf-8")
    assert P.check_keys(tmp_path)[0] == "FAIL"                                              # present but empty
    fake = "not-a-real-" + "value-1234"
    (tmp_path / ".env").write_text(f"# a comment\n{name}={fake}\n", encoding="utf-8")
    status, detail = P.check_keys(tmp_path)
    assert status == "PASS" and fake not in detail


def test_dashboard_and_recordings(tmp_path):
    assert P.check_dashboard(tmp_path)[0] == "FAIL"
    (tmp_path / "dashboard" / "dist").mkdir(parents=True)
    (tmp_path / "dashboard" / "dist" / "index.html").write_text("x", encoding="utf-8")
    assert P.check_dashboard(tmp_path)[0] == "PASS"
    rec = tmp_path / "recordings"
    assert P.check_recordings(rec)[0] == "FAIL"
    rec.mkdir()
    (rec / "notes.txt").write_text("not a video", encoding="utf-8")
    assert P.check_recordings(rec)[0] == "FAIL"
    (rec / "take1.MP4").write_bytes(b"\0")
    assert P.check_recordings(rec)[0] == "PASS"


def test_frozen_sets_and_deck_figures_pass_on_the_real_repository():
    assert P.check_frozen(ROOT)[0] == "PASS"
    assert P.check_deck(ROOT)[0] == "PASS"                                                  # only the held-out numbers are pending, and that is not a failure here


def test_a_tampered_frozen_set_fails(tmp_path):
    shutil.copytree(ROOT / "eval" / "frozen", tmp_path / "eval" / "frozen")
    (tmp_path / "eval" / "frozen" / "probes" / "policy_probes.json").write_text("{}", encoding="utf-8")
    assert P.check_frozen(tmp_path)[0] == "FAIL"


def test_heldout_run_and_ledger(db_path, clock, conn):
    assert P.check_heldout(ROOT, db_path)[0] == "FAIL" and "no held-out run" in P.check_heldout(ROOT, db_path)[1]
    assert P.check_heldout(ROOT, db_path.parent / "missing.db")[0] == "FAIL"
    HeldOutRunner(conn, clock).run("heldout", freeze_hash(ROOT), lambda: {"metrics": {}})
    conn.commit()
    status, detail = P.check_heldout(ROOT, db_path)
    assert status == "PASS" and "MR-" in detail
    assert P.check_ledger(db_path)[0] == "PASS"
    assert P.check_ledger(db_path.parent / "missing.db")[0] == "FAIL"


def test_the_full_run_lists_the_manual_items_and_fails_when_anything_fails(tmp_path, capsys):
    results = P.run_checks(db=tmp_path / "none.db", recordings=tmp_path / "rec", skip_slow=True)
    by = {name: status for name, status, _ in results}
    assert by["kill switch"] == "MANUAL" and by["frozen sets"] == "PASS" and by["recordings on disk"] == "FAIL"
    assert sum(1 for _, s, _ in results if s == "MANUAL") == len(P.MANUAL) + 1
    assert P.main(["--db", str(tmp_path / "none.db"), "--recordings", str(tmp_path / "rec"), "--skip-slow"]) == 1
    assert "FAIL" in capsys.readouterr().out
