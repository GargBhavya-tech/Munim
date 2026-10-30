import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends

from .common import read_db, require

router = APIRouter(prefix="/mock/merchants")
Conn = Annotated[sqlite3.Connection, Depends(read_db("merchant"))]


@router.get("/{mid}")
def get_merchant(mid: str, conn: Conn):
    m = require(conn, "SELECT * FROM merchants WHERE merchant_id = ?", (mid,), "MERCHANT_NOT_FOUND")
    m["has_device"] = conn.execute("SELECT COUNT(*) FROM devices WHERE merchant_id = ?", (mid,)).fetchone()[0] > 0
    return m
