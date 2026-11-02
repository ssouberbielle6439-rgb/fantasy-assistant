import itertools
import random
from datetime import date

from fantasy_assistant.lineup import (FutureGame, LockMove, Slot, best_assignment, can_fill,
                                      move_instructions, plan_week, player_values,
                                      recommend_locks, slot_labels)
from fantasy_assistant import locks
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


def test_sunday_night_murray_stays_at_pg_instead_of_swapping_with_white():
    # Real lineup, Sunday 11/01 after 7pm: nobody has games left, so every lock
    # gains 0. Rounding noise used to break the tie and said "lock Murray in G",
    # which swaps White into PG for nothing (Murray was already locked at PG).
    kinds = ["PG", "SG", "G", "SF", "PF", "F", "C", "UTIL", "UTIL"]
    lineup = [("murray", 28.8), ("quickley", 24.3), ("white", 14.1), ("george", 24.8),
              ("smith", 30.1), ("mobley", 25.3), ("wemby", 44.5), ("kgeorge", 12.9),
              ("clingan", 11.4)]
    positions = {"murray": {"PG"}, "quickley": {"PG", "SG"}, "white": {"PG", "SG"},
                 "george": {"PF", "SF", "SG"}, "smith": {"C", "PF"}, "mobley": {"C", "PF"},
                 "wemby": {"C"}, "kgeorge": {"SF", "SG"}, "clingan": {"C"}}
    slots = [Slot(label, kind, pid, score)
             for label, kind, (pid, score) in zip(slot_labels(kinds), kinds, lineup)]
    moves, held = recommend_locks(slots, {}, positions)
    assert moves and all(m.from_label == m.to_label for m in moves + held)


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


def test_powershell_pipe_byte_order_mark_is_ignored():
    # Piping into advise from Windows PowerShell 5.1 prepends an invisible
    # byte-order mark; it printed "Didn't recognize that" before this fix.
    labels = ["PG", "PF", "UTIL1", "UTIL2"]
    assert parse_answer("\ufeff", labels, ["PG"]) == ["PG"]            # "" keeps saved
    assert parse_answer("\ufeffPG PF", labels, []) == ["PG", "PF"]     # slots still read
    assert parse_answer("\ufeffnone", labels, ["PG"]) == []


def test_powershell_piped_locks_keep_their_first_slot():
    # `"PG F" | python -m fantasy_assistant advise` in PowerShell 5.1 arrives as
    # "\ufeffPG F": the hidden mark made PG unknown and the saved locks were used instead.
    labels = ["PG", "F", "UTIL1", "UTIL2"]
    assert parse_answer("\ufeffPG F", labels, ["UTIL1"]) == ["PG", "F"]
    assert parse_answer("\ufeff", labels, ["UTIL1"]) == ["UTIL1"]   # piped "" keeps saved


def sunday_night_lineup():
    """Sleeper's API on Sunday 11/01: Mobley and P. George locked at 27.9 in Sleeper,
    but the matchup endpoint showed 25.3 and 24.8 for them."""
    return [Slot("PG", "PG", "murray", 28.8), Slot("SF", "SF", "george", 24.8),
            Slot("PF", "PF", "smith", 30.1), Slot("F", "F", "mobley", 25.3)]


def test_mobley_and_george_keep_their_279_locks_when_sleeper_shows_other_games():
    saved = {"F": {"player_id": "mobley", "score": 27.9},
             "SF": {"player_id": "george", "score": 27.9}}
    slots = sunday_night_lineup()
    notes = locks.disagreements(slots, saved)
    locks.lock_slots(slots, ["F", "SF"], saved, kept=True)
    by_label = {s.label: s for s in slots}
    assert by_label["F"].score == 27.9 and by_label["SF"].score == 27.9
    assert by_label["F"].locked and not by_label["PG"].locked
    assert len(notes) == 2 and "Sleeper's API now says 25.3" in notes[0] + notes[1]


def test_first_table_shows_mobleys_saved_279_not_sleepers_253():
    # The opening lineup table printed Sleeper's 25.3 for Mobley's locked F,
    # even though the saved lock (27.9) is what the app actually uses.
    saved = {"F": {"player_id": "mobley", "score": 27.9},
             "PG": {"player_id": "murray", "score": None}}  # old file: no score saved
    by_label = {s.label: s for s in sunday_night_lineup()}
    assert locks.shown(by_label["F"], saved) == ("mobley", 27.9)
    assert locks.shown(by_label["PG"], saved) == ("murray", 28.8)    # falls back to Sleeper
    assert locks.shown(by_label["PF"], saved) == ("smith", 30.1)     # not locked


def test_typed_locked_score_fixes_what_sleeper_shows():
    # Locked in Sleeper without telling the app, and the API shows the wrong game:
    # "PG PF F=27.9 SF=27.9" sets the scores by hand.
    slots = sunday_night_lineup()
    labels = [s.label for s in slots]
    answer = "﻿PG PF F=27.9 SF=27.9"
    chosen = parse_answer(answer, labels, [])
    assert chosen == ["PG", "PF", "F", "SF"]
    locks.lock_slots(slots, chosen, {}, locks.typed_scores(answer, labels))
    assert [s.score for s in slots] == [28.8, 27.9, 30.1, 27.9]
    assert locks.from_slots(slots)["F"] == {"player_id": "mobley", "score": 27.9}
    assert parse_answer("F=abc", labels, []) is None


def test_saved_lock_isnt_erased_when_sleeper_shows_someone_else_there():
    # A saved lock used to be dropped without a word (and the file overwritten)
    # whenever Sleeper showed a different player in that slot. Enter now keeps
    # it, with a note; typing the slots takes Sleeper's lineup.
    saved = {"PG": {"player_id": "murray", "score": 28.8},
             "PF": {"player_id": "smith", "score": 30.1}}
    slots = [Slot("PG", "PG", "white", 14.1), Slot("PF", "PF", "smith", 31.7)]
    assert "Sleeper shows white" in locks.disagreements(slots, saved)[0]
    locks.lock_slots(slots, parse_answer("", ["PG", "PF"], list(saved)), saved, kept=True)
    assert [(s.player_id, s.score, s.locked) for s in slots] == [("murray", 28.8, True),
                                                                ("smith", 30.1, True)]
    slots = [Slot("PG", "PG", "white", 14.1), Slot("PF", "PF", "smith", 31.7)]
    locks.lock_slots(slots, ["PG", "PF"], saved)
    assert [(s.player_id, s.score) for s in slots] == [("white", 14.1), ("smith", 30.1)]


def test_lock_files_from_before_scores_were_saved_still_load(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "DATA_DIR", tmp_path)
    (tmp_path / "locks_2026-10-26.json").write_text('{"PG": "murray"}', encoding="utf-8")
    saved = locks.load(date(2026, 10, 26))
    assert saved == {"PG": {"player_id": "murray", "score": None}}
    slots = [Slot("PG", "PG", "murray", 23.4)]
    locks.lock_slots(slots, ["PG"], saved, kept=True)
    assert slots[0].score == 23.4   # no saved score -> Sleeper's number


def test_answers_typed_at_the_1942_prompt_keep_pg_pf_c():
    # The prompt shows "Enter to keep: PG, PF, C", so answers get typed like that.
    labels = ["PG", "SG", "G", "SF", "PF", "F", "C", "UTIL1", "UTIL2"]
    for answer in ["PG, PF, C", "pg pf c", "  PG   PF  C  ", "pg,pf,c", "PG,\tPF, C "]:
        assert parse_answer(answer, labels, []) == ["PG", "PF", "C"], answer
    # Only 'none' means nothing is locked; a stray comma is asked again.
    assert parse_answer(",", labels, ["PG"]) is None
    assert parse_answer(" , , ", labels, ["PG"]) is None


def ask_with(monkeypatch, answers, slots, saved):
    """Run advise's lock prompt with these typed lines; then input ends, like a pipe."""
    from fantasy_assistant.__main__ import _ask_locks

    lines = iter(answers)

    def fake_input(prompt):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError
    monkeypatch.setattr("builtins.input", fake_input)
    _ask_locks(slots, saved)
    return [s.label for s in slots if s.locked]


def locked_at_1930():
    saved = {"PG": {"player_id": "murray", "score": 28.8},
             "PF": {"player_id": "smith", "score": 30.1},
             "C": {"player_id": "wemby", "score": 44.5}}
    slots = [Slot("PG", "PG", "murray", 23.4), Slot("PF", "PF", "smith", 30.1),
             Slot("C", "C", "wemby", 44.5), Slot("UTIL1", "UTIL", "clingan", 11.4)]
    return slots, saved


def test_saved_locks_wiped_at_1942_now_need_a_yes_to_remove(monkeypatch):
    # 11/01 19:42: the first run after the merge saved no locks, wiping PG, PF, C.
    slots, saved = locked_at_1930()
    assert ask_with(monkeypatch, ["none"], slots, saved) == ["PG", "PF", "C"]   # no yes: kept
    slots, saved = locked_at_1930()
    assert ask_with(monkeypatch, ["pg pf", "no", ""], slots, saved) == ["PG", "PF", "C"]
    slots, saved = locked_at_1930()
    assert ask_with(monkeypatch, ["none", "yes"], slots, saved) == []         # confirmed
    slots, saved = locked_at_1930()
    assert ask_with(monkeypatch, ["PG PF C UTIL1"], slots, saved) == ["PG", "PF", "C", "UTIL1"]
    assert slots[0].score == 28.8    # adding a lock keeps the saved scores, no question asked


def test_swap_instructions_follow_sleeper_rule():
    slots = [Slot("PG", "PG", "quickley", 26.0), Slot("SG", "SG", "white", 15.0)]
    moves = [LockMove("quickley", "PG", "SG", 26.0, 5.0)]
    name = str.title
    steps = move_instructions(slots, moves, {"white": {"SG", "PG"}}, name)
    assert steps[0].startswith("Swap Quickley (PG) with White (SG)")
    steps = move_instructions(slots, moves, {"white": {"SG"}}, name)
    assert "can't play PG" in steps[0] and "clears his 15.0" in steps[0]
