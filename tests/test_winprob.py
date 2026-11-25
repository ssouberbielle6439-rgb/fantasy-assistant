"""The win-probability simulation (winprob.py) on small made-up weeks: no
network, no service. Every run uses a fixed seed."""

import random
from datetime import date
from statistics import mean, stdev

from fantasy_assistant import winprob
from fantasy_assistant.lineup import FutureGame, lock_floor
from fantasy_assistant.lockin import GameForecast, value_of_waiting
from fantasy_assistant.winprob import PASSIVE, SlotPlan, TeamWeek

FRI, SAT, SUN = date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)
SEED = 7


def games(pid, *forecasts, days=(FRI, SAT, SUN)):
    """A player's remaining games: (mean, sd, play_prob) per game, Friday on."""
    return [FutureGame(pid, day, GameForecast(*fc)) for day, fc in zip(days, forecasts)]


def team(*slots):
    return TeamWeek(tuple(slots))


def totals(teams, all_games, floor=None, sims=2000, seed=SEED):
    """Each team's total in every simulated week."""
    return [[sum(t.values()) for t in week]
            for week in winprob.simulated_weeks(teams, all_games, floor, sims, seed)]


# ---------- Reproducible ----------

GAMES = {"a": games("a", (24, 8, 0.95), (24, 8, 0.95)),
         "b": games("b", (20, 7, 0.9), (20, 7, 0.9), (20, 7, 0.9)),
         "x": games("x", (23, 8, 0.95), (23, 8, 0.95))}
MINE = team(SlotPlan("PG", "a", 0.0), SlotPlan("C", "b", 0.0), SlotPlan("F", None, 27.9, True))
THEIRS = team(SlotPlan("PG", "x", 0.0), SlotPlan("C", None, 40.0, True))


def test_same_seed_same_answer_and_another_seed_a_slightly_different_one():
    first = winprob.simulate([MINE], THEIRS, GAMES, lock_floor, sims=3000, seed=SEED)
    again = winprob.simulate([MINE], THEIRS, GAMES, lock_floor, sims=3000, seed=SEED)
    other = winprob.simulate([MINE], THEIRS, GAMES, lock_floor, sims=3000, seed=SEED + 1)
    assert first == again
    assert first != other and abs(first[0].win - other[0].win) < 0.05


def test_week_seed_is_one_string_random_accepts():
    # random.seed() takes no tuples on Python 3.11+, so (seed, k) becomes "seed:k".
    assert winprob.week_seed(2026, 17) == "2026:17"
    assert random.Random("2026:17").random() == random.Random(winprob.week_seed(2026, 17)).random()


# ---------- The same draws for every version of the week ----------

def test_skipping_a_lock_cant_shift_anyone_elses_draws():
    # With the lock, A's 31.0 is locked in PG; without it, PG waits on A's
    # games. C (B's slot) and the opponent must come out the same in every
    # simulated week either way, and also when more versions are compared.
    with_lock = team(SlotPlan("PG", "a", 31.0, True), SlotPlan("C", "b", 0.0))
    skip_it = team(SlotPlan("PG", "a", 0.0), SlotPlan("C", "b", 0.0))
    two = list(winprob.simulated_weeks([with_lock, skip_it, THEIRS], GAMES, lock_floor, 500, SEED))
    one = list(winprob.simulated_weeks([with_lock, THEIRS], GAMES, lock_floor, 500, SEED))
    assert all(w[0]["C"] == w[1]["C"] for w in two)
    assert [w[2] for w in two] == [w[1] for w in one]         # the opponent's weeks
    assert [w[0] for w in two] == [w[0] for w in one]         # and yours with the lock
    assert any(w[0]["PG"] != w[1]["PG"] for w in two)         # only A's slot changed


def test_every_game_is_drawn_even_if_no_slot_uses_it():
    # Each game takes two random numbers whether or not a policy reads it, so
    # a draw depends only on (seed, week) and its place in the fixed order.
    order = winprob.game_order(GAMES)
    assert [pid for pid, _ in order] == ["a", "b", "x"]
    drawn = winprob.draw_week(order, SEED, 0)
    assert [len(drawn[p]) for p in ("a", "b", "x")] == [2, 3, 2]
    assert winprob.draw_week(order, SEED, 0) == drawn


# ---------- The policies ----------

def test_app_slot_locks_a_game_that_beats_his_bar_and_passive_keeps_the_last_game():
    sure = {"a": games("a", (30, 0, 1.0), (10, 0, 1.0))}   # 30 Friday, then 10 Saturday
    week = next(winprob.simulated_weeks(
        [team(SlotPlan("PG", "a", 0.0)), team(SlotPlan("PG", "a", 0.0, policy=PASSIVE))],
        sure, None, 1, SEED))
    assert [w["PG"] for w in week] == [30.0, 10.0]


def test_sitting_out_the_last_game_scores_0_even_after_a_big_game():
    sat_out = {"a": games("a", (35, 0, 1.0), (20, 0, 0.0))}   # plays Friday, sits Saturday
    [passive] = next(winprob.simulated_weeks([team(SlotPlan("PG", "a", 18.0, policy=PASSIVE))],
                                             sat_out, None, 1, SEED))
    assert passive["PG"] == 0.0   # rule 3: no reverting to his 35


def test_passive_slots_keep_the_locks_entered_for_them():
    [week] = next(winprob.simulated_weeks(
        [team(SlotPlan("PG", "a", 26.0, True, PASSIVE), SlotPlan("C", "nobody", 12.0,
                                                                policy=PASSIVE))],
        GAMES, None, 1, SEED))
    assert week == {"PG": 26.0, "C": 12.0}   # locked; no games left keeps his score


def test_the_floor_holds_back_a_lock_through_saturday_but_not_20_on_sunday():
    fri = {"a": games("a", (22, 0, 1.0), (10, 0, 1.0))}                  # Fri 22, then Sat 10
    sun = {"a": games("a", (21, 0, 1.0), (10, 0, 1.0), days=(SUN, SUN))}  # Sunday's floor is 20
    slot = [team(SlotPlan("PG", "a", 0.0))]
    assert next(winprob.simulated_weeks(slot, fri, None, 1, SEED))[0]["PG"] == 22.0
    assert next(winprob.simulated_weeks(slot, fri, lock_floor, 1, SEED))[0]["PG"] == 10.0
    assert next(winprob.simulated_weeks(slot, sun, lock_floor, 1, SEED))[0]["PG"] == 21.0
    assert winprob.lock_bars(fri["a"], lock_floor) == [23.0, None]


# ---------- Checks on the numbers ----------

def test_with_the_floor_off_the_average_total_matches_the_plans_expected_total():
    # The plan's Exp for a slot is the lock-in value of its player's games
    # (lockin.value_of_waiting), so the simulated average must match it.
    week = team(SlotPlan("PG", "a", 0.0), SlotPlan("C", "b", 0.0), SlotPlan("F", None, 27.9, True))
    expected = (27.9 + value_of_waiting([g.forecast for g in GAMES["a"]])
                + value_of_waiting([g.forecast for g in GAMES["b"]]))
    mine = [t[0] for t in totals([week], GAMES, floor=None, sims=20000)]
    error = stdev(mine) / len(mine) ** 0.5
    assert abs(mean(mine) - expected) < 4 * error


def test_a_sure_win_is_100_percent_and_a_dead_even_tie_is_50():
    locked_high = team(SlotPlan("PG", None, 200.0, True))
    [odds] = winprob.simulate([locked_high], THEIRS, GAMES, lock_floor, sims=500, seed=SEED)
    assert (odds.win, odds.percent) == (1.0, 100)
    even = team(SlotPlan("PG", None, 50.0, True))
    [tie] = winprob.simulate([even], even, GAMES, lock_floor, sims=100, seed=SEED)
    assert tie.win == 0.5   # a tie counts half


def test_two_teams_with_the_same_players_win_about_half_the_time():
    twins = {"a": GAMES["a"], "a2": games("a2", (24, 8, 0.95), (24, 8, 0.95))}
    [odds] = winprob.simulate([team(SlotPlan("PG", "a", 0.0))], team(SlotPlan("PG", "a2", 0.0)),
                              twins, lock_floor, sims=10000, seed=SEED)
    assert abs(odds.win - 0.5) < 0.02


# ---------- The speedup: same numbers, each player worked out once a week ----------

# A busier made-up week: several versions of my lineup sharing players (b in
# SG in one version and G in another), locked slots, a slot nobody fills, a
# player with no games left, and both opponent policies.
SPEED_GAMES = {"a": GAMES["a"], "b": GAMES["b"], "x": GAMES["x"],
               "c": games("c", (31, 10, 0.65), (31, 10, 0.97), (31, 10, 0.97)),
               "d": games("d", (17, 6, 0.97), (17, 6, 0.45), days=(SAT, SUN)),
               "y": games("y", (28, 9, 0.9), (28, 9, 0.9), (28, 9, 0.15))}
SPEED_MINE = [
    team(SlotPlan("PG", "a", 0.0), SlotPlan("SG", "b", 0.0), SlotPlan("G", "d", 0.0),
         SlotPlan("C", "c", 0.0), SlotPlan("F", None, 27.9, True), SlotPlan("UTIL1", "gone", 21.4)),
    team(SlotPlan("PG", "a", 31.0, True), SlotPlan("SG", "d", 0.0), SlotPlan("G", "b", 0.0),
         SlotPlan("C", "c", 0.0), SlotPlan("F", None, 27.9, True), SlotPlan("UTIL1", None, 0.0)),
    team(SlotPlan("PG", "a", 0.0), SlotPlan("SG", "b", 0.0), SlotPlan("G", "d", 0.0),
         SlotPlan("C", "c", 26.2, True), SlotPlan("F", None, 27.9, True),
         SlotPlan("UTIL1", "gone", 21.4)),
]
SPEED_THEIRS = {"smart": team(SlotPlan("PG", "x", 0.0), SlotPlan("C", "y", 0.0),
                              SlotPlan("F", None, 90.0, True)),
                "passive": team(SlotPlan("PG", "x", 12.0, policy=PASSIVE),
                                SlotPlan("C", "y", 0.0, policy=PASSIVE),
                                SlotPlan("F", "a", 118.0, True, PASSIVE))}

# (win, my average total, their average total) per version of my week, as the
# code before the speedup (commit 6459173) printed them for
# winprob.simulate(SPEED_MINE, SPEED_THEIRS[...], SPEED_GAMES, lock_floor,
# sims=2000, seed=SEED).
SMART_BEFORE = [(0.494, 144.383024897, 143.91599135965205),
                (0.232, 127.66341451733786, 143.91599135965205),
                (0.321, 134.84351035107224, 143.91599135965205)]
PASSIVE_BEFORE = [(0.5205, 144.383024897, 144.08319341537083),
                  (0.222, 127.66341451733786, 144.08319341537083),
                  (0.3375, 134.84351035107224, 144.08319341537083)]


def simulate_slot_by_slot(scenarios, theirs, all_games, floor, sims, seed):
    """winprob.simulate as it was before the speedup: every slot of every
    version scored on its own with slot_score, totals summed from {slot: score}."""
    order = winprob.game_order(all_games)
    bars = {pid: winprob.lock_bars(gs, floor) for pid, gs in order}
    n = len(scenarios)
    wins, mine_sum, theirs_sum = [0.0] * n, [0.0] * n, 0.0
    for k in range(sims):
        drawn = winprob.draw_week(order, seed, k)
        week = [{s.label: winprob.slot_score(s, drawn, bars) for s in t.slots}
                for t in scenarios + [theirs]]
        their_total = sum(week[-1].values())
        theirs_sum += their_total
        for i in range(n):
            my_total = sum(week[i].values())
            mine_sum[i] += my_total
            wins[i] += 1.0 if my_total > their_total else 0.5 if my_total == their_total else 0.0
    return [winprob.Odds(wins[i] / sims, mine_sum[i] / sims, theirs_sum / sims) for i in range(n)]


def test_speedup_gives_exactly_the_same_numbers_as_scoring_slot_by_slot():
    for policy, theirs in SPEED_THEIRS.items():
        for floor in (lock_floor, None):
            fast = winprob.simulate(SPEED_MINE, theirs, SPEED_GAMES, floor, sims=2000, seed=SEED)
            slow = simulate_slot_by_slot(SPEED_MINE, theirs, SPEED_GAMES, floor, 2000, SEED)
            assert fast == slow, (policy, floor)   # equal floats, not just close


def test_speedup_gives_the_numbers_the_code_gave_before_it():
    # Same seed, same numbers as SMART_BEFORE and PASSIVE_BEFORE, to the last bit.
    smart = winprob.simulate(SPEED_MINE, SPEED_THEIRS["smart"], SPEED_GAMES, lock_floor,
                             sims=2000, seed=SEED)
    passive = winprob.simulate(SPEED_MINE, SPEED_THEIRS["passive"], SPEED_GAMES, lock_floor,
                               sims=2000, seed=SEED)
    assert [(o.win, o.mine, o.theirs) for o in smart] == SMART_BEFORE
    assert [(o.win, o.mine, o.theirs) for o in passive] == PASSIVE_BEFORE


def test_each_player_is_worked_out_once_a_week_however_many_slots_wait_on_him(monkeypatch):
    calls = []
    real = winprob.player_end
    monkeypatch.setattr(winprob, "player_end",
                        lambda games, bars, policy: calls.append(policy) or real(games, bars, policy))
    list(winprob.simulated_slot_scores(SPEED_MINE + [SPEED_THEIRS["passive"]], SPEED_GAMES,
                                       lock_floor, sims=10, seed=SEED))
    # 12 slots across the 4 teams wait on someone, but only 6 (player, policy)
    # pairs: a, b, c, d (app; each in 2 or 3 versions) and x, y (passive).
    # The passive team's locked a isn't simulated. So 6 a week, not 12.
    assert len(calls) == 6 * 10


def test_settled_once_no_slot_has_a_game_left():
    assert not MINE.settled(GAMES)
    done = team(SlotPlan("PG", "a", 30.0, True), SlotPlan("C", "nobody", 12.0))
    assert done.settled(GAMES) and done.total_now() == 42.0
