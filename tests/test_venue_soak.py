"""#150 venue game soak: 1,000 scripted adversarial attempts against the guarded arena before the event.

Budget, per-player cap and kill switch are all exercised. The model is the scripted stand-in from tests/fakes_gate.py, so the soak can never spend real
Sarvam budget; the arena's own SIMULATED budget meter is what is being tested: it must stop everyone at the configured paise, not after it.
"""
import pytest

from core.judge import arena as A
from core.judge.arena import ArenaError, ArenaStopped, level_true
from tests.test_judge_arena import ATTACKS, lang_en, make, say  # noqa: F401  (fixtures)
from tests.test_turn_assembly import INVENTED, decision, render

PLAYERS = 25
PER_PLAYER = 40                                   # 25 x 40 = 1,000 attempts, exactly the per-player cap


def attack(a, token, n, seed):
    seed = (seed * 1103515245 + 12345) % (2**31)
    text, proposal, claims = ATTACKS[seed % len(ATTACKS)]
    replies = [decision(proposal, claims), decision(proposal, claims)] if claims == [INVENTED] else [decision(proposal, claims), render(*claims)]
    return say(a, token, text, replies), seed


def test_a_thousand_attempts_clear_no_level_and_the_caps_and_kill_switch_hold(make):
    a = make(player_burst=10**6, global_burst=10**6, max_attempts_per_player=PER_PLAYER, max_sandboxes=PLAYERS + 5, budget_paise=10**9)
    tokens = [a.join(f"Soaker {i}")["token"] for i in range(PLAYERS)]
    seed = 777
    for n in range(PLAYERS * PER_PLAYER):
        r, seed = attack(a, tokens[n % PLAYERS], n, seed)
        assert r["cleared_now"] == [], n
    assert a.meta.execute("SELECT COUNT(*) FROM arena_attempts").fetchone()[0] == 1000
    assert a.meta.execute("SELECT COUNT(*) FROM arena_clears").fetchone()[0] == 0
    for sb in a.sandboxes.values():
        assert [lv.id for lv in a.levels if level_true(sb.ro, lv, sb.base_seq)] == []
    blocked = a.meta.execute("SELECT SUM(blocked) FROM arena_attempts").fetchone()[0]
    assert blocked > 30                                                                              # non-vacuous: the guards really stopped things
    assert 0 < a.spent_paise() < 10**9                                                               # the meter ran, on the simulated rate

    with pytest.raises(ArenaError):                                                                   # cap: attempt 41 for a player is refused
        say(a, tokens[0], "one more", [decision(None, [])])
    assert a.meta.execute("SELECT COUNT(*) FROM arena_attempts").fetchone()[0] == 1000

    a.kill()                                                                                          # kill switch: everyone stopped, nobody can join
    with pytest.raises(ArenaStopped):
        a.join("Latecomer")
    a.revive()
    fresh = a.join("Latecomer")["token"]
    r, _ = attack(a, fresh, 0, seed)
    assert r["cleared_now"] == [] and a.meta.execute("SELECT COUNT(*) FROM arena_attempts").fetchone()[0] == 1001


def test_the_budget_stop_triggers_at_the_configured_amount_not_after_it(make):
    budget, reserve = 20, 1                       # the scripted model reports few tokens: the whole 1,000-attempt soak costs 29 paise, so 20 stops it part-way
    a = make(player_burst=10**6, global_burst=10**6, max_attempts_per_player=10**6, max_sandboxes=PLAYERS + 5, budget_paise=budget, reserve_paise=reserve)
    tokens = [a.join(f"Soaker {i}")["token"] for i in range(PLAYERS)]
    seed, done = 4242, 0
    with pytest.raises(ArenaStopped) as stop:
        for n in range(1000):
            _, seed = attack(a, tokens[n % PLAYERS], n, seed)
            done += 1
    assert stop.value.extra["state"] == A.BUDGET_STOPPED
    assert 0 < done < 1000                                                                            # it stopped the soak early
    assert a.spent_paise() <= budget                                                                  # never past the configured amount
    assert a.spent_paise() + reserve > budget                                                         # and only because the reserve no longer fit
    with pytest.raises(ArenaStopped):
        a.join("Newcomer")
