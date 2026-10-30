"""#130 phone token: single use, persona binding, SimClock expiry, determinism. Real ledger and session code, no fakes."""
from datetime import timedelta

import pytest

from core.audit.ledger import Ledger
from core.judge import phone
from core.judge import session as sess
from core.memory.store import connect

SECRET = "test-secret"


@pytest.fixture
def env(db_path, clock):
    conn = connect(db_path)
    return conn, Ledger(conn, clock), clock


def issue(env, persona="P-1", ttl=900, secret=SECRET):
    conn, led, clock = env
    return phone.issue_token(conn, led, clock, secret=secret, persona_id=persona, ttl_s=ttl)


def redeem(env, token, persona=None):
    conn, led, clock = env
    return phone.redeem_token(conn, led, clock, token=token, persona_id=persona)


def n_sessions(env):
    return env[0].execute("SELECT COUNT(*) FROM ledger WHERE payload_json LIKE '%JUDGE_SESSION_START%'").fetchone()[0]


def test_redeem_opens_one_session_bound_to_the_issued_persona(env):
    t = issue(env, "P-2")
    s = redeem(env, t["token"])
    assert (s.persona_id, s.merchant_id) == ("P-2", "M-0002")
    assert sess.load_session(env[0], s.session_id) == s
    assert n_sessions(env) == 1


def test_second_redeem_fails_and_creates_no_second_session(env):
    t = issue(env)
    redeem(env, t["token"])
    with pytest.raises(phone.TokenError) as e:
        redeem(env, t["token"])
    assert e.value.code == "TOKEN_USED" and n_sessions(env) == 1


def test_token_for_persona_a_never_opens_persona_b_and_is_not_burned(env):
    t = issue(env, "P-1")
    with pytest.raises(phone.TokenError) as e:
        redeem(env, t["token"], persona="P-2")
    assert e.value.code == "PERSONA_MISMATCH" and n_sessions(env) == 0
    assert redeem(env, t["token"], persona="P-1").persona_id == "P-1"      # still usable for its own persona


def test_two_tokens_for_different_personas_stay_separate(env):
    a, b = issue(env, "P-1"), issue(env, "P-2")
    assert a["token"] != b["token"]
    assert redeem(env, b["token"]).persona_id == "P-2"
    assert redeem(env, a["token"]).persona_id == "P-1"


def test_expiry_is_simclock_time(env):
    t = issue(env, ttl=60)
    env[2].advance(timedelta(seconds=61))
    with pytest.raises(phone.TokenError) as e:
        redeem(env, t["token"])
    assert e.value.code == "TOKEN_EXPIRED" and n_sessions(env) == 0


def test_token_just_inside_ttl_works(env):
    t = issue(env, ttl=60)
    env[2].advance(timedelta(seconds=59))
    assert redeem(env, t["token"]).persona_id == "P-1"


def test_unknown_and_forged_tokens_fail(env):
    issue(env)
    for bad in ("", "jt_" + "0" * 32, "nonsense"):
        with pytest.raises(phone.TokenError) as e:
            redeem(env, bad)
        assert e.value.code == "UNKNOWN_TOKEN"


def test_token_is_deterministic_from_secret_persona_and_ordinal(env):
    import hashlib
    t = issue(env, "P-1")
    assert t["token"] == "jt_" + hashlib.sha256(f"{SECRET}|P-1|1".encode()).hexdigest()[:32]
    t2 = issue(env, "P-1")
    assert t2["token"] != t["token"]                       # same persona, next ordinal


def test_only_the_hash_is_stored(env):
    t = issue(env)
    assert env[0].execute("SELECT COUNT(*) FROM ledger WHERE payload_json LIKE ?", (f"%{t['token']}%",)).fetchone()[0] == 0


def test_issue_requires_a_secret_and_a_known_persona(env):
    with pytest.raises(ValueError):
        issue(env, secret="")
    with pytest.raises(KeyError):
        issue(env, persona="P-NOPE")
