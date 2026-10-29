import itertools
import random
from datetime import date

from fantasy_assistant.lineup import (FutureGame, LockMove, Slot, best_assignment, can_fill,
                                      move_instructions, plan_week, player_values,
                                      recommend_locks, slot_labels)
from fantasy_assistant.lockin import GameForecast
from fantasy_assistant.locks import parse_answer

TUE, WED, FRI = date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 30)
STAR = GameForecast(40, 10, 0.97)
ROLE = GameForecast(20, 6, 0.97)
GUARD = GameForecast(20, 6, 0.97)  # a typical guard: 20+ on an ok night


def games(**players):
    """games(fox=[(TUE, GUARD), (WED, GUARD)]) -> future dict for player_values."""
    future = {}
    for pid, slate in players.items():
        for day, fc in slate:
            future.setdefault(day, []).append(FutureGame(pid, day, fc))
    return future


def test_slot_labels_unique():
    assert slot_labels(["PG", "G", "UTIL", "UTIL"]) == ["PG", "G", "UTIL1", "UTIL2"]


def test_eligibility():
    assert can_fill("G", {"PG"})
    assert can_fill("F", {"SF", "SG"})
    assert not can_fill("C", {"PF"})
    assert can_fill("UTIL", {"C"})


def test_assignment_matches_brute_force():
    rng = random.Random(7)
    for _ in range(200):
        rows, cols = rng.randint(1, 5), rng.randint(5, 7)
        w = [[rng.choice([0, rng.uniform(5, 40)]) for _ in range(cols)] for _ in range(rows)]
        got = sum(w[i][j] for i, j in enumerate(best_assignment(w)))
        best = max(sum(w[i][p[i]] for i in range(rows))
                   for p in itertools.permutations(range(cols), rows))
        assert abs(got - best) < 1e-9


def test_more_games_means_higher_week_value():
    vals = player_values(games(a=[(WED, ROLE)], b=[(TUE, ROLE), (WED, ROLE), (FRI, ROLE)]))
    assert vals["b"] > vals["a"]


def test_each_player_fills_only_one_slot():
    # The bug Santiago caught: one player was being counted toward several slots.
    slots = [Slot("UTIL1", "UTIL", None, 0), Slot("UTIL2", "UTIL", None, 0)]
    vals = player_values(games(wemby=[(TUE, STAR), (WED, STAR), (FRI, STAR)]))
    plan = plan_week(slots, vals, {"wemby": {"C"}})
    assert sorted(p for p in plan.assignment.values() if p) == ["wemby"]


def test_restrictive_slot_gets_the_only_eligible_player():
    slots = [Slot("UTIL", "UTIL", None, 0), Slot("PG", "PG", None, 0)]
    vals = player_values(games(guard=[(WED, ROLE)], big=[(WED, STAR)]))
    plan = plan_week(slots, vals, {"guard": {"PG"}, "big": {"C"}})
    assert plan.assignment == {"UTIL": "big", "PG": "guard"}


def test_dont_lock_white_at_15_with_guards_on_the_bench():
    # Santiago's feedback: never lock Derrick White for 15.1 when Fox and
    # Quickley can still fill guard slots this week.
    slots = [Slot("SG", "SG", "white", 15.1), Slot("G", "G", "fox", 0.0)]
    future = games(white=[(FRI, GUARD)], fox=[(TUE, GUARD), (WED, GUARD)],
                   quickley=[(WED, GUARD), (FRI, GUARD)])
    positions = {"white": {"SG", "PG"}, "fox": {"PG", "SG"}, "quickley": {"PG", "SG"}}
    moves, held = recommend_locks(slots, player_values(future), positions)
    assert moves == [] and held == []


def test_quickley_goes_to_sg_not_pg():
    # Santiago's real mistake: G is locked, Quickley (PG/SG) has 28 in the PG slot,
    # and Jamal Murray (PG only) still has a big game coming Friday.
    slots = [Slot("PG", "PG", "quickley", 28.0), Slot("SG", "SG", "white", 0.0),
             Slot("G", "G", "fox", 30.0, locked=True)]
    positions = {"quickley": {"PG", "SG"}, "white": {"SG", "PG"}, "murray": {"PG"}}
    future = games(murray=[(FRI, STAR)], white=[(WED, ROLE)], quickley=[(FRI, ROLE)])
    moves, _ = recommend_locks(slots, player_values(future), positions)
    assert [(m.player_id, m.to_label) for m in moves] == [("quickley", "SG")]


def test_lock_players_with_no_games_left_in_the_right_slot():
    # Quickley is done for the week: keeping him is free, so lock him, in SG.
    slots = [Slot("PG", "PG", "quickley", 28.0), Slot("SG", "SG", "white", 0.0)]
    positions = {"quickley": {"PG", "SG"}, "white": {"SG", "PG"}, "murray": {"PG"}}
    future = games(murray=[(FRI, STAR)], white=[(WED, ROLE)])
    moves, _ = recommend_locks(slots, player_values(future), positions)
    assert [(m.player_id, m.to_label) for m in moves] == [("quickley", "SG")]


def test_pass_when_a_star_has_games_left():
    slots = [Slot("C", "C", "wemby", 22.0)]
    future = games(wemby=[(FRI, STAR)])
    moves, _ = recommend_locks(slots, player_values(future), {"wemby": {"C"}})
    assert moves == []


def test_minimum_lock_rule_and_its_exception():
    positions = {"a": {"C"}, "b": {"C"}}
    # 22 beats a weak backup, but it's under the 25 minimum -> held back.
    slots = [Slot("C", "C", "a", 22.0)]
    weak_night = GameForecast(12, 4, 0.97)
    weak = player_values(games(a=[(FRI, weak_night)], b=[(FRI, weak_night)]))
    moves, held = recommend_locks(slots, weak, positions)
    assert moves == [] and [h.player_id for h in held] == ["a"]
    # Same score with the minimum lowered -> lock.
    moves, _ = recommend_locks(slots, weak, positions, min_lock=20)
    assert [m.player_id for m in moves] == ["a"]
    # Nobody else can fill the slot -> lock even under the minimum.
    moves, _ = recommend_locks(slots, player_values(games(a=[(FRI, weak_night)])), positions)
    assert [m.player_id for m in moves] == ["a"]


def test_parse_lock_answer():
    labels = ["PG", "F", "UTIL1", "UTIL2"]
    assert parse_answer("", labels, ["PG"]) == ["PG"]          # Enter keeps saved
    assert parse_answer("none", labels, ["PG"]) == []
    assert parse_answer("pg, util2", labels, []) == ["PG", "UTIL2"]
    assert parse_answer("PG XYZ", labels, []) is None         # typo -> ask again


def test_powershell_piped_locks_keep_their_first_slot():
    # `"PG F" | python -m fantasy_assistant advise` in PowerShell 5.1 arrives as
    # "﻿PG F": the hidden mark made PG unknown and the saved locks were used instead.
    labels = ["PG", "F", "UTIL1", "UTIL2"]
    assert parse_answer("﻿PG F", labels, ["UTIL1"]) == ["PG", "F"]
    assert parse_answer("﻿", labels, ["UTIL1"]) == ["UTIL1"]   # piped "" keeps saved


def test_swap_instructions_follow_sleeper_rule():
    slots = [Slot("PG", "PG", "quickley", 26.0), Slot("SG", "SG", "white", 15.0)]
    moves = [LockMove("quickley", "PG", "SG", 26.0, 5.0)]
    name = str.title
    steps = move_instructions(slots, moves, {"white": {"SG", "PG"}}, name)
    assert steps[0].startswith("Swap Quickley (PG) with White (SG)")
    steps = move_instructions(slots, moves, {"white": {"SG"}}, name)
    assert "can't play PG" in steps[0] and "clears his 15.0" in steps[0]
