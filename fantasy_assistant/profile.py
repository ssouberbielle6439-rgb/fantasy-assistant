"""Turn a player's game logs into the numbers the lock-in engine needs."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, stdev

from .lockin import GameForecast
from .scoring import fantasy_points

RECENT_GAMES = 25      # how many recent games to average over
MIN_GAMES = 3          # fewer than this and we don't trust the numbers
MIN_SWING = 4.0        # never assume a player is perfectly consistent
SEASON_GAMES = 82
DEFAULT_PLAY_PROB = 0.90  # for players with no previous season (rookies)

# On opening night last season's average counts as this many games. Its weight
# shrinks with every game he plays this season and is gone by game 25
# (RECENT_GAMES). Chosen by replaying this season's games in
# scripts/backtest_profiles.py (11/15); rerun it mid-season to check.
LAST_SEASON_WEIGHT = 5


@dataclass(frozen=True)
class PlayerProfile:
    name: str
    games_used: int
    average: float     # expected fantasy points when he plays
    swing: float       # standard deviation of his fantasy points
    play_prob: float   # chance he plays in any given game

    def forecast(self) -> GameForecast:
        return GameForecast(mean=self.average, sd=self.swing, play_prob=self.play_prob)


def estimate_play_prob(last_season_games: int | None) -> float:
    """Simple v1 estimate: share of last season's games he played.

    Limitation: a player who missed time with one long injury looks less
    reliable than he is. Improving this is on the roadmap.
    """
    if not last_season_games:
        return DEFAULT_PLAY_PROB
    return min(0.98, max(0.5, last_season_games / SEASON_GAMES))


def last_season_weight(games_this_season: int, start: float = LAST_SEASON_WEIGHT) -> float:
    """How many games' worth last season's average counts for.

    `start` games' worth on opening night, shrinking in a straight line to 0
    once he has RECENT_GAMES games this season:
    5 -> 4.4 after 3 games, 3 after 10, 1 after 20, 0 from 25 on.
    """
    return max(0.0, start * (1 - games_this_season / RECENT_GAMES))


def weighted_average(this_season: list[float], last_season: list[float],
                     start: float = LAST_SEASON_WEIGHT) -> float:
    """Average fantasy points, leaning more on this season as it goes on.

    Both lists are most recent first. This season's last 25 games each count
    once; last season's average (over his last 25 games of it) is added in as
    if it were `last_season_weight` more games:

        (sum of this season's points + weight * last season's average)
        / (games this season + weight)

    Example: 10 games this season averaging 20, last season averaged 30, so
    the weight is 3: (200 + 3 * 30) / (10 + 3) = 22.3.

    Before 11/15 the average was simply his last 25 games of either season,
    so after 10 games this season it still leaned 60% on last season, and
    forecasts ran high early in the season (see scripts/backtest_profiles.py).
    """
    this_season = this_season[:RECENT_GAMES]
    if not last_season:                      # rookies: this season is all we have
        return mean(this_season)
    weight = last_season_weight(len(this_season), start)
    last_average = mean(last_season[:RECENT_GAMES])
    return (sum(this_season) + weight * last_average) / (len(this_season) + weight)


def build_profile(name: str, this_season: list[dict], last_season: list[dict],
                  scoring: dict) -> PlayerProfile | None:
    """Profile from his box scores (each season most recent first).

    The average weights this season more as it goes on (weighted_average).
    The swing is still the spread of his last 25 games of either season: it
    checked out on this season's games (about 68% land within one swing of
    the average, as they should), so it didn't need changing.
    """
    this_points = [fantasy_points(b, scoring) for b in this_season]
    last_points = [fantasy_points(b, scoring) for b in last_season]
    recent = (this_points + last_points)[:RECENT_GAMES]
    if len(recent) < MIN_GAMES:
        return None
    return PlayerProfile(
        name=name,
        games_used=len(recent),
        average=round(weighted_average(this_points, last_points), 1),
        swing=round(max(MIN_SWING, stdev(recent)), 1),
        play_prob=round(estimate_play_prob(len(last_season)), 2),
    )
