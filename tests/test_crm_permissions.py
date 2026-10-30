import pytest

from core.memory.crm import AGENT_NEVER, AGENT_RO, AGENT_RW, CRMGateway, CRMPermissionError

M = "M-0001"


@pytest.fixture
def crm(conn, clock):
    return CRMGateway(conn, clock)


@pytest.mark.parametrize("field", sorted(AGENT_RW))
def test_agent_can_read_and_write_rw_fields(crm, field):
    e = crm.agent_write(M, field, "v1")
    assert e.written_by == "agent" and e.actor == "munim"
    got = crm.agent_read(M, field)
    assert (got[-1] if isinstance(got, list) else got).value == "v1"


@pytest.mark.parametrize("field", sorted(AGENT_RO))
def test_ro_fields_readable_not_writable(crm, field, conn):
    crm.system_load(M, field, "premium")
    assert crm.agent_read(M, field).value == "premium"
    with pytest.raises(CRMPermissionError, match="read-only"):
        crm.agent_write(M, field, "free")
    assert len(crm.history(M, field)) == 1


@pytest.mark.parametrize("field", sorted(AGENT_NEVER))
def test_never_fields_blocked_for_read_and_write(crm, field):
    with pytest.raises(CRMPermissionError):
        crm.agent_read(M, field)
    with pytest.raises(CRMPermissionError):
        crm.agent_write(M, field, "x")
    with pytest.raises(CRMPermissionError):
        crm.human_edit(M, field, "x", "rep-1")  # not even stored in this store
    assert crm.history(M, field) == []


def test_unknown_field_denied(crm):
    with pytest.raises(CRMPermissionError, match="unknown"):
        crm.agent_write(M, "favourite_colour", "x")


def test_agent_can_never_delete(crm, conn):
    crm.agent_write(M, "stage", "ACTIVATING")
    with pytest.raises(CRMPermissionError, match="never delete"):
        crm.agent_delete(M, "stage")
    assert len(crm.history(M, "stage")) == 1
    assert not hasattr(crm, "delete")


def test_agent_writes_add_not_overwrite(crm, clock):
    crm.agent_write(M, "next_action", "call tomorrow")
    crm.agent_write(M, "next_action", "send link")
    h = crm.history(M, "next_action")
    assert [x.value for x in h] == ["call tomorrow", "send link"] and all(x.written_by == "agent" for x in h)
    assert crm.agent_read(M, "next_action").value == "send link"


def test_human_edit_wins_agent_cannot_change_it_back(crm, conn):
    crm.agent_write(M, "stage", "ACTIVATING")
    crm.human_edit(M, "stage", "CHURNED", "rep-7")
    with pytest.raises(CRMPermissionError, match="edited by a human"):
        crm.agent_write(M, "stage", "ACTIVATING")
    assert crm.agent_read(M, "stage").value == "CHURNED" and crm.agent_read(M, "stage").written_by == "human"
    assert [x.value for x in crm.history(M, "stage")] == ["ACTIVATING", "CHURNED"]
    assert conn.execute("SELECT outcome FROM crm_log WHERE op='WRITE' ORDER BY log_id DESC LIMIT 1").fetchone()[0] == "REFUSED"
    # other merchants and other fields are unaffected
    crm.agent_write("M-0002", "stage", "NEW")
    crm.agent_write(M, "next_action", "x")
    # the human can hand control back by editing again, and a later human edit still wins
    crm.human_edit(M, "stage", "ACTIVE", "rep-7")
    with pytest.raises(CRMPermissionError):
        crm.agent_write(M, "stage", "NEW")


def test_activity_log_entries_append_even_after_human_entry(crm):
    crm.human_edit(M, "activity_log", "rep called", "rep-7")
    crm.agent_write(M, "activity_log", "bot sent link")
    assert [(e.written_by, e.value) for e in crm.agent_read(M, "activity_log")] == [("human", "rep called"), ("agent", "bot sent link")]


def test_ensure_schema_idempotent(conn, clock):
    CRMGateway(conn, clock); CRMGateway(conn, clock)
