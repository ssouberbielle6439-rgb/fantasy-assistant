"""Odds calls (win probability, step B) on the frozen week 2 against Rival
Five (test_win_probability.py): Friday 10/30 at noon, Mobley locked in F.
advise's points calls there: PASS on Murray's 23.4 in PG, LOCK Clingan's
22.8 in C, PASS on Smith Jr.'s 14.4 in UTIL.

Most tests swap winprob.simulate for a fake that gives chosen odds to chosen
versions of the week (e.g. "+6% when Smith Jr. is locked in UTIL"), and a
different gain on the noise check's seed, so each rule is tested on known
numbers. The last tests run the real simulation.
"""

import pytest

from fantasy_assistant import __main__ as cli
from fantasy_assistant import advice_log, service, sleeper, winprob
from test_service import (CLINGAN, FRIDAY_NOON, LEAGUE_INFO, MURRAY, SMITH_JR,  # noqa: F401
                          STARTERS, save_mobley_lock)
from test_win_probability import (SIMS, lock_block, odds_at, rival, run_advise,  # noqa: F401
                                  week)

SEED, CHECK = winprob.SEED, winprob.SEED + 1
BASE_WIN, BASE_TOTAL = 0.60, 100.0


def fake_simulate(monkeypatch, effects):
    """winprob.simulate, faked: each version wins BASE_WIN plus the gain of
    every effect it matches, on that seed. `effects` maps
    ("lock", player, slot) or ("pass", player) to (gain on SEED, gain on
    CHECK, change in my expected total). Returns the seeds it was called with."""
    seeds = []

    def simulate(scenarios, theirs, games, floor, sims, seed):
        seeds.append(seed)
        out = []
        for week in scenarios:
            locked = {(s.player_id, s.label) for s in week.slots if s.locked}
            locked_players = {pid for pid, _ in locked}
            win, total = BASE_WIN, BASE_TOTAL
            for key, (gain, check_gain, points) in effects.items():
                hit = ((key[1], key[2]) in locked if key[0] == "lock"
                       else key[1] not in locked_players)
                if hit:
                    win += gain if seed == SEED else check_gain
                    total += points
            out.append(winprob.Odds(win, total, 95.0))
        return out
    monkeypatch.setattr(winprob, "simulate", simulate)
    return seeds


def odds_section(out):
    """advise's lines from 'Win probability' to the recommended locks."""
    return out.split("Win probability: ")[1].split("\nRecommended locks")[0].strip().splitlines()


# ---------- When the odds disagree ----------

def test_odds_say_lock_smith_jr_in_util_under_the_floor_when_it_gains_on_both_seeds(
        rival, monkeypatch, capsys):
    save_mobley_lock()
    seeds = fake_simulate(monkeypatch, {("lock", SMITH_JR, "UTIL"): (0.06, 0.05, -1.8)})
    out = run_advise(monkeypatch, capsys, "", "")
    assert odds_section(out)[2:] == [
        "  Where the odds disagree (today's locks; the points advice below is still the main call):",
        "    Points say PASS, odds say LOCK (+6%, gives up 1.8 expected points): "
        "J. Smith Jr. 14.4 in UTIL, 60% -> 66%  [under today's 23 floor]",
        "  (Each was checked on a second, independent 2,000 simulated weeks and still gained "
        "at least 1%.)"]
    assert seeds == [SEED, CHECK]


def test_noise_check_drops_a_gain_that_fades_on_the_second_seed(rival, monkeypatch, capsys):
    # +6% on the main simulation, only +0.4% on the re-check: luck, not shown.
    save_mobley_lock()
    seeds = fake_simulate(monkeypatch, {("lock", SMITH_JR, "UTIL"): (0.06, 0.004, -1.8)})
    out = run_advise(monkeypatch, capsys, "", "")
    assert odds_section(out)[2:] == ["  Odds agree with every call."]
    assert seeds == [SEED, CHECK]
    [entry] = advice_log.load()
    assert not any(c["shown"] for c in entry["win_probability"]["odds_calls"])


def test_noise_check_needs_a_whole_point_not_just_a_gain(rival, monkeypatch):
    # 0.99 percentage points on the re-check is under the 1-point bar.
    save_mobley_lock()
    fake_simulate(monkeypatch, {("lock", SMITH_JR, "UTIL"): (0.06, 0.0099, -1.8)})
    *_, odds = odds_at(FRIDAY_NOON)
    assert odds.disagreements == []
    fake_simulate(monkeypatch, {("lock", SMITH_JR, "UTIL"): (0.06, 0.01, -1.8)})
    *_, odds = odds_at(FRIDAY_NOON)
    [call] = odds.disagreements
    assert call.check_gain == 1.0


def test_a_gain_under_one_shown_percent_isnt_rechecked(rival, monkeypatch):
    # 60.0% -> 60.4%: both round to 60%, so there's nothing to show.
    save_mobley_lock()
    seeds = fake_simulate(monkeypatch, {("lock", SMITH_JR, "UTIL"): (0.004, 0.05, 0.0)})
    *_, odds = odds_at(FRIDAY_NOON)
    assert odds.disagreements == [] and odds.check_seed is None and seeds == [SEED]


def test_points_say_lock_clingan_but_odds_say_pass(rival, monkeypatch, capsys):
    save_mobley_lock()
    fake_simulate(monkeypatch, {("pass", CLINGAN): (0.03, 0.02, 1.1)})
    out = run_advise(monkeypatch, capsys, "", "")
    assert ("    Points say LOCK, odds say PASS (+3%, gains 1.1 expected points): "
            "D. Clingan 22.8, 60% -> 63%") in odds_section(out)


def test_points_say_lock_clingan_in_c_but_odds_say_util(rival, monkeypatch, capsys):
    # 22.8 is under Friday's 23 floor: advise locks it anyway because nobody
    # else can fill C, and the odds call carries the floor label.
    save_mobley_lock()
    fake_simulate(monkeypatch, {("lock", CLINGAN, "UTIL"): (0.02, 0.02, 0.0)})
    out = run_advise(monkeypatch, capsys, "", "")
    assert ("    Points say LOCK in C, odds say LOCK in UTIL (+2%, same expected points): "
            "D. Clingan 22.8 in UTIL, 60% -> 62%  [under today's 23 floor]") in odds_section(out)


# ---------- Picking the best version ----------

def test_best_version_is_picked_on_the_unrounded_odds(rival, monkeypatch):
    # Murray in PG +6.40%, in UTIL +6.49%: both show as 66%, UTIL is better.
    save_mobley_lock()
    fake_simulate(monkeypatch, {("lock", MURRAY, "PG"): (0.0640, 0.05, 0.0),
                                ("lock", MURRAY, "UTIL"): (0.0649, 0.05, 0.0)})
    *_, odds = odds_at(FRIDAY_NOON)
    [call] = odds.disagreements
    assert (call.player_id, call.odds, call.odds_slot, call.odds_percent) == (MURRAY, "LOCK",
                                                                            "UTIL", 66)


def test_exact_tie_goes_to_more_expected_points_then_the_restrictive_slot(rival, monkeypatch):
    save_mobley_lock()
    fake_simulate(monkeypatch, {("lock", MURRAY, "PG"): (0.05, 0.05, 0.0),
                                ("lock", MURRAY, "UTIL"): (0.05, 0.05, 0.3)})
    *_, odds = odds_at(FRIDAY_NOON)
    assert odds.disagreements[0].odds_slot == "UTIL"      # more expected points
    fake_simulate(monkeypatch, {("lock", MURRAY, "PG"): (0.05, 0.05, 0.0),
                                ("lock", MURRAY, "UTIL"): (0.05, 0.05, 0.0)})
    *_, odds = odds_at(FRIDAY_NOON)
    assert odds.disagreements[0].odds_slot == "PG"        # keep UTIL open


# ---------- Several disagreements ----------

def test_two_calls_wanting_util_the_bigger_gain_keeps_it(rival, monkeypatch):
    save_mobley_lock()
    fake_simulate(monkeypatch, {("lock", MURRAY, "UTIL"): (0.05, 0.05, 0.0),
                                ("lock", SMITH_JR, "UTIL"): (0.08, 0.06, -2.0)})
    *_, odds = odds_at(FRIDAY_NOON)
    assert [(c.player_id, c.odds_slot) for c in odds.disagreements] == [(SMITH_JR, "UTIL")]
    assert odds.combined is None   # only one left


def test_two_calls_are_also_simulated_together(rival, monkeypatch, capsys):
    save_mobley_lock()
    fake_simulate(monkeypatch, {("lock", MURRAY, "PG"): (0.04, 0.03, -0.5),
                                ("lock", SMITH_JR, "UTIL"): (0.05, 0.04, -1.8)})
    out = run_advise(monkeypatch, capsys, "", "")
    lines = odds_section(out)
    assert ("    Points say PASS, odds say LOCK (+4%, gives up 0.5 expected points): "
            "J. Murray 23.4 in PG, 60% -> 64%  [below your 25 target]") in lines
    assert "    All 2 together: 60% -> 69%, gives up 2.3 expected points." in lines


# ---------- Playoffs, the lock lines, the log ----------

def test_playoff_week_says_only_the_win_counts_and_leaves_out_points(rival, monkeypatch, capsys):
    save_mobley_lock()
    monkeypatch.setattr(sleeper, "get_state", lambda sport="nba": {"week": 22})
    fake_simulate(monkeypatch, {("lock", SMITH_JR, "UTIL"): (0.06, 0.05, -1.8)})
    out = run_advise(monkeypatch, capsys, "", "")
    lines = odds_section(out)
    assert "  Playoff week: only the win counts." in lines
    assert ("    Points say PASS, odds say LOCK (+6%): J. Smith Jr. 14.4 in UTIL, 60% -> 66%  "
            "[under today's 23 floor]") in lines


def test_playoff_start_comes_from_sleepers_league_settings_with_22_as_the_fallback():
    # Santiago's real league (checked 11/25): settings.playoff_week_start is 22.
    assert service.playoff_week_start({"settings": {"playoff_week_start": 22}}) == 22
    assert service.playoff_week_start({"settings": {"playoff_week_start": 20}}) == 20
    assert service.playoff_week_start({"settings": {}}) == 22
    assert service.playoff_week_start({}) == 22
    assert service.playoff_week_start({"settings": {"playoff_week_start": None}}) == 22
    assert service.playoff_week_start({"settings": {"playoff_week_start": 0}}) == 22


def test_a_league_whose_playoffs_start_in_week_20_is_in_its_playoffs_from_week_20(
        rival, monkeypatch):
    save_mobley_lock()
    monkeypatch.setitem(LEAGUE_INFO, "settings", {"playoff_week_start": 20})
    for week_no, playoff in ((19, False), (20, True), (23, True)):
        monkeypatch.setattr(sleeper, "get_state", lambda sport="nba", w=week_no: {"week": w})
        *_, odds = odds_at(FRIDAY_NOON)
        assert odds.playoff is playoff, week_no


def test_lock_lines_are_the_same_whatever_the_odds_say(rival, week, monkeypatch, capsys):
    save_mobley_lock()
    real = run_advise(monkeypatch, capsys, "", "")
    fake_simulate(monkeypatch, {("pass", CLINGAN): (0.03, 0.02, 1.1),
                                ("lock", SMITH_JR, "UTIL"): (0.06, 0.05, -1.8)})
    disagreeing = run_advise(monkeypatch, capsys, "", "")
    assert "Points say LOCK, odds say PASS" in disagreeing
    monkeypatch.setattr(sleeper, "get_matchups", lambda league_id, week_no: [
        {"roster_id": 1, "starters": STARTERS, "starters_points": week.sleeper_points}])
    no_opponent = run_advise(monkeypatch, capsys, "", "")
    assert lock_block(disagreeing) == lock_block(real) == lock_block(no_opponent)
    after = [x.split("Slot ", 1)[1].split("Saved as advice")[0] for x in
             (real, disagreeing, no_opponent)]
    assert after[0] == after[1] == after[2]   # the table and today's plan too


def test_advice_log_saves_every_score_in_hands_odds_call_with_both_seeds(rival, monkeypatch,
                                                                          capsys):
    save_mobley_lock()
    fake_simulate(monkeypatch, {("lock", SMITH_JR, "UTIL"): (0.06, 0.05, -1.8)})
    run_advise(monkeypatch, capsys, "", "")
    [entry] = advice_log.load()
    wp = entry["win_probability"]
    assert (wp["policy"], wp["seed"], wp["check_seed"], wp["playoff"]) == ("smart", SEED, CHECK,
                                                                          False)
    assert wp["odds_calls"] == [
        {"player_id": MURRAY, "score": 23.4, "from": "PG", "points": "PASS", "points_slot": None,
         "odds": "PASS", "odds_slot": None, "points_percent": 60, "odds_percent": 60,
         "points_given_up": 0.0, "shown": False},
        {"player_id": CLINGAN, "score": 22.8, "from": "C", "points": "LOCK", "points_slot": "C",
         "odds": "LOCK", "odds_slot": "C", "points_percent": 60, "odds_percent": 60,
         "points_given_up": 0.0, "shown": False},
        {"player_id": SMITH_JR, "score": 14.4, "from": "UTIL", "points": "PASS",
         "points_slot": None, "odds": "LOCK", "odds_slot": "UTIL", "points_percent": 60,
         "odds_percent": 66, "points_given_up": 1.8, "shown": True, "check_gain": 5.0}]
    assert "combined" not in wp


# ---------- The real simulation ----------

def test_friday_noon_odds_agree_with_every_call_and_cover_every_score_in_hand(rival, monkeypatch,
                                                                              capsys):
    save_mobley_lock()
    *_, odds = odds_at(FRIDAY_NOON)
    assert [(c.player_id, c.points, c.points_slot) for c in odds.calls] == [
        (MURRAY, "PASS", None), (CLINGAN, "LOCK", "C"), (SMITH_JR, "PASS", None)]
    assert odds.disagreements == [] and odds.check_seed is None
    assert all(c.points_percent == c.odds_percent == odds.percent for c in odds.calls)
    out = run_advise(monkeypatch, capsys, "", "")
    assert "  Odds agree with every call." in odds_section(out)


def test_odds_calls_dont_change_step_as_numbers(rival):
    save_mobley_lock()
    team, lineup, advice, opponent, with_calls = odds_at(FRIDAY_NOON)
    without = service.win_probability(team, lineup, advice, opponent, FRIDAY_NOON, sims=SIMS,
                                      odds_calls=False)
    assert without.calls == [] and with_calls.calls
    assert (with_calls.percent, with_calls.my_expected, with_calls.their_expected,
            with_calls.locks, with_calls.held) == (without.percent, without.my_expected,
                                                   without.their_expected, without.locks,
                                                   without.held)


def test_odds_say_nothing_once_the_week_is_done(rival, monkeypatch):
    from datetime import datetime
    save_mobley_lock()
    rival.at(datetime(2026, 11, 1, 12, 0))   # nobody here plays on Sunday
    monkeypatch.setattr(winprob, "simulate", lambda *a, **k: pytest.fail("simulated"))
    *_, odds = odds_at(datetime(2026, 11, 1, 12, 0))
    assert odds.final and odds.calls == []
