"""Where game logs come from: Sleeper first, NBA.com if Sleeper fails.

Both sources return the same box-score dicts, most recent game first, so the
rest of the app doesn't care which one answered.
"""

from __future__ import annotations

from . import nba_stats, sleeper_stats


class NoGameLogs(Exception):
    """Neither Sleeper nor NBA.com had this player's games."""


def season_games(sleeper_id: str, name: str, season: str, is_current: bool,
                 max_age: float | None = None) -> list[dict]:
    """A player's games for a season ("2026-27"), most recent first.

    `max_age` (seconds) is how old this season's cache may be; if it's not
    given, each source uses its own default.
    """
    age = {} if max_age is None else {"max_age": max_age}
    try:
        return sleeper_stats.game_logs(sleeper_id, season, is_current, **age)
    except sleeper_stats.SleeperStatsUnavailable as err:
        print(f"  (Sleeper stats failed for {name}: {err}; using NBA.com)")

    nba_id = nba_stats.find_nba_id(name)
    if nba_id is None:
        raise NoGameLogs("couldn't match the name to NBA.com "
                         "(add it to NAME_OVERRIDES in nba_stats.py)")
    try:
        return nba_stats.game_logs(nba_id, season, is_current, **age)
    except nba_stats.NBAStatsUnavailable as err:
        raise NoGameLogs("NBA.com refused the connection") from err
