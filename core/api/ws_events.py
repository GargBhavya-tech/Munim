"""WebSocket /ws: pushes new ledger entries. The server tails the ledger table; the browser never polls.

On connect: {"type":"hello","sim_time":...}. Then {"type":"ledger","entry":...} for each entry appended AFTER the connection
opened. A client may send {"subscribe":{"case_id":"CS-0001"}} to receive only that case's entries (null/absent = all).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.api.deps import ledger_json, open_ro

router = APIRouter()


def _max_seq(db_path) -> int:
    conn = open_ro(db_path)
    try:
        return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger").fetchone()[0]
    finally:
        conn.close()


def _new_entries(db_path, after: int, case_id) -> tuple[list[dict], int]:
    """Entries after `after` (for the filter) and the new high-water mark over ALL entries (so a filter never re-scans)."""
    conn = open_ro(db_path)
    try:
        top = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger").fetchone()[0]
        if top <= after:
            return [], after
        sql, args = "SELECT * FROM ledger WHERE seq > ? AND seq <= ?", [after, top]
        if case_id:
            sql += " AND case_id = ?"
            args.append(case_id)
        return [ledger_json(r) for r in conn.execute(sql + " ORDER BY seq", args)], top
    finally:
        conn.close()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    st = ws.app.state
    await ws.accept()
    last = await asyncio.to_thread(_max_seq, st.db_path)      # before hello: anything appended after hello is pushed
    try:
        await ws.send_json({"type": "hello", "sim_time": st.clock.now_iso()})
    except (WebSocketDisconnect, RuntimeError):               # the browser left before hello (a page reload): nothing to stream
        return
    case_filter = None
    recv = asyncio.ensure_future(ws.receive_json())
    try:
        while True:
            done, _ = await asyncio.wait({recv}, timeout=st.ws_poll_s)
            if done:
                try:
                    msg = recv.result()
                except (WebSocketDisconnect, RuntimeError):
                    break
                except Exception:  # noqa: BLE001 - malformed frame: ignore it, keep the stream
                    msg = {}
                sub = msg.get("subscribe") if isinstance(msg, dict) else None
                if isinstance(sub, dict):
                    case_filter = sub.get("case_id") or None
                recv = asyncio.ensure_future(ws.receive_json())
            entries, last = await asyncio.to_thread(_new_entries, st.db_path, last, case_filter)
            for e in entries:
                await ws.send_json({"type": "ledger", "entry": e})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        recv.cancel()
