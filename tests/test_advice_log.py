from datetime import date

import pytest

from fantasy_assistant import advice_log
from fantasy_assistant.advice_log import (FREE, PENDING, RIGHT, WRONG, SlotOutcome,
                                          calls_in, forecast_check, review)

WEEK = "2026-10-26"
WED, THU, FRI, SAT = date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30), date(2026, 10, 31)
FRIDAY_GAME = {"wemby": ["2026-10-30"], "white": ["2026-10-30"]}  # a game left after the advice


def entry(run_id=1, slots=(), locks=(), held=(), games_left=None):
    """A saved advise run, shaped like the one cmd_advise writes."""
    return {"id": run_id, "time": "2026-10-28T14:00:00", "week": 1, "week_start": WEEK,
            "first_day": "2026-10-28", "floor": 23.0, "target": 25.0, "slots": list(slots),
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


# ---------- PASS / HELD: graded on the player's next game, like LOCK ----------

def test_pass_right_when_next_game_beats_it_even_if_the_slot_ends_lower():
    # The slot's finish is shown, but doesn't decide the grade.
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)], games_left=FRIDAY_GAME)
    outcomes = {"C": SlotOutcome("wemby", 12.0, locked=False, final=True)}
    c = graded([e], outcomes, points={"wemby": {FRI: 30.0}})[("PASS", "wemby")]
    assert c.verdict == RIGHT and "slot ended at 12.0 (V. Wembanyama)" in c.detail


def test_pass_wrong_when_next_game_is_lower_even_if_the_slot_ends_higher():
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)], games_left=FRIDAY_GAME)
    outcomes = {"C": SlotOutcome("wemby", 45.2, locked=True, final=True)}
    c = graded([e], outcomes, points={"wemby": {FRI: 15.0}})[("PASS", "wemby")]
    assert c.verdict == WRONG and "slot ended at 45.2" in c.detail


def test_pass_and_lock_on_the_same_tied_game_are_mirror_images():
    # A next game equal to the score: locking lost nothing, passing gained nothing.
    e = entry(locks=[lock("quickley", "PG", "PG", 22.0)],
              slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)],
              games_left={"quickley": ["2026-10-30"], "wemby": ["2026-10-30"]})
    calls = graded([e], points={"quickley": {FRI: 22.0}, "wemby": {FRI: 22.0}})
    assert calls[("LOCK", "quickley")].verdict == RIGHT
    assert calls[("PASS", "wemby")].verdict == WRONG


def test_pass_pending_until_his_next_game_and_shows_the_slot_so_far():
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)], games_left=FRIDAY_GAME)
    outcomes = {"C": SlotOutcome("wemby", 30.0, locked=False, final=False)}
    c = graded([e], outcomes, today=THU)[("PASS", "wemby")]
    assert c.verdict == PENDING and "slot so far 30.0" in c.detail


def test_pass_you_locked_anyway_is_still_graded_and_marked_as_your_override():
    # The app said PASS on Wemby's 22.0 and White's 22.0 (held by the floor), you
    # locked both anyway. Wemby's next game was 30.0 (the app was right), White's
    # 18.0 (your override beat the app).
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0),
                     row("SG", "white", 22.0, "PASS", "white", 20.0)],
              held=[lock("white", "SG", "SG", 22.0)], games_left=FRIDAY_GAME)
    outcomes = {"C": SlotOutcome("wemby", 22.0, locked=True, final=True),
                "SG": SlotOutcome("white", 22.0, locked=True, final=True)}
    calls = graded([e], outcomes, points={"wemby": {FRI: 30.0}, "white": {FRI: 18.0}})
    wemby, white = calls[("PASS", "wemby")], calls[("HELD", "white")]
    assert wemby.override and wemby.verdict == RIGHT and "you locked it anyway" in wemby.detail
    assert white.override and white.verdict == WRONG and "overrode the floor" in white.detail
    assert advice_log.override_record(list(calls.values())) == (1, 2)


def test_override_record_leaves_out_pending_and_calls_you_followed():
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0),
                     row("PG", "quickley", 19.0, "PASS", "quickley", 25.0)],
              games_left={"wemby": ["2026-10-30"], "quickley": ["2026-10-30"]})
    outcomes = {"C": SlotOutcome("wemby", 22.0, locked=True, final=True)}  # overrode Wemby
    calls = list(graded([e], outcomes, today=THU).values())  # Friday not played yet
    assert advice_log.override_record(calls) == (0, 0)
    calls = list(graded([e], outcomes, points={"wemby": {FRI: 12.0}, "quickley": {FRI: 10.0}})
                 .values())
    assert advice_log.override_record(calls) == (1, 1)  # Quickley's PASS wasn't overridden


def test_held_lock_is_graded_like_a_pass_not_twice():
    # White 22 is under the 23 floor: he shows as PASS in the table AND as held.
    # It should count once, as HELD.
    e = entry(slots=[row("SG", "white", 22.0, "PASS", "white", 20.0)],
              held=[lock("white", "SG", "SG", 22.0)], games_left=FRIDAY_GAME)
    outcomes = {"SG": SlotOutcome("murray", 31.0, locked=True, final=True)}
    calls = graded([e], outcomes, points={"white": {FRI: 27.5}})
    assert ("PASS", "white") not in calls
    assert calls[("HELD", "white")].verdict == RIGHT


def test_sunday_night_pass_and_held_with_no_games_left_arent_graded():
    # Sunday 11/01 late: nobody had games left, yet advise showed PASS and
    # "held back" calls that review then graded as if passing had a cost.
    e = entry(slots=[row("UTIL2", "clingan", 11.4, "PASS"), row("SG", "white", 14.1, "PASS")],
              held=[lock("white", "SG", "SG", 14.1)])
    outcomes = {"UTIL2": SlotOutcome("clingan", 11.4, locked=False, final=True),
                "SG": SlotOutcome("white", 14.1, locked=False, final=True)}
    calls = graded([e], outcomes)
    assert calls[("PASS", "clingan")].verdict == FREE
    assert calls[("HELD", "white")].verdict == FREE


# ---------- Week 3 (11/02 to 11/08): real PASS calls from run #7, Thu 11/05 9:45am ----------

WEEK3 = "2026-11-02"
FRI_1106, SAT_1107, MON_1109 = date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 9)


def week3_pass(pid, name, slot, score, games_left, points):
    """Grade one of run #7's PASS calls against his real next game."""
    e = {**entry(7, slots=[row(slot, pid, score, "PASS", pid, 20.0)],
                 games_left={pid: games_left}), "week_start": WEEK3, "names": {pid: name}}
    return review([e], {WEEK3: {}}, {pid: points}, MON_1109)[0]


def test_week3_smith_jr_pass_at_17_0_right_his_next_game_was_25_8():
    c = week3_pass("2445", "J. Smith Jr.", "PF", 17.0, ["2026-11-07"], {SAT_1107: 25.8})
    assert c.verdict == RIGHT and c.detail.startswith("next game 25.8 beat it")


def test_week3_mobley_pass_at_24_8_right_his_next_game_was_26_7():
    c = week3_pass("2259", "E. Mobley", "F", 24.8, ["2026-11-06", "2026-11-07"],
                   {FRI_1106: 26.7, SAT_1107: 16.6})  # only Friday's game counts
    assert c.verdict == RIGHT and c.detail.startswith("next game 26.7 beat it")


def test_week3_quickley_pass_at_18_0_wrong_he_sat_out_his_next_game():
    c = week3_pass("2157", "I. Quickley", "G", 18.0, ["2026-11-06"], {})  # no box score: DNP
    assert c.verdict == WRONG and c.later[0].dnp and c.detail.startswith("next game he sat")


def test_week3_fox_pass_at_13_3_wrong_his_next_game_was_10_2():
    c = week3_pass("1872", "D. Fox", "UTIL2", 13.3, ["2026-11-06", "2026-11-07"],
                   {FRI_1106: 10.2, SAT_1107: 15.8})  # Saturday's better game doesn't count
    assert c.verdict == WRONG and c.detail.startswith("next game 10.2, lower")


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


def test_scratch_runs_write_where_log_path_points_not_the_real_log(tmp_path, monkeypatch):
    # 11/01: a scratch run set advice_log.LOG_PATH, but append's default path had
    # been fixed at import, so three test runs landed in the real advice log.
    scratch = tmp_path / "scratch" / "advice_log.json"
    monkeypatch.setattr(advice_log, "LOG_PATH", scratch)
    e = entry(slots=[row("C", "wemby", 22.0, "PASS", "wemby", 41.0)])
    del e["id"], e["notes"]
    assert advice_log.append(e) == 1
    advice_log.add_note(1, "scratch note")
    assert scratch.exists() and advice_log.load()[0]["notes"][0]["text"] == "scratch note"


def test_calls_follow_players_moved_by_a_swap():
    # Quickley locks in SG by swapping with White, so White now sits in PG.
    e = entry(slots=[row("PG", "white", 15.0, "PASS", "murray", 35.0),
                     row("SG", "quickley", 28.0, "LOCK")],
              locks=[lock("quickley", "PG", "SG", 28.0)], held=[lock("white", "SG", "G", 15.0)])
    held = next(c for c in calls_in(e) if c.kind == "HELD")
    assert held.slot == "PG"
