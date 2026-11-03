import json
from pathlib import Path

import pytest

from fantasy_assistant import sleeper_stats
from fantasy_assistant.scoring import fantasy_points
from fantasy_assistant.sleeper_stats import (SleeperStatsUnavailable, merge_games,
                                             merge_statuses, parse_player_games,
                                             played_games)

DATA = Path(__file__).parent / "data"

# Santiago's league scoring as Sleeper returned it on 11/03 (after the 11/01
# bonus change: double-double 2, triple-double 4, +3 for 15+ assists).
LEAGUE = {
    "ast": 1.0, "blk": 2.0, "bonus_ast_15p": 3.0, "bonus_pt_40p": 2.0, "bonus_pt_50p": 3.0,
    "bonus_reb_20p": 3.0, "dd": 2.0, "ff": -2.0, "fgm": 0.1, "ftmi": -0.5, "oreb": 0.2,
    "pf": -0.5, "pts": 0.5, "reb": 1.0, "stl": 2.0, "td": 4.0, "tf": -2.0, "to": -1.0,
    "tpm": 0.5,
}

MURRAY, CLINGAN, SMITH_JR = "1747", "2728", "2445"


@pytest.fixture
def fake_sleeper(monkeypatch):
    """Answers Sleeper requests with the real week-2 responses saved in tests/data/
    (trimmed to week 2, without per-quarter stats). Returns the list of requests made."""
    requests_made = []

    def get_json(path):
        requests_made.append(path)
        if path.startswith("/schedule/"):
            name = "sleeper_schedule_2026.json"
        else:
            player_id = path.split("/")[-1].split("?")[0]
            name = f"sleeper_player_{player_id}_2026.json"
        return json.loads((DATA / name).read_text(encoding="utf-8"))

    monkeypatch.setattr(sleeper_stats, "_get_json", get_json)
    return requests_made


def points_on(player_id: str, game_date: str) -> float:
    games = sleeper_stats.game_logs(player_id, "2026-27", is_current=True)
    [box] = [b for b in games if b["game_date"] == game_date]
    return fantasy_points(box, LEAGUE)


# ---------- The three games confirmed against Sleeper on 11/02 ----------

def test_clingan_10_29_vs_mem_is_22_8_with_his_technical(fake_sleeper):
    # NBA.com's game log has no technical fouls, so we used to score this 24.8.
    assert points_on(CLINGAN, "Oct 29, 2026") == 22.8


def test_smith_jr_10_31_vs_okc_is_31_7_with_his_technical(fake_sleeper):
    # NBA.com: 33.7, two points high for the same reason.
    assert points_on(SMITH_JR, "Oct 31, 2026") == 31.7


def test_murray_10_30_vs_phx_is_34_4(fake_sleeper):
    assert points_on(MURRAY, "Oct 30, 2026") == 34.4


def test_games_come_back_most_recent_first_in_nba_com_format(fake_sleeper):
    games = sleeper_stats.game_logs(CLINGAN, "2026-27", is_current=True)
    assert [g["game_date"] for g in games] == ["Oct 31, 2026", "Oct 29, 2026", "Oct 27, 2026"]
    assert games[0]["matchup"] == "POR @ UTA"
    assert games[1]["matchup"] == "POR vs. MEM"


# ---------- Only finished games count ----------

def _game(game_id, day, stats, updated_at=1):
    return {"game_id": game_id, "date": day, "team": "DEN", "opponent": "POR",
            "is_away_team": False, "updated_at": updated_at, "stats": stats}


def test_game_still_in_progress_is_not_counted():
    # 11/02, 7:50pm: Sleeper showed live partial lines (Bridges 2 pts in the
    # 1st quarter). A partial line must not count as a game.
    games = {"g1": _game("g1", "2026-10-30", {"pts": 39.0}),
             "g2": _game("g2", "2026-11-02", {"pts": 2.0})}
    statuses = {"g1": "complete", "g2": "in_game"}
    assert [b["game_id"] for b in played_games(games, statuses)] == ["g1"]


def test_empty_stats_mean_did_not_play_only_once_the_game_is_complete():
    # Mid-game, a player who hasn't checked in yet has {} too. Either way the
    # game isn't one he played, so it never shows up as a 0.
    games = {"dnp": _game("dnp", "2026-10-31", {}),
             "live": _game("live", "2026-11-02", {})}
    assert played_games(games, {"dnp": "complete", "live": "in_game"}) == []


def test_did_not_play_games_are_not_counted_as_games_played(monkeypatch):
    # Last season's response lists the games he sat out with empty stats (7 of
    # Murray's 82 in 2025-26). profiles uses len(last season) as games played.
    response = {"1": [_game("a", "2025-10-23", {"pts": 25.0}), _game("b", "2025-10-25", {})]}
    monkeypatch.setattr(sleeper_stats, "_get_json", lambda path: (
        [{"game_id": "a", "status": "complete"}, {"game_id": "b", "status": "complete"}]
        if path.startswith("/schedule/") else response))
    games = sleeper_stats.game_logs(MURRAY, "2025-26", is_current=False)
    assert [g["game_id"] for g in games] == ["a"]


def test_finished_game_stays_finished_when_a_stale_schedule_says_in_game():
    # 11/02, 10:09pm: a 13-minute-old copy showed three finished games as in_game.
    assert merge_statuses({"g1": "complete"}, {"g1": "in_game"}) == {"g1": "complete"}
    assert merge_statuses({"g1": "pre_game"}, {"g1": "in_game"}) == {"g1": "in_game"}


# ---------- Newest version of each game's line wins ----------

def test_stale_copy_does_not_overwrite_a_newer_line():
    saved = {"g1": _game("g1", "2026-10-29", {"pts": 11.0, "tf": 1.0}, updated_at=200)}
    stale = [_game("g1", "2026-10-29", {"pts": 9.0}, updated_at=100)]
    assert merge_games(saved, stale)["g1"]["stats"] == {"pts": 11.0, "tf": 1.0}


def test_stat_correction_days_later_replaces_the_older_line():
    saved = {"g1": _game("g1", "2026-10-29", {"pts": 11.0}, updated_at=100)}
    corrected = [_game("g1", "2026-10-29", {"pts": 11.0, "ast": 2.0}, updated_at=200)]
    assert merge_games(saved, corrected)["g1"]["stats"] == {"pts": 11.0, "ast": 2.0}


# ---------- Caching ----------

def test_cache_is_used_until_it_gets_old(fake_sleeper):
    sleeper_stats.game_logs(MURRAY, "2026-27", is_current=True)
    sleeper_stats.game_logs(MURRAY, "2026-27", is_current=True)
    assert len(fake_sleeper) == 2  # schedule + Murray, once each

    sleeper_stats.game_logs(MURRAY, "2026-27", is_current=True, max_age=0)
    assert len(fake_sleeper) == 4  # both refreshed


def test_cached_games_are_merged_with_fresh_ones(fake_sleeper, monkeypatch):
    # A refresh that comes back without a game we already have keeps that game.
    sleeper_stats.game_logs(MURRAY, "2026-27", is_current=True)
    schedule = json.loads((DATA / "sleeper_schedule_2026.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(sleeper_stats, "_get_json", lambda path: (
        schedule if path.startswith("/schedule/") else {"1": [], "2": []}))
    games = sleeper_stats.game_logs(MURRAY, "2026-27", is_current=True, max_age=0)
    assert len(games) == 3


# ---------- Unexpected responses ----------

@pytest.mark.parametrize("response", [
    [],                                   # a list instead of {week: games}
    {"1": {"oops": 1}},                   # a week that isn't a list
    {"1": [{"date": "2026-10-29"}]},      # a game with no game_id
    {"1": [{"game_id": "g", "date": "2026-10-29", "stats": [1, 2]}]},
])
def test_unexpected_response_raises_so_we_fall_back(response):
    with pytest.raises(SleeperStatsUnavailable):
        parse_player_games(response)


def test_season_with_no_games_is_empty():
    # What Sleeper sends for a rookie's previous season.
    assert parse_player_games({"1": [], "2": []}) == []
