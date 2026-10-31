"""#104 style learning: Thompson sampling per segment; the reward is a qualified conversation / conversion, never a reply."""
import numpy as np
import pytest

from jobs import style_bandit as sb
from jobs.style_bandit import BanditError, StyleBandit, reward_for

# A synthetic world where reply rate and conversion DISAGREE: the flashy opening gets answers and no business, the plain one the reverse.
ENV = {"flashy": {"reply": 0.60, "qualify": 0.05}, "plain": {"reply": 0.20, "qualify": 0.30}}
OPTIONS = {"opening_line": ["flashy", "plain"], "channel": ["whatsapp", "voice"]}


def run(bandit, segment, env, rounds, rng, reward_replies=False, options=OPTIONS):
    picks = []
    for _ in range(rounds):
        c = bandit.choose(segment, options)
        e = env[c.choice["opening_line"]]
        replied = rng.random() < e["reply"]
        qualified = rng.random() < e["qualify"]
        outcome = "QUALIFIED" if (qualified or (reward_replies and replied)) else ("REPLIED" if replied else "NO_REPLY")
        bandit.record(c.decision_id, outcome)
        picks.append(c.choice["opening_line"])
    return picks


def test_reward_is_conversion_not_reply():
    assert [reward_for(o) for o in ("QUALIFIED", "WON", "CONVERTED")] == [1, 1, 1]
    assert [reward_for(o) for o in ("REPLIED", "ENGAGED", "NO_REPLY", "NOT_NOW", "DO_NOT_CONTACT", "")] == [0] * 6


def test_converges_on_conversion_when_reply_rate_points_the_other_way(conn, clock):
    b = StyleBandit(conn, clock, seed=3)
    picks = run(b, "kirana", ENV, 700, np.random.default_rng(11))
    late = picks[-200:]
    assert late.count("plain") / len(late) >= 0.85                       # converged on the style that converts (30% vs 5%)
    assert b.best("kirana", "opening_line") == "plain"
    # the style with the higher REPLY rate really is "flashy": the test would be meaningless otherwise
    assert ENV["flashy"]["reply"] > ENV["plain"]["reply"] and ENV["plain"]["qualify"] > ENV["flashy"]["qualify"]
    a, bt, pulls = b.posterior("kirana", "opening_line", "plain")
    assert pulls > 400 and a / (a + bt) == pytest.approx(0.30, abs=0.06)


def test_watch_out_optimising_for_reply_rate_would_converge_on_the_wrong_style(conn, clock, monkeypatch):
    """The named mistake, made on purpose: if replies counted as success the bandit picks 'flashy'. Proves the first test can fail."""
    monkeypatch.setattr(sb, "REWARD_OUTCOMES", frozenset({"QUALIFIED", "WON", "CONVERTED", "REPLIED"}))
    b = StyleBandit(conn, clock, seed=3)
    picks = run(b, "kirana", ENV, 700, np.random.default_rng(11))
    assert picks[-200:].count("flashy") / 200 >= 0.7
    assert b.best("kirana", "opening_line") == "flashy"


def test_segments_learn_separately(conn, clock):
    b = StyleBandit(conn, clock, seed=5)
    flipped = {"flashy": ENV["plain"], "plain": ENV["flashy"]}
    pk = run(b, "kirana", ENV, 500, np.random.default_rng(1))
    pd = run(b, "d2c", flipped, 500, np.random.default_rng(2))
    assert pk[-150:].count("plain") / 150 >= 0.8 and pd[-150:].count("flashy") / 150 >= 0.8
    assert b.posterior("kirana", "opening_line", "flashy")[2] != b.posterior("d2c", "opening_line", "flashy")[2]


def test_an_explicit_preference_always_wins_and_is_not_learned_from(conn, clock):
    b = StyleBandit(conn, clock, seed=1)
    opts = {"language_mix": ["en-IN", "hinglish", "hi-IN"], "time": ["11:00", "17:00"], "channel": ["whatsapp", "voice"]}
    for _ in range(40):
        c = b.choose("kirana", opts, explicit={"language_mix": "en-IN", "time": "21:00"})
        assert c.choice["language_mix"] == "en-IN" and c.choice["time"] == "21:00"        # even a time that is not among the learned options
        assert c.forced == ("language_mix", "time")
        b.record(c.decision_id, "QUALIFIED")
    assert b.posterior("kirana", "language_mix", "en-IN")[2] == 0                          # forced choices teach nothing
    assert b.posterior("kirana", "time", "21:00")[2] == 0
    assert sum(b.posterior("kirana", "channel", o)[2] for o in ("whatsapp", "voice")) == 40  # the free dimension did learn


def test_a_decision_is_learned_from_once_and_late_outcomes_are_fine(conn, clock):
    b = StyleBandit(conn, clock, seed=2)
    c = b.choose("kirana", {"channel": ["whatsapp"]})
    assert b.posterior("kirana", "channel", "whatsapp") == (1.0, 1.0, 0)                  # no outcome yet: nothing learned
    assert b.record(c.decision_id, "QUALIFIED") == 1
    assert b.record(c.decision_id, "NO_REPLY") == 1                                        # a second report cannot change it or count twice
    assert b.posterior("kirana", "channel", "whatsapp") == (2.0, 1.0, 1)


def test_choosing_is_deterministic_for_a_seed_and_varies_with_it(conn, clock):
    b1, b2 = StyleBandit(conn, clock, seed=9), StyleBandit(conn, clock, seed=9)
    first = [b1.choose("seg-a", {"opening_line": ["a", "b", "c"]}).choice for _ in range(10)]
    conn.execute("DELETE FROM style_decisions")
    second = [b2.choose("seg-a", {"opening_line": ["a", "b", "c"]}).choice for _ in range(10)]
    assert first == second and len({c["opening_line"] for c in first}) > 1


def test_unknown_dimensions_and_empty_option_lists_are_refused(conn, clock):
    b = StyleBandit(conn, clock)
    with pytest.raises(BanditError):
        b.choose("kirana", {"price": ["low", "high"]})                                     # price is a fact, not a style
    with pytest.raises(BanditError):
        b.choose("kirana", {"channel": []})
    with pytest.raises(BanditError):
        b.choose("kirana", {"channel": ["whatsapp"]}, explicit={"discount": "10"})


def test_only_the_options_the_caller_allows_are_ever_chosen(conn, clock):
    b = StyleBandit(conn, clock, seed=4)
    for _ in range(60):
        assert b.choose("kirana", {"channel": ["whatsapp"]}).choice == {"channel": "whatsapp"}       # e.g. the only consented channel
