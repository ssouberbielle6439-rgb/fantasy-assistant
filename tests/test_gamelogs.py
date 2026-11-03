import pytest

from fantasy_assistant import gamelogs, nba_stats, sleeper_stats

NBA_BOX = {"pts": 25, "game_date": "Oct 30, 2026", "matchup": "DEN vs. PHX"}


def _sleeper_down(*args, **kwargs):
    raise sleeper_stats.SleeperStatsUnavailable("couldn't reach Sleeper")


def _must_not_be_called(*args, **kwargs):
    raise AssertionError("NBA.com was asked although Sleeper answered")


def test_sleeper_answers_so_nba_com_is_never_asked(monkeypatch):
    # Sleeper is keyed by player id: no name matching, no NBA.com download.
    monkeypatch.setattr(sleeper_stats, "game_logs", lambda *a, **k: [{"pts": 39}])
    monkeypatch.setattr(nba_stats, "find_nba_id", _must_not_be_called)
    monkeypatch.setattr(nba_stats, "game_logs", _must_not_be_called)
    assert gamelogs.season_games("1747", "Jamal Murray", "2026-27", True) == [{"pts": 39}]


def test_sleeper_failure_falls_back_to_nba_com_with_a_note(monkeypatch, capsys):
    monkeypatch.setattr(sleeper_stats, "game_logs", _sleeper_down)
    monkeypatch.setattr(nba_stats, "find_nba_id", lambda name: 1627750)
    monkeypatch.setattr(nba_stats, "game_logs", lambda *a, **k: [NBA_BOX])

    assert gamelogs.season_games("1747", "Jamal Murray", "2026-27", True) == [NBA_BOX]
    note = capsys.readouterr().out
    assert "Sleeper stats failed for Jamal Murray" in note
    assert "using NBA.com" in note


def test_max_age_is_passed_to_whichever_source_answers(monkeypatch):
    seen = []
    monkeypatch.setattr(sleeper_stats, "game_logs", _sleeper_down)
    monkeypatch.setattr(nba_stats, "find_nba_id", lambda name: 1627750)
    monkeypatch.setattr(nba_stats, "game_logs",
                        lambda nba_id, season, is_current, **age: seen.append(age) or [])
    gamelogs.season_games("1747", "Jamal Murray", "2026-27", True, max_age=3600)
    assert seen == [{"max_age": 3600}]


def test_both_sources_failing_raises_no_game_logs(monkeypatch):
    def nba_down(*args, **kwargs):
        raise nba_stats.NBAStatsUnavailable("reset")

    monkeypatch.setattr(sleeper_stats, "game_logs", _sleeper_down)
    monkeypatch.setattr(nba_stats, "find_nba_id", lambda name: 1627750)
    monkeypatch.setattr(nba_stats, "game_logs", nba_down)
    with pytest.raises(gamelogs.NoGameLogs, match="NBA.com refused"):
        gamelogs.season_games("1747", "Jamal Murray", "2026-27", True)


def test_name_nba_com_doesnt_know_raises_no_game_logs(monkeypatch):
    monkeypatch.setattr(sleeper_stats, "game_logs", _sleeper_down)
    monkeypatch.setattr(nba_stats, "find_nba_id", lambda name: None)
    with pytest.raises(gamelogs.NoGameLogs, match="NAME_OVERRIDES"):
        gamelogs.season_games("9999", "Nobody Known", "2026-27", True)
