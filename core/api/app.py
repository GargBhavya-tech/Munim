"""The dashboard API (docs/API_CONTRACT.md). ``create_api`` builds the FastAPI app around injected collaborators."""
from __future__ import annotations

import os
import threading
from typing import Callable

from fastapi import FastAPI

from pathlib import Path

from core.api import (routes_approvals, routes_arena, routes_cases, routes_control, routes_coverage, routes_export, routes_handoffs, routes_judge, routes_ladder, routes_ledger, routes_messages,
                      routes_leads, routes_ops, routes_phone, routes_sales, routes_policy_compile, routes_questions, routes_systems, routes_twin, routes_voice, ws_events)
from core.memory.store import connect


def create_api(db_path, clock, executor, mock_http, *, on_approve=None, registry=None, after_write: Callable[[], None] | None = None,
               ws_poll_s: float = 0.2, rules=None, root=None, pack_dir=None, results_path=None, voice=None) -> FastAPI:
    """db_path: the shared SQLite file. clock: the SimClock. executor: core.actions.executor.Executor (undo goes through it).
    mock_http: HTTP client of the in-process mock app, used ONLY by the demo controls (pay, chaos).
    rules: policy Rule objects for 'Kyun nahi?' (default: the Paytm merchant pack). root/pack_dir/results_path: where the coverage matrix reads
    the pack, hashes the code and finds recorded scenario results (defaults: this repo, packs/paytm_merchant, eval/out/scenario_results.json).
    after_write: optional hook run after the clock moves, a payment is simulated or a handoff is returned (e.g. dispatcher.run())."""
    app = FastAPI(title="Munim dashboard API")
    st = app.state
    st.db_path, st.clock, st.executor, st.mock_http = db_path, clock, executor, mock_http
    st.on_approve, st.registry, st.ws_poll_s = on_approve, registry, ws_poll_s
    st.rules = rules
    st.voice = voice                                  # a core.voice.pipeline.VoiceTurnProcessor, or None (the /ws/voice endpoint then refuses)
    st.root = Path(root) if root else Path(__file__).resolve().parents[2]
    st.pack_dir = Path(pack_dir) if pack_dir else st.root / "packs" / "paytm_merchant"
    st.results_path = Path(results_path) if results_path else st.root / "eval" / "out" / "scenario_results.json"
    st.phone_secret = os.environ.get("MUNIM_PHONE_SECRET")      # signs phone tokens (#130); issuing returns 503 until it is set
    st.lock = threading.RLock()                      # serialises use of the shared executor / clock / mock client
    st.conn = connect(db_path)                       # for routes_approvals (its decide() takes the write lock itself)

    def _after() -> None:
        if after_write is not None:
            with st.lock:
                after_write()
    st.after_write = _after

    app.include_router(routes_cases.router)
    app.include_router(routes_systems.router)
    app.include_router(routes_leads.router)
    app.include_router(routes_ops.router)
    app.include_router(routes_sales.router)
    app.include_router(routes_ledger.router)
    app.include_router(routes_coverage.router)
    app.include_router(routes_questions.router)
    app.include_router(routes_ladder.router)
    app.include_router(routes_voice.router)
    app.include_router(routes_messages.router)
    app.include_router(routes_handoffs.router)
    app.include_router(routes_control.router)
    app.include_router(routes_judge.router)
    app.include_router(routes_export.router)
    app.include_router(routes_arena.router)
    app.include_router(routes_twin.router)
    app.include_router(routes_policy_compile.router)
    app.include_router(routes_policy_compile.tour_router)
    app.include_router(routes_phone.router)
    app.include_router(routes_approvals.router)
    app.include_router(ws_events.router)
    return app
