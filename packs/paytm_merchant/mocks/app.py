"""Assemble the 15 mock systems into one FastAPI app sharing a database and a simulated clock."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from core.engine.clock import SimClock

from . import bank, chaos, closures, comms, control, device, kyc, lender, merchant, payments, plans, pricing, refunds, remote, risk, scheduler, settlements, tickets
from .common import MockContext

ROUTERS = (merchant, kyc, bank, device, remote, payments, settlements, pricing, plans, risk, tickets, scheduler, comms, lender, refunds, closures, chaos, control)


def create_app(db_path: Path | str, clock: SimClock | None = None, chaos_seed: int = 0) -> FastAPI:
    app = FastAPI(title="Munim mock Paytm systems (SIMULATED)")
    app.state.ctx = MockContext(db_path=db_path, clock=clock or SimClock(), chaos_seed=chaos_seed)
    for mod in ROUTERS:
        app.include_router(mod.router)
    app.include_router(remote.fault_router)
    return app
