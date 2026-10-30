"""#124: recurring gaps become DRAFT playbooks that validate, live outside the pack, and can never be loaded by the engine."""
import json
from pathlib import Path

import pytest
import yaml

from core.engine.knowledge_layer import log_gap
from core.engine.playbook import PlaybookError, load_playbook, load_playbooks
from jobs import playbook_drafts as pd
from packs.paytm_merchant.actions import build_registry

PACK_PB = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "playbooks"


def add_handoff(conn, n, reasons):
    conn.execute("INSERT INTO handoffs(handoff_id, case_id, packet_json, status, created_at) VALUES (?, 'CS-X', ?, 'OPEN', '2026-10-30T10:00:00+05:30')",
                 (f"HO-T{n}", json.dumps({"reasons": reasons})))


@pytest.fixture
def reg():
    return build_registry()


def test_clusters_need_min_count_and_skip_non_gaps(conn):
    for i in range(3):
        add_handoff(conn, i, ["NO_PLAYBOOK_FOR_GST_INVOICE: asked"])
    for i in range(3, 5):
        add_handoff(conn, i, ["RARE_REASON"])
    for i in range(5, 9):
        add_handoff(conn, i, ["WANTS_HUMAN"])
    cl = pd.find_clusters(conn, 3)
    assert [(c.kind, c.key, c.count) for c in cl] == [("ESCALATION", "NO_PLAYBOOK_FOR_GST_INVOICE", 3)]


def test_unsupported_gaps_cluster_by_normalised_question(conn, clock):
    for i, q in enumerate(["Mujhe GST invoice chahiye 123", "mujhe gst invoice chahiye 456", "Mujhe GST invoice chahiye!"]):
        log_gap(conn, clock, kind="UNSUPPORTED", text=q, case_id=f"CS-{i}")
    log_gap(conn, clock, kind="NO_ANSWER", text="Mujhe GST invoice chahiye", case_id="CS-9")
    cl = pd.find_clusters(conn, 3)
    assert len(cl) == 1 and cl[0].kind == "UNSUPPORTED" and cl[0].key == "mujhe gst invoice chahiye" and cl[0].count == 3


def test_draft_is_valid_labelled_and_saved_outside_the_pack(conn, reg, tmp_path):
    for i in range(3):
        add_handoff(conn, i, ["NO_PLAYBOOK_FOR_GST_INVOICE"])
    out = pd.write_drafts(conn, reg, drafts_dir=tmp_path / "d")
    assert len(out) == 1 and out[0].name.endswith(".yaml.draft")
    text = out[0].read_text(encoding="utf-8")
    assert text.startswith("# DRAFT") and "NOT LIVE" in text
    assert pd.PACK_DIR.resolve() not in out[0].resolve().parents
    data = yaml.safe_load(text)
    assert data["allowed_actions"] == ["send_message"] and data["id"].startswith("DRAFT_")
    # the real loader accepts it once a human renames it to .yaml
    copy = tmp_path / "live.yaml"
    copy.write_text(text, encoding="utf-8")
    assert load_playbook(copy, reg, {"safety_lines"}).id == data["id"]


def test_drafts_are_never_loaded_by_the_pack_loader(conn, reg, tmp_path):
    for i in range(3):
        add_handoff(conn, i, ["NO_PLAYBOOK_FOR_GST_INVOICE"])
    out = pd.write_drafts(conn, reg, drafts_dir=tmp_path / "d")
    ids = yaml.safe_load(out[0].read_text(encoding="utf-8"))["id"]
    assert ids not in load_playbooks(PACK_PB, reg)
    assert load_playbooks(tmp_path / "d", reg) == {}                  # even pointed at the drafts directory: *.yaml finds nothing
    assert not list(PACK_PB.glob("DRAFT*")) and not list((tmp_path / "d").glob("*.yaml"))


def test_default_drafts_dir_is_outside_packs():
    assert pd.PACK_DIR.resolve() not in pd.DRAFTS_DIR.resolve().parents and pd.DRAFTS_DIR.name == "playbooks"


def test_saving_under_packs_or_as_loadable_yaml_is_refused():
    with pytest.raises(ValueError):
        pd.refuse_inside_pack(pd.PACK_DIR / "paytm_merchant" / "playbooks" / "X.yaml.draft")
    with pytest.raises(ValueError):
        pd.refuse_inside_pack(Path("/tmp/anywhere/X.yaml"))


def test_invalid_draft_is_not_written(conn, reg, tmp_path):
    for i in range(3):
        add_handoff(conn, i, ["NO_PLAYBOOK_FOR_GST_INVOICE"])
    with pytest.raises(PlaybookError):
        pd.write_drafts(conn, reg, drafts_dir=tmp_path / "d", kb_scopes={"some_other_scope"})
    assert not (tmp_path / "d").exists() or not list((tmp_path / "d").iterdir())


def test_draft_can_never_carry_a_forbidden_or_write_action(conn):
    for i in range(3):
        add_handoff(conn, i, ["NO_PLAYBOOK_FOR_X"])
    c = pd.find_clusters(conn, 3)[0]
    assert yaml.safe_load(pd.build_yaml(c))["allowed_actions"] == ["send_message"]
