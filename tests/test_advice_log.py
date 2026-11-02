from datetime import date

import pytest

from fantasy_assistant import advice_log
from fantasy_assistant.advice_log import (FREE, IGNORED, PENDING, RIGHT, WRONG, SlotOutcome,
                                          calls_in, forecast_check, review)

WEEK = "2026-10-26"
WED, THU, FRI, SAT = date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30), date(2026, 10, 31)


def entry(run_id=1, slots=(), locks=(), held=(), games_left=None):
    """A saved advise run, shaped like the one cmd_advise writes."""
    return {"id": run_id, "time": "2026-10-28T14:00:00", "week": 1, "week_start": WEEK,
            "first_day": "2026-10-28", "min_lock": 25.0, "slots": list(slots),
            "locks": list(locks), "held": list(held), "games_left": games_left or {},
            "names": {"quickley": "I. Quickley", "wemby": "V. Wembanyama", "white": "D. White",
                      "murray": "J. Murray"}, "notes": []}


def lock(pid, frm, to, score):
    return {"player_id": pid, "from": frm, "to": to, "score": score, "gain": 3.0}


def row(slot, pid, score, status, waiting_on=None, expected=0.0):
    return {"slot": slot, "player_id": pid, "score": score, "status": status,
            "waiting_on": waiting_on, "expected": expected}


def graded(entries, outcomes=None, points=None, today=SAT):
    calls = review(entries, {WEEK: outcomes or {}}, points or {}, today)
    return {(c.kind, c.player_id): c for c in calls}


# ---------- LOCK: graded on the player's next game ----------

def test_lock_right_when_next_game_is_lower():
    e = entry(slots=[row("SG", "quickley", 28.0, "LOCK")],
              locks=[lock("quickley", "PG", "SG", 28.0)],
              games_left={"quickley": ["2026-10-30"]})
    c = graded([e], points={"quickley": {FRI: 18.5}})[("LOCK", "quickley")]
    assert c.verdict == RIGHT and [g.points for g in c.later] == [18.5]


def test_lock_wrong_when_next_game_beats_it():
    e = entry(locks=[lock("quickley", "PG", "SG", 28.0)], games_left={"quickley": ["2026-10-30"]})
    assert graded([e], points={"quickley": {FRI: 34.0}})[("LOCK", "quickley")].verdict == WRONG


def test_lock_right_when_he_sits_his_next_game():
    # Friday is past and there's no box score: he didn't play, so passing gave 0.
    e = entry(locks=[lock("quickley", "PG", "SG", 28.0)], games_left={"quickley": ["2026-10-30"]})
    c = graded([e], points={"quickley": {}})[("LOCK", "quickley")]
    assert c.verdict == RIGHT and c.later[0].dnp


def test_lock_pending_until_next_game_and_free_with_no_games():
    e = entry(locks=[lock("quickley", "PG", "SG", 28.0), lock("white", "SG", "G", 30.0)],
              games_left={"quickley": ["2026-10-30"]})
    calls = graded([e], today=THU)
    assert calls[("LOCK", "quickley")].verdict == PENDING
    assert calls[("LOCK", "white")].verdict == FREE  # no games left: not graded


# ---------- PASS / HELD: graded on how the slot finished ----------

def test_pass_right_when_slot_ends_higher():
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)],
              games_left={"wemby": ["2026-10-30"]})
    outcomes = {"C": SlotOutcome("wemby", 45.2, locked=True, final=True)}
    assert graded([e], outcomes)[("PASS", "wemby")].verdict == RIGHT


def test_pass_wrong_when_slot_ends_lower():
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)])
    outcomes = {"C": SlotOutcome("wemby", 12.0, locked=False, final=True)}
    assert graded([e], outcomes)[("PASS", "wemby")].verdict == WRONG


def test_pass_pending_while_slot_is_still_open():
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)])
    outcomes = {"C": SlotOutcome("wemby", 30.0, locked=False, final=False)}
    c = graded([e], outcomes)[("PASS", "wemby")]
    assert c.verdict == PENDING and "30.0" in c.detail


def test_pass_ignored_when_you_locked_it_anyway():
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)])
    outcomes = {"C": SlotOutcome("wemby", 22.0, locked=True, final=True)}
    assert graded([e], outcomes)[("PASS", "wemby")].verdict == IGNORED


def test_held_lock_is_graded_like_a_pass_not_twice():
    # White 22 is under the 25 minimum: he shows as PASS in the table AND as held.
    # It should count once, as HELD.
    e = entry(slots=[row("SG", "white", 22.0, "PASS", "white", 20.0)],
              held=[lock("white", "SG", "SG", 22.0)])
    outcomes = {"SG": SlotOutcome("murray", 31.0, locked=True, final=True)}
    calls = graded([e], outcomes)
    assert ("PASS", "white") not in calls
    assert calls[("HELD", "white")].verdict == RIGHT


# ---------- Repeats, forecasts, notes ----------

def test_same_call_in_two_runs_is_graded_once():
    first = entry(1, slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)])
    again = entry(2, slots=[row("C", "wemby", 22.0, "PASS", "wemby", 40.0)])
    calls = review([first, again], {WEEK: {}}, {}, SAT)
    assert len(calls) == 1 and calls[0].run_id == 1 and calls[0].also_in == [2]


def test_forecast_check_uses_first_forecast_of_finished_slots():
    first = entry(1, slots=[row("C", "wemby", 0.0, "WAIT", "wemby", 41.0),
                            row("PG", "murray", 0.0, "WAIT", "murray", 30.0)])
    again = entry(2, slots=[row("C", "wemby", 0.0, "WAIT", "wemby", 38.0)])
    outcomes = {"C": SlotOutcome("wemby", 45.0, True, True),
                "PG": SlotOutcome("murray", 20.0, False, False)}  # PG not finished
    assert forecast_check([first, again], {WEEK: outcomes}) == [(41.0, 45.0)]


def test_saving_and_notes(tmp_path):
    path = tmp_path / "advice_log.json"
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)])
    del e["id"], e["notes"]
    assert advice_log.append(e, path) == 1
    assert advice_log.append(dict(e), path) == 2
    advice_log.add_note(1, "locked him anyway, too risky", slot="C", path=path)
    saved = advice_log.load(path)
    assert saved[0]["notes"][0]["slot"] == "C"
    assert saved[0]["notes"][0]["text"] == "locked him anyway, too risky"
    assert saved[1]["notes"] == []
    with pytest.raises(KeyError):
        advice_log.add_note(99, "nope", path=path)


def test_calls_follow_players_moved_by_a_swap():
    # Quickley locks in SG by swapping with White, so White now sits in PG.
    e = entry(slots=[row("PG", "white", 15.0, "PASS", "murray", 35.0),
                     row("SG", "quickley", 28.0, "LOCK")],
              locks=[lock("quickley", "PG", "SG", 28.0)], held=[lock("white", "SG", "G", 15.0)])
    held = next(c for c in calls_in(e) if c.kind == "HELD")
    assert held.slot == "PG"
