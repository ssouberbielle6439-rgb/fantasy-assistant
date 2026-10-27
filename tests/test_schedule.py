from datetime import date

from fantasy_assistant.schedule import fantasy_week, nba_team, parse_games, team_games

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
