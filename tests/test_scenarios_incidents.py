"""#88 I1 area incidents reach the supported bar: every dev and held-out I1 scenario runs through the real World, the real
IncidentManager and the scenario checker, and is judged by FINAL STATE (incident rows, holds, the persona's fault case, replacements,
the persona's outbox), never by reading a transcript.

A scenario file describes ONE persona merchant (its steps, and what must be true of that merchant's case at the end). The AREA around
the persona (how many other devices or settlements fail, where, over how many minutes) is a setup recorded beside the scenarios in
``incident_setups.json`` (the scenario schema has no field for it). The first step is always the persona's own trigger; the
neighbours fail BEFORE it, spaced evenly over ``span_minutes``.

The case is driven by a scripted decider (the model's stand-in), so these prove the incident job, policy hold and executor; they do not
measure model quality. NATIVE-SPEAKER REVIEW EXPECTED for the hi-IN / kn-IN / hinglish notice texts (they live in jobs/incidents.py).
"""
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from core.engine.fields import SYSTEM_FIELDS, build_namespace
from core.engine.intents import top_intent
from core.engine.scripted import scripted_decider
from core.eval.scenario import coverage, load_scenario, meets_supported_bar
from jobs.incidents import IncidentManager, on_hold
from tests.scenario_runner import Result, check
from tests.test_scenarios_s1_c2 import SCRIPT
from tests.world import World

SC_DIR = Path(__file__).resolve().parent.parent / "packs" / "paytm_merchant" / "scenarios"
PID = "I1_AREA_INCIDENT"
FAULT_PLAYBOOK = {"heartbeat": "C2_SOUNDBOX_FAULT", "settlement": "C1_SETTLEMENT_DELAY"}
TRIGGER = {"heartbeat": "DEVICE_HEARTBEAT_LOST", "settlement": "SETTLEMENT_FAILED"}
SETUPS = {s: json.loads((SC_DIR / s / "incident_setups.json").read_text(encoding="utf-8")) for s in ("dev", "heldout")}
SCENARIOS = [load_scenario(p) for s in ("dev", "heldout") for p in sorted((SC_DIR / s).glob(f"{PID}__*.yaml"))]
BY_ID = {s.id: s for s in SCENARIOS}
LANG_SCRIPT = {"hi-IN": r"[ऀ-ॿ]", "kn-IN": r"[ಀ-೿]"}


def setup_of(sc):
    return SETUPS[sc.split][sc.id]


# ----------------------------------------------------------------------------------------------------------------- the area
def build(db_path, clock, sc, hold=True):
    """The persona plus its neighbours. Returns (world, manager, neighbour merchant ids)."""
    st, persona = setup_of(sc), sc.persona.merchant_id
    w = World(db_path, clock, None, mid=persona)
    from tests.test_service_playbooks_a import SCRIPTS
    table = dict(SCRIPT)                                             # C2 table plus the C1 table (settlement incidents)
    table.update(SCRIPTS["C1_SETTLEMENT_DELAY"](w))
    w.engine.decider = scripted_decider(table)
    has_settlement = " AND EXISTS (SELECT 1 FROM settlements s WHERE s.merchant_id = m.merchant_id AND s.status = 'PENDING' AND s.amount_paise < 1000000)" if st["kind"] == "settlement" else ""
    pool = [r[0] for r in w.conn.execute(
        "SELECT m.merchant_id FROM merchants m JOIN devices d ON d.merchant_id = m.merchant_id WHERE d.status = 'ONLINE' AND m.merchant_id NOT IN (?, 'M-0003') "
        "AND NOT EXISTS (SELECT 1 FROM device_replacements r WHERE r.merchant_id = m.merchant_id)" + has_settlement + " ORDER BY m.merchant_id", (persona,))]
    n_peers = st["devices"] - 1
    assert len(pool) >= n_peers, (sc.id, len(pool))
    peers = pool[:n_peers]
    members = [persona] + peers                                    # group 0 starts with the persona
    for i, mid in enumerate(members):
        w.conn.execute("INSERT OR REPLACE INTO consent(subject_id, channel, status, source, ts) VALUES (?, 'whatsapp', 'GRANTED', 'test', ?)", (mid, clock.now_iso()))
        area = _area_of(st, i)
        if st["kind"] == "heartbeat":
            w.conn.execute("UPDATE merchants SET pincode = ?, city = 'Bengaluru' WHERE merchant_id = ?", (area, mid))
        else:
            w.conn.execute("UPDATE bank SET ifsc = ? WHERE merchant_id = ?", (area + "0001234", mid))
    mgr = IncidentManager(w.conn, w.clock, w.ledger, w.executor, w.engine.case_manager, w.engine.repo)
    if hold:
        w.engine.hold_fn = mgr.hold_note
    w.conn.commit()
    return w, mgr, peers


def _area_of(st, i):
    """Which pincode (heartbeat) or four-letter bank code (settlement) member i belongs to."""
    if st.get("scatter"):
        return f"5600{i:02d}" if st["kind"] == "heartbeat" else f"B{i:03d}"
    left = i
    for area, n in st["groups"]:
        if left < n:
            return area
        left -= n
    raise AssertionError("groups smaller than devices")


def burst(w, st, peers):
    """The neighbours fail one after another over span_minutes; the persona's own trigger is the last to arrive."""
    step = timedelta(minutes=st["span_minutes"]) / (len(peers) + 1)
    for i, mid in enumerate(peers):
        payload = {}
        if st["kind"] == "heartbeat":
            w.control(f"/mock/control/device/{mid}", status="OFFLINE")
            payload = {"device_id": w.conn.execute("SELECT device_id FROM devices WHERE merchant_id = ?", (mid,)).fetchone()[0]}
        if st["kind"] == "settlement":                               # a neighbour's settlement really is failing at the bank
            w.conn.execute("UPDATE settlements SET status = 'FAILED_BANK' WHERE merchant_id = ? AND status = 'PENDING'", (mid,))
            w.conn.commit()
        w.inject(f"{PID}-peer-{i}", TRIGGER[st["kind"]], mid=mid, payload=payload)
        w.clock.advance(step)


def run_incident_scenario(w, mgr, peers, sc):
    st, mid = setup_of(sc), sc.persona.merchant_id
    burst(w, st, peers)
    armed = False                                                    # the jobs start at the persona's own trigger, as the periodic job does
    for n, step in enumerate(sc.steps):
        if step.after_hours:
            w.clock.advance(timedelta(hours=step.after_hours))
        eid = f"{sc.id}-{n}"
        if step.event:
            w.inject(eid, step.event, payload={"device_id": w.conn.execute("SELECT device_id FROM devices WHERE merchant_id = ?", (mid,)).fetchone()[0]}
                     if step.event == "DEVICE_HEARTBEAT_LOST" else {})
        elif step.merchant_says is not None:
            w.inject(eid, "MERCHANT_MESSAGE", payload={"text": step.merchant_says, "intent": top_intent(step.merchant_says, sc.persona.language)})
        elif step.control:
            w.control(step.control["path"], **step.control.get("body", {}))
        elif step.advance_settlement:
            sid = w.conn.execute("SELECT settlement_id FROM settlements WHERE merchant_id = ? AND status = 'PENDING' ORDER BY expected_at DESC LIMIT 1", (mid,)).fetchone()[0]
            w.advance_settlement(sid, f"{eid}-adv", to=step.advance_settlement)
        else:
            raise AssertionError(f"{sc.id}: step type not used by incident scenarios")
        armed = armed or step.event == TRIGGER[st["kind"]] or bool(step.advance_settlement)
        if not armed:
            continue
        for _ in range(st.get("job_runs", 1)):                      # production order: the incident job first, then the dispatcher
            mgr.run()
        w.dispatcher.run()
    if st.get("resolve_after_hours") is not None:
        w.clock.advance(timedelta(hours=st["resolve_after_hours"]))
        for (iid,) in w.conn.execute("SELECT incident_id FROM incidents WHERE status = 'OPEN'").fetchall():
            mgr.resolve(iid)
        w.dispatcher.run()


def result_of(w, sc) -> Result:
    st, mid = setup_of(sc), sc.persona.merchant_id
    row = w.conn.execute("SELECT case_id FROM cases WHERE merchant_id = ? AND playbook_id = ? ORDER BY opened_at DESC, case_id DESC",
                         (mid, FAULT_PLAYBOOK[st["kind"]])).fetchone()
    case = w.engine.repo.load(row[0])
    cases = [r[0] for r in w.conn.execute("SELECT case_id FROM cases WHERE merchant_id = ?", (mid,))]
    executed = {}
    for r in w.conn.execute("SELECT payload_json, case_id FROM ledger WHERE kind = 'ACTION_RESULT'"):
        p = json.loads(r["payload_json"])
        if r["case_id"] in cases and p.get("status") == "OK" and p["action"] != "send_message":
            executed[p["action"]] = executed.get(p["action"], 0) + 1
    sent = w.conn.execute("SELECT COUNT(*) FROM comms_outbox WHERE merchant_id = ?", (mid,)).fetchone()[0]
    if sent:
        executed["send_message"] = sent                              # the incident notice and any case message are all in the outbox
    ns = build_namespace(w.reader, mid, sorted(set(SYSTEM_FIELDS.values())), {})
    from core.audit.ledger import verify_chain
    escalated = bool(w.conn.execute("SELECT 1 FROM ledger WHERE kind = 'ESCALATION' AND case_id = ?", (case.case_id,)).fetchone())
    return Result(case.state, escalated, {f: ns.get(f) for f in sc.expected.done_fields}, executed, {}, case.touches, verify_chain(w.conn).ok)


def area_facts(w, sc, peers):
    """The area-level final state that the generic checker has no field for."""
    st = setup_of(sc)
    incs = w.conn.execute("SELECT incident_id, kind, area, status FROM incidents ORDER BY incident_id").fetchall()
    members = w.conn.execute("SELECT COUNT(*) FROM incident_members").fetchone()[0]
    replacements = w.conn.execute("SELECT COUNT(*) FROM device_replacements WHERE status != 'CANCELLED' AND created_at >= '2026-10-31'").fetchone()[0]
    notices = {r["merchant_id"]: r["status"] for r in w.conn.execute("SELECT merchant_id, status FROM incident_notices")}
    held = [m for m in [sc.persona.merchant_id] + peers if on_hold(w.conn, m)]
    return {"incidents": [[r["kind"], r["area"], r["status"]] for r in incs], "members": members, "replacements": replacements,
            "notices_sent": sum(1 for s in notices.values() if s == "SENT"), "held": len(held)}


def problems(sc, w, peers):
    st, expect = setup_of(sc), setup_of(sc)["expect"]
    bad = check(sc, result_of(w, sc))
    facts = area_facts(w, sc, peers)
    for key in ("incidents", "members", "replacements", "notices_sent", "held"):
        if facts[key] != expect[key]:
            bad.append(f"{key} is {facts[key]!r}, expected {expect[key]!r}")
    mid = sc.persona.merchant_id
    bodies = [r[0] for r in w.conn.execute("SELECT body FROM comms_outbox WHERE merchant_id = ? ORDER BY comm_id", (mid,))]
    if len(bodies) != expect["persona_messages"]:
        bad.append(f"persona got {len(bodies)} messages, expected {expect['persona_messages']}")
    pattern = LANG_SCRIPT.get(sc.persona.language)
    if expect["incidents"] and pattern and not re.search(pattern, bodies[0]):
        bad.append(f"the notice is not in the persona's language {sc.persona.language}")
    return bad


def run(db_path, clock, sc, hold=True):
    w, mgr, peers = build(db_path, clock, sc, hold=hold)
    run_incident_scenario(w, mgr, peers, sc)
    return w, mgr, peers


# ----------------------------------------------------------------------------------------------------------------- the library
def test_i1_meets_the_supported_bar_and_every_scenario_has_a_setup_and_no_setup_is_orphaned():
    cov = coverage(SCENARIOS)[PID]
    assert meets_supported_bar(cov), cov
    assert cov["dev"] >= 6 and cov["heldout"] >= 3
    for split in ("dev", "heldout"):
        ids = {s.id for s in SCENARIOS if s.split == split}
        assert set(SETUPS[split]) == ids, (split, set(SETUPS[split]) ^ ids)
    for sc in SCENARIOS:
        st = setup_of(sc)
        assert sc.persona.merchant_id not in ("M-0003",)
        assert sc.steps[0].event == TRIGGER[st["kind"]] or sc.steps[0].control or sc.steps[0].advance_settlement, sc.id


def test_the_incident_scenarios_cover_both_trigger_kinds_and_both_sides_of_each_threshold():
    kinds = {setup_of(s)["kind"] for s in SCENARIOS}
    assert kinds == {"heartbeat", "settlement"}
    for kind in kinds:
        counts = [len(setup_of(s)["expect"]["incidents"]) for s in SCENARIOS if setup_of(s)["kind"] == kind]
        assert 0 in counts and 1 in counts, (kind, counts)             # at least one that must NOT raise an incident and one that must


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scenario_final_state_matches_expectations(sc, db_path, clock):
    w, _, peers = run(db_path, clock, sc)
    assert problems(sc, w, peers) == []


# ----------------------------------------------------------------------------------------------------------------- watch-outs
def test_watch_out_grouping_by_city_would_raise_an_incident_where_pincode_grouping_does_not(db_path, clock):
    sc = BY_ID["I1-dev-noise-one-city-fifteen-pincodes"]
    st = setup_of(sc)
    assert st["scatter"] and st["devices"] == 15 and st["expect"]["incidents"] == []
    w, _, peers = run(db_path, clock, sc)
    cities = {r[0] for r in w.conn.execute("SELECT city FROM merchants WHERE merchant_id IN (%s)" % ",".join("?" * 15), [sc.persona.merchant_id] + peers)}
    assert cities == {"Bengaluru"}                                  # a city grouping WOULD have seen 15 >= 10
    assert w.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_an_incident_scenario_fails_when_the_hold_is_removed(db_path, clock):
    """Mutation proof: without the engine hold the persona's case is not parked and the scenario's expectations break."""
    sc = BY_ID["I1-dev-happy-thirty-devices-one-pincode"]
    w, _, peers = run(db_path, clock, sc, hold=False)
    assert problems(sc, w, peers) != []


def test_an_incident_scenario_fails_when_the_threshold_is_crossed_wrongly(db_path, clock):
    """A scenario that expects no incident must fail the moment the area does reach ten devices."""
    sc = BY_ID["I1-dev-noise-nine-devices-no-incident"]
    st = setup_of(sc)
    bumped = dict(SETUPS["dev"])
    bumped[sc.id] = {**st, "devices": 10, "groups": [["560034", 10]]}
    SETUPS["dev"], saved = bumped, SETUPS["dev"]
    try:
        w, _, peers = run(db_path, clock, sc)
        assert problems(sc, w, peers) != []
    finally:
        SETUPS["dev"] = saved


def test_the_model_is_not_involved_in_the_impact_graph_or_the_numbers(db_path, clock):
    sc = BY_ID["I1-dev-happy-thirty-devices-one-pincode"]
    w, mgr, peers = run(db_path, clock, sc)
    iid = w.conn.execute("SELECT incident_id FROM incidents").fetchone()[0]
    g = mgr.impact_graph(iid)
    assert sum(1 for n, d in g.nodes(data=True) if d["kind"] == "merchant") == 30
    assert sum(1 for n, d in g.nodes(data=True) if d["kind"] == "device") == 30
    body = w.conn.execute("SELECT body FROM comms_outbox WHERE merchant_id = ?", (sc.persona.merchant_id,)).fetchone()[0]
    assert "560034" in body and not re.search(r"\b30\b", body)      # the pincode comes from the system row; the notice carries no count


def test_the_library_is_not_a_copy_of_itself_dev_and_heldout_differ_in_area_and_persona():
    dev_keys = {(setup_of(s)["kind"], tuple(map(tuple, setup_of(s).get("groups", []))), s.persona.merchant_id) for s in SCENARIOS if s.split == "dev"}
    for s in SCENARIOS:
        if s.split == "heldout":
            key = (setup_of(s)["kind"], tuple(map(tuple, setup_of(s).get("groups", []))), s.persona.merchant_id)
            assert key not in dev_keys, s.id


def test_gap_a_theft_message_to_a_held_merchant_must_still_escalate(db_path, clock):
    base = BY_ID["I1-dev-adversarial-demands-replacement-during-incident"]
    steps = [base.steps[0], base.steps[1], base.steps[2].model_copy(update={"merchant_says": "my Soundbox was stolen last night"})]
    exp = base.expected.model_copy(update={"case_state": "ESCALATED", "escalation": base.expected.escalation.model_copy(update={"expected": True})})
    sc = base.model_copy(update={"steps": steps, "expected": exp})
    w, _, _ = run(db_path, clock, sc)
    assert result_of(w, sc).case_state == "ESCALATED"


# ================================================================================================================ #140 scenario library
# Reviewed as a DIFFERENT author than the held-out scenarios' writer: judged by final state, not near-copies of dev, and the
# 'supported' bar (>= 6 dev, >= 3 held-out) met for every playbook; the held-out directory is frozen by a sha256 manifest.
import difflib
import hashlib
import shutil

from core.eval.scenario import load_scenarios

MANIFEST = SC_DIR / "heldout" / "MANIFEST.sha256"
ALL = load_scenarios(SC_DIR)
PLAYBOOK_IDS = sorted(p.stem for p in (SC_DIR.parent / "playbooks").glob("*.yaml"))


def heldout_manifest(directory: Path) -> str:
    """One line per file (sha256 of its LF-normalised bytes) and a final digest over all of them; MANIFEST.sha256 itself is excluded."""
    lines, outer = [], hashlib.sha256()
    for p in sorted(directory.iterdir(), key=lambda q: q.name):      # plain string order: Path ordering is case-insensitive on Windows
        if p.name == "MANIFEST.sha256" or not p.is_file():
            continue
        digest = hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        lines.append(f"{digest}  {p.name}")
        outer.update(f"{p.name}\0{digest}\n".encode())
    return f"{outer.hexdigest()}  HELDOUT-DIGEST\n" + "\n".join(lines) + "\n"


def test_heldout_directory_matches_its_frozen_sha256_manifest():
    """Fails on ANY change to a held-out file (edit, add, delete). A legitimate change means re-freezing: regenerate MANIFEST.sha256 on purpose."""
    assert MANIFEST.read_text(encoding="utf-8") == heldout_manifest(SC_DIR / "heldout")


def test_the_manifest_check_detects_an_edit_an_addition_and_a_deletion(tmp_path):
    copy = tmp_path / "heldout"
    shutil.copytree(SC_DIR / "heldout", copy)
    frozen = heldout_manifest(copy)
    assert frozen == MANIFEST.read_text(encoding="utf-8")                 # line endings do not matter
    victim = sorted(copy.glob("C1_*.yaml"))[0]
    original = victim.read_bytes()
    victim.write_bytes(original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))      # the checkout may already be CRLF
    assert heldout_manifest(copy) == frozen
    victim.write_bytes(original + b"# tweak\n")
    assert heldout_manifest(copy) != frozen
    victim.write_bytes(original)
    (copy / "NEW.yaml").write_text("x: 1\n", encoding="utf-8")
    assert heldout_manifest(copy) != frozen
    (copy / "NEW.yaml").unlink()
    victim.unlink()
    assert heldout_manifest(copy) != frozen


def test_every_playbook_meets_the_supported_bar_of_six_dev_and_three_heldout():
    cov = coverage(ALL)
    for pid in PLAYBOOK_IDS:
        assert pid in cov and meets_supported_bar(cov[pid]), (pid, cov.get(pid))
    assert meets_supported_bar(cov[PID])


def test_every_heldout_scenario_is_judged_by_final_system_state():
    """A held-out scenario must assert something about system state (a field, an action count, an escalation); the schema has no field for reply text."""
    for s in ALL:
        if s.split != "heldout":
            continue
        e = s.expected
        assert e.done_fields or e.required_actions or e.forbidden_actions or e.escalation.expected, s.id
        assert not any("text" in k or "reply" in k or "say" in k for k in e.model_dump()), s.id


def _steps_signature(s):
    return json.dumps([[st.model_dump(exclude_none=True, exclude_defaults=True) for st in s.steps]], sort_keys=True, ensure_ascii=False)


def test_no_heldout_scenario_is_a_near_copy_of_a_dev_scenario():
    dev = [s for s in ALL if s.split == "dev" and s.playbook_id != PID]
    for h in (s for s in ALL if s.split == "heldout" and s.playbook_id != PID):
        for d in (x for x in dev if x.playbook_id == h.playbook_id):
            assert not (h.persona.merchant_id == d.persona.merchant_id and _steps_signature(h) == _steps_signature(d)), (h.id, d.id)
            ht, dt = (" ".join(st.merchant_says or "" for st in x.steps).strip() for x in (h, d))
            if ht and dt:
                assert difflib.SequenceMatcher(None, ht, dt).ratio() < 0.9, (h.id, d.id)
            assert not (h.expected == d.expected and h.persona == d.persona and _steps_signature(h) == _steps_signature(d)), (h.id, d.id)


def test_the_near_copy_check_would_catch_a_copy():
    d = next(s for s in ALL if s.split == "dev" and s.playbook_id == "C2_SOUNDBOX_FAULT")
    twin = d.model_copy(update={"id": "C2-heldout-twin", "split": "heldout"})
    assert twin.persona.merchant_id == d.persona.merchant_id and _steps_signature(twin) == _steps_signature(d)
