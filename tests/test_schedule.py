from datetime import date, datetime

from fantasy_assistant.schedule import (LOCK_BEFORE_TIPOFF, Game, fantasy_week, games_to_come,
                                        nba_team, parse_games, still_to_come, team_games,
                                        tipoff_note)

ROWS = [
    {"gameId": "1", "gameDateEst": "2026-10-26T00:00:00Z",
     "homeTeam_teamTricode": "DEN", "awayTeam_teamTricode": "GSW"},
    {"gameId": "1", "gameDateEst": "2026-10-26T00:00:00Z",  # duplicate row
     "homeTeam_teamTricode": "DEN", "awayTeam_teamTricode": "GSW"},
    {"gameId": "2", "gameDateEst": "2026-10-28T00:00:00Z",
     "homeTeam_teamTricode": "NOP", "awayTeam_teamTricode": "DEN"},
    {"gameId": "3", "gameDateEst": "2026-11-02T00:00:00Z",
     "homeTeam_teamTricode": "DEN", "awayTeam_teamTricode": "PHX"},
]


def test_duplicates_removed():
    assert len(parse_games(ROWS)) == 3


def test_week_is_monday_to_sunday():
    week = fantasy_week(date(2026, 10, 27))  # a Tuesday
    assert week[0] == date(2026, 10, 26)
    assert week[-1] == date(2026, 11, 1)
    assert len(week) == 7


def test_team_games_in_week():
    games = parse_games(ROWS)
    week = fantasy_week(date(2026, 10, 27))
    den = team_games(games, "DEN", week)
    assert sorted(den) == [date(2026, 10, 26), date(2026, 10, 28)]  # Nov 2 is next week
    assert den[date(2026, 10, 26)].opponent_of("DEN") == "vs GSW"
    assert den[date(2026, 10, 28)].opponent_of("DEN") == "@NOP"


def test_team_aliases():
    assert nba_team("NO") == "NOP"
    assert nba_team("DEN") == "DEN"
    assert nba_team(None) is None


# ---------- Has today's game tipped off? ----------

SUNDAY = date(2026, 11, 1)
TOR_AT_LAL = Game("0022600157", SUNDAY, "LAL", "TOR")  # Quickley's game, 9pm Eastern


def test_sunday_11_01_quickley_9pm_game_still_to_come_at_7_22pm():
    # advise at 7:22pm treated Quickley's 9pm game as already played (the old
    # fixed 7pm cutoff), so the plan thought he had no games left.
    at_7_22pm = datetime(2026, 11, 1, 19, 22)
    today_statuses = {"TOR": "pre_game", "LAL": "pre_game"}
    to_come = games_to_come({SUNDAY: TOR_AT_LAL}, "TOR", at_7_22pm, today_statuses)
    assert to_come == {SUNDAY: TOR_AT_LAL}
    assert tipoff_note(to_come, SUNDAY) == LOCK_BEFORE_TIPOFF


def test_game_today_has_started_once_it_is_in_game_or_complete():
    at_9_05pm = datetime(2026, 11, 1, 21, 5)
    for status in ("in_game", "complete"):
        to_come = games_to_come({SUNDAY: TOR_AT_LAL}, "TOR", at_9_05pm, {"TOR": status})
        assert to_come == {}
        assert tipoff_note(to_come, SUNDAY) == ""


def test_early_tip_off_has_started_even_before_7pm():
    # A 3:30pm Sunday game is in_game at 4pm; the old cutoff still counted it.
    assert not still_to_come(SUNDAY, datetime(2026, 11, 1, 16, 0), "in_game")


def test_without_sleepers_schedule_today_falls_back_to_7pm():
    assert still_to_come(SUNDAY, datetime(2026, 11, 1, 18, 59), None)
    assert not still_to_come(SUNDAY, datetime(2026, 11, 1, 19, 0), None)
    # Sleeper unreachable (None), or the game not in its schedule (no entry for TOR).
    assert games_to_come({SUNDAY: TOR_AT_LAL}, "TOR", datetime(2026, 11, 1, 19, 22), None) == {}
    assert games_to_come({SUNDAY: TOR_AT_LAL}, "TOR", datetime(2026, 11, 1, 19, 22), {}) == {}


def test_other_days_ignore_the_status_and_the_clock():
    late_saturday = datetime(2026, 10, 31, 23, 30)
    assert still_to_come(SUNDAY, late_saturday, None)
    assert not still_to_come(date(2026, 10, 31), datetime(2026, 11, 1, 9, 0), None)


def test_postponed_game_today_is_not_still_to_come():
    assert not still_to_come(SUNDAY, datetime(2026, 11, 1, 12, 0), "postponed")


def test_tipoff_note_only_when_the_next_game_is_today():
    monday_game = Game("x", date(2026, 11, 2), "TOR", "BOS")
    assert tipoff_note({date(2026, 11, 2): monday_game}, SUNDAY) == ""
