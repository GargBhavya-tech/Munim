"""Remote-fix toolkit for Soundbox devices (#93; Bible section 19). SIMULATED.

Each OFFLINE / silent device has a hidden fault the remote steps can or cannot clear (set by the scenario control):
  NEEDS_RESTART  -> restart brings it ONLINE and the test announcement passes
  VOLUME_ZERO    -> the device is online but silent; a volume reset makes the announcement pass
  CONFIG_STALE   -> a config push fixes it (and can be rolled back)
  NETWORK        -> the connectivity check reports weak signal; nothing remote fixes it (a SIM swap / visit does)
  HARDWARE       -> nothing remote helps (the default for an offline device): a replacement is the fix
Firmware updates are ops-only and are NOT here (they are never an agent tool).
"""
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .common import Ctx, Idem, Result, execute_write, fetch_one, new_id, read_db, require, require_w

router = APIRouter(prefix="/mock/devices")
Conn = Annotated[sqlite3.Connection, Depends(read_db("device"))]
FAULTS = ("NEEDS_RESTART", "VOLUME_ZERO", "CONFIG_STALE", "NETWORK", "HARDWARE")
SCHEMA = """
CREATE TABLE IF NOT EXISTS device_faults (merchant_id TEXT PRIMARY KEY, fault TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS device_remote_ops (
  op_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, op TEXT NOT NULL, ts TEXT NOT NULL, result TEXT NOT NULL, config_version TEXT, status TEXT NOT NULL
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    """DDL one statement at a time: executescript would COMMIT the executor's open savepoint."""
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def fault_of(conn: sqlite3.Connection, mid: str, device: dict) -> str | None:
    try:
        r = conn.execute("SELECT fault FROM device_faults WHERE merchant_id = ?", (mid,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r:
        return r[0]
    return "HARDWARE" if device["status"] == "OFFLINE" else None


def remote_attempts(conn: sqlite3.Connection, mid: str) -> int:
    try:
        return conn.execute("SELECT COUNT(*) FROM device_remote_ops WHERE merchant_id = ? AND op != 'connectivity'", (mid,)).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


class ConfigIn(BaseModel):
    config_version: str = Field(default="cfg-latest", max_length=40)


class FaultIn(BaseModel):
    status: str = Field(pattern="^(NEEDS_RESTART|VOLUME_ZERO|CONFIG_STALE|NETWORK|HARDWARE|NONE)$")


def _op(op: str, fixes: tuple[str, ...], mid: str, ctx: Ctx, idem, extra: dict | None = None):
    def fn(conn):
        ensure(conn)
        d = require_w(conn, "SELECT * FROM devices WHERE merchant_id = ?", (mid,), "DEVICE_NOT_FOUND")
        fault = fault_of(conn, mid, d)
        fixed = fault in fixes
        if fixed:
            conn.execute("UPDATE devices SET status = 'ONLINE', last_heartbeat = ?, last_test_announcement_ok = 1 WHERE merchant_id = ?", (ctx.now_iso(), mid))
            conn.execute("INSERT OR REPLACE INTO device_faults VALUES (?, 'NONE')", (mid,))
        oid = new_id(conn, "RO")
        conn.execute("INSERT INTO device_remote_ops VALUES (?,?,?,?,?,?,?)", (oid, mid, op, ctx.now_iso(), "FIXED" if fixed else "NO_CHANGE",
                                                                             (extra or {}).get("config_version"), "DONE"))
        return Result(200, {"op_id": oid, "op": op, "result": "FIXED" if fixed else "NO_CHANGE",
                            "test_announcement_ok": bool(fixed or (d["last_test_announcement_ok"] and fault is None))})
    return execute_write(ctx, "device", idem, {"op": f"device.{op}", "mid": mid, **(extra or {})}, fn)


@router.post("/{mid}/restart")
def restart(mid: str, ctx: Ctx, idem: Idem = None):
    return _op("restart", ("NEEDS_RESTART",), mid, ctx, idem)


@router.post("/{mid}/volume-reset")
def volume_reset(mid: str, ctx: Ctx, idem: Idem = None):
    return _op("volume_reset", ("VOLUME_ZERO",), mid, ctx, idem)


@router.post("/{mid}/config")
def push_config(mid: str, body: ConfigIn, ctx: Ctx, idem: Idem = None):
    return _op("config_push", ("CONFIG_STALE",), mid, ctx, idem, {"config_version": body.config_version})


@router.delete("/{mid}/config/{op_id}")
def rollback_config(mid: str, op_id: str, ctx: Ctx, idem: Idem = None):
    def fn(conn):
        ensure(conn)
        require_w(conn, "SELECT 1 FROM device_remote_ops WHERE op_id = ? AND merchant_id = ? AND op = 'config_push'", (op_id, mid), "OP_NOT_FOUND")
        conn.execute("UPDATE device_remote_ops SET status = 'ROLLED_BACK' WHERE op_id = ?", (op_id,))
        return Result(200, {"op_id": op_id, "status": "ROLLED_BACK"})
    return execute_write(ctx, "device", idem, {"op": "device.config_rollback", "mid": mid, "op_id": op_id}, fn)


@router.get("/{mid}/connectivity")
def connectivity(mid: str, conn: Conn):
    """Read-only diagnostics: battery, signal, heartbeat, last announcement and what the network looks like."""
    require(conn, "SELECT 1 FROM merchants WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
    d = fetch_one(conn, "SELECT * FROM devices WHERE merchant_id = ?", (mid,))
    if d is None:
        return {"merchant_id": mid, "device_id": None, "reachable": False}
    fault = fault_of(conn, mid, d)
    signal = 12 if fault == "NETWORK" else d["signal_pct"]
    return {"merchant_id": mid, "device_id": d["device_id"], "status": d["status"], "battery_pct": d["battery_pct"], "signal_pct": signal,
            "last_heartbeat": d["last_heartbeat"], "last_test_announcement_ok": bool(d["last_test_announcement_ok"]),
            "reachable": d["status"] == "ONLINE" or fault in ("NEEDS_RESTART", "CONFIG_STALE", "VOLUME_ZERO"), "weak_signal": signal is not None and signal < 25,
            "remote_fix_attempts": remote_attempts(conn, mid)}


fault_router = APIRouter(prefix="/mock/control")


@fault_router.post("/device-fault/{mid}")
def set_fault(mid: str, body: FaultIn, ctx: Ctx):
    """Scenario control: what is really wrong with the device (hidden from the agent; only remote results reveal it)."""
    conn = ctx.connect()
    try:
        ensure(conn)
        conn.execute("INSERT OR REPLACE INTO device_faults VALUES (?,?)", (mid, body.status))
        if body.status in ("NEEDS_RESTART", "CONFIG_STALE", "NETWORK", "HARDWARE"):
            conn.execute("UPDATE devices SET status = 'OFFLINE', last_test_announcement_ok = 0 WHERE merchant_id = ?", (mid,))
        elif body.status == "VOLUME_ZERO":
            conn.execute("UPDATE devices SET status = 'ONLINE', last_test_announcement_ok = 0 WHERE merchant_id = ?", (mid,))
        return {"ok": True}
    finally:
        conn.close()
