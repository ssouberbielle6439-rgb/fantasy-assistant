"""Slot scores from Sleeper's box scores, checked against Sunday night 11/01,
when Sleeper's matchup numbers flipped between games every minute."""

import json
from datetime import date, timedelta
from pathlib import Path

from fantasy_assistant import locks
from fantasy_assistant.lineup import Slot
from fantasy_assistant.locks import parse_answer, typed_scores
from fantasy_assistant.slot_scores import (WeekGame, cross_check, rescore_locks,
                                           score_open_slots, settle_new_lock, week_games)
from fantasy_assistant.sleeper_stats import merge_games, parse_player_games, parse_statuses
from test_sleeper_stats import CLINGAN, LEAGUE

DATA = Path(__file__).parent / "data"
MOBLEY = "2259"
WEEK_2 = [date(2026, 10, 26) + timedelta(days=i) for i in range(7)]  # Mon 10/26 - Sun 11/01

# Mobley's week 2: Mon vs MIN 26.9, Wed vs WAS 27.9, Thu @ ATL 17.6, Sat vs MIL 25.3.
MOBLEY_VS_WAS = "1393723776029425664"


def _load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def raw_games(player_id):
    return merge_games({}, parse_player_games(_load(f"sleeper_player_{player_id}_2026.json")))


def statuses():
    return parse_statuses(_load("sleeper_schedule_2026.json"))


def sunday_11_01(*player_ids):
    """Each player's week-2 games as they stood on Sunday night 11/01 (saved samples)."""
    return {pid: week_games(raw_games(pid), statuses(), WEEK_2, LEAGUE) for pid in player_ids}


def test_week_games_score_like_sleeper_oldest_first():
    games = sunday_11_01(MOBLEY)[MOBLEY]
    assert [(g.day.day, g.points) for g in games] == [(26, 26.9), (28, 27.9), (29, 17.6),
                                                       (31, 25.3)]
    assert all(g.final for g in games)


# ---------- The three cases from Sunday 11/01 ----------

def test_sunday_11_01_mobley_locked_f_keeps_27_9_though_his_latest_game_was_25_3():
    # Sleeper's matchup flipped Mobley's locked F between 25.3 (his latest game)
    # and 27.9 (the game he was locked on).
    saved = {"F": {"player_id": MOBLEY, "score": 27.9, "game_id": MOBLEY_VS_WAS}}
    slots = [Slot("F", "F", MOBLEY, 25.3)]
    by_player = sunday_11_01(MOBLEY)
    assert rescore_locks(saved, by_player) == []          # no correction: still 27.9
    score_open_slots(slots, by_player)
    assert slots[0].score == 25.3                         # what the open-slot rule would say...
    locks.lock_slots(slots, ["F"], saved, kept=True)
    assert (slots[0].score, slots[0].game_id) == (27.9, MOBLEY_VS_WAS)   # ...but the lock wins


def test_sunday_11_01_clingan_open_util2_ends_at_11_4_not_sleepers_22_8():
    # Sleeper briefly showed 22.8, his Thursday game vs MEM; his last game was
    # Saturday @ UTA, 11.4.
    slots = [Slot("UTIL2", "UTIL", CLINGAN, 22.8)]
    by_player = sunday_11_01(CLINGAN)
    score_open_slots(slots, by_player)
    assert slots[0].score == 11.4
    [note] = cross_check(slots, {"UTIL2": 22.8}, by_player)
    assert "our score 11.4, Sleeper's 22.8. Using 11.4" in note


def test_latest_game_a_dnp_scores_0_even_after_a_30_point_game():
    # Rule 3: if he sits out his next game, the slot is 0. No going back.
    games = {"g1": {"game_id": "g1", "date": "2026-10-27", "team": "DEN", "opponent": "POR",
                    "is_away_team": False, "updated_at": 1, "stats": {"pts": 40.0, "reb": 10.0}},
             "g2": {"game_id": "g2", "date": "2026-10-30", "team": "DEN", "opponent": "PHX",
                    "is_away_team": True, "updated_at": 1, "stats": {}}}
    by_player = {"murray": week_games(games, {"g1": "complete", "g2": "complete"}, WEEK_2, LEAGUE)}
    slots = [Slot("PG", "PG", "murray", 32.0)]
    score_open_slots(slots, by_player)
    assert (slots[0].score, slots[0].game_id) == (0.0, "g2")


# ---------- Open slots ----------

def test_no_game_yet_means_an_empty_slot():
    slots = [Slot("C", "C", "clingan", 0.0)]
    score_open_slots(slots, {"clingan": []})
    assert (slots[0].score, slots[0].game_id) == (0.0, None)


def test_live_game_doesnt_replace_the_score_until_its_complete():
    live = {gid: ("in_game" if gid == "1393723794631163904" else s)   # Sat vs MIL
            for gid, s in statuses().items()}
    by_player = {MOBLEY: week_games(raw_games(MOBLEY), live, WEEK_2, LEAGUE)}
    slots = [Slot("F", "F", MOBLEY, 12.0)]
    score_open_slots(slots, by_player)
    assert slots[0].score == 17.6                          # Thursday's game still holds
    assert "isn't final yet" in cross_check(slots, {"F": 12.0}, by_player)[0]


def test_stats_unavailable_keeps_sleepers_number():
    slots = [Slot("F", "F", MOBLEY, 25.3)]
    score_open_slots(slots, {MOBLEY: None})
    assert slots[0].score == 25.3
    assert settle_new_lock(slots[0], None, 25.3) is None


# ---------- Locked slots ----------

def test_stat_correction_to_the_locked_game_flows_through():
    # Corrections land days later with a newer updated_at.
    games = raw_games(MOBLEY)
    fixed = dict(games[MOBLEY_VS_WAS], updated_at=games[MOBLEY_VS_WAS]["updated_at"] + 1)
    fixed["stats"] = dict(fixed["stats"], ast=fixed["stats"]["ast"] + 1)
    games = merge_games(games, [fixed])
    by_player = {MOBLEY: week_games(games, statuses(), WEEK_2, LEAGUE)}
    saved = {"F": {"player_id": MOBLEY, "score": 27.9, "game_id": MOBLEY_VS_WAS}}
    [line] = rescore_locks(saved, by_player, name=lambda pid: "E. Mobley")
    assert saved["F"]["score"] == 28.9
    assert line.startswith("F (E. Mobley) re-scored 27.9 -> 28.9")


def test_old_lock_without_a_game_keeps_its_saved_score(tmp_path):
    (tmp_path / "locks_2026-10-26.json").write_text(
        '{"F": {"player_id": "2259", "score": 27.9}}', encoding="utf-8")
    saved = locks.load(WEEK_2[0])
    assert rescore_locks(saved, sunday_11_01(MOBLEY)) == []
    assert saved["F"] == {"player_id": MOBLEY, "score": 27.9, "game_id": None}


def test_typed_score_beats_the_saved_game_and_is_never_rescored():
    saved = {"F": {"player_id": MOBLEY, "score": 27.9, "game_id": MOBLEY_VS_WAS}}
    slots = [Slot("F", "F", MOBLEY, 25.3)]
    answer = "F=30.0"
    assert locks.lock_slots(slots, parse_answer(answer, ["F"], ["F"]), saved,
                            typed_scores(answer, ["F"])) == []
    assert locks.from_slots(slots)["F"] == {"player_id": MOBLEY, "score": 30.0, "game_id": None}


# ---------- New locks: the game whose score Sleeper shows ----------

def new_mobley_lock(sleeper_score):
    slots = [Slot("F", "F", MOBLEY, sleeper_score)]
    by_player = sunday_11_01(MOBLEY)
    score_open_slots(slots, by_player)
    [new] = locks.lock_slots(slots, ["F"], {})
    note = settle_new_lock(new, by_player[MOBLEY], sleeper_score, name=lambda pid: "E. Mobley")
    return slots[0], note


def test_new_lock_told_to_the_app_later_saves_the_game_sleeper_shows():
    # Locked Mobley's Wednesday 27.9 in Sleeper, told the app on Sunday after
    # his Thursday and Saturday games. His latest game (25.3) would be wrong.
    slot, note = new_mobley_lock(27.9)
    assert (slot.score, slot.game_id, note) == (27.9, MOBLEY_VS_WAS, None)
    assert locks.from_slots([slot])["F"]["game_id"] == MOBLEY_VS_WAS


def test_new_lock_right_after_the_final_while_sleeper_still_says_in_game():
    # Sleeper's schedule can show in_game for a few minutes after the buzzer.
    # The open-slot rule would still say Thursday's 17.6.
    live = {gid: ("in_game" if gid == "1393723794631163904" else s)
            for gid, s in statuses().items()}
    games = week_games(raw_games(MOBLEY), live, WEEK_2, LEAGUE)
    slot = Slot("F", "F", MOBLEY, 17.6, locked=True)
    assert settle_new_lock(slot, games, 25.3) is None
    assert (slot.score, slot.game_id) == (25.3, "1393723794631163904")


def test_new_lock_matching_no_game_takes_his_latest_with_a_note():
    slot, note = new_mobley_lock(22.0)          # a stale number Sleeper never had for him
    assert (slot.score, slot.game_id) == (25.3, "1393723794631163904")
    assert "none of his games this week match Sleeper's 22.0" in note
    assert "type F=score" in note


def test_new_lock_with_no_game_yet_asks_to_check():
    slot = Slot("F", "F", MOBLEY, 0.0, locked=True)
    assert "no game this week yet" in settle_new_lock(slot, [], 0.0)


def test_lock_on_a_dnp_matches_sleepers_0():
    games = [WeekGame("g1", date(2026, 10, 27), True, 31.0),
             WeekGame("g2", date(2026, 10, 30), True, 0.0)]
    slot = Slot("PG", "PG", "murray", 0.0, locked=True)
    assert settle_new_lock(slot, games, 0.0) is None
    assert (slot.score, slot.game_id) == (0.0, "g2")
