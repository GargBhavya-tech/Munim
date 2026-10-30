import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.engine.clock import DEFAULT_START, IST, SimClock, iso, parse_iso

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ["core", "packs", "jobs", "field", "integrations", "data", "scripts", "prop"]
ALLOWED = {"core/engine/clock.py", "core/eval/latency.py"}
FORBIDDEN = [
    r"datetime\.now\(", r"datetime\.utcnow\(", r"date\.today\(", r"time\.time\(",
    r"CURRENT_TIMESTAMP", r"datetime\('now'", r"strftime\('%s',\s*'now'",
]


def test_no_module_reads_the_wall_clock_except_the_clock():
    offenders = []
    for top in SCANNED:
        for path in (ROOT / top).rglob("*"):
            if path.suffix not in {".py", ".sql"} or not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED:
                continue
            text = path.read_text()
            for pat in FORBIDDEN:
                if re.search(pat, text):
                    offenders.append(f"{rel}: {pat}")
    assert offenders == []


def test_advance_fires_timer_scheduled_at_47h_exactly_once():
    clock = SimClock()
    fired = []
    clock.call_at(clock.now() + timedelta(hours=47), lambda: fired.append(clock.now()))
    clock.advance(timedelta(hours=48))
    assert len(fired) == 1
    assert fired[0] == DEFAULT_START + timedelta(hours=47)  # clock reads due time inside the callback
    clock.advance(timedelta(hours=48))
    assert len(fired) == 1
    assert clock.now() == DEFAULT_START + timedelta(hours=96)


def test_timers_fire_in_order_and_cascade():
    clock = SimClock()
    order = []

    def first():
        order.append("a")
        clock.call_at(clock.now() + timedelta(minutes=30), lambda: order.append("c"))

    clock.call_at(clock.now() + timedelta(hours=2), lambda: order.append("b"))
    clock.call_at(clock.now() + timedelta(hours=1), first)
    clock.advance(timedelta(hours=5))
    assert order == ["a", "c", "b"]


def test_cancelled_timer_never_fires():
    clock = SimClock()
    fired = []
    h = clock.call_at(clock.now() + timedelta(hours=1), lambda: fired.append(1))
    assert clock.cancel(h) is True
    assert clock.cancel(h) is False
    clock.advance(timedelta(hours=2))
    assert fired == []


def test_set_forward_fires_backward_does_not():
    clock = SimClock()
    fired = []
    clock.call_at(clock.now() + timedelta(hours=1), lambda: fired.append(1))
    clock.set(clock.now() + timedelta(hours=2))
    assert fired == [1]
    clock.set(DEFAULT_START)
    assert clock.now() == DEFAULT_START


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        SimClock(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        iso(datetime(2026, 1, 1))


def test_iso_roundtrip_is_ist():
    s = iso(DEFAULT_START)
    assert s.endswith("+05:30")
    assert parse_iso(s) == DEFAULT_START and parse_iso(s).tzinfo == IST or parse_iso(s).utcoffset() == IST.utcoffset(None)
