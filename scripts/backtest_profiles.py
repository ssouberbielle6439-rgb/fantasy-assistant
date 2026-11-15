"""How far off are player averages? Replays this season to find out.

For every player rostered in the league, and every game he has played this
season, it rebuilds his average from only the games he had played *before*
that game, then compares it to what he actually scored. It does that for the
old average (his last 25 games of either season) and for the weighted average
in profile.py with several starting weights for last season (see
profile.last_season_weight: last season counts as that many games on opening
night, fading to 0 by game 25).

Two numbers per method:
- bias: average of (forecast - actual). Positive means forecasts ran high.
- miss: average size of the miss, either direction (lower is better).

Run it from the project folder (needs .env; downloads into data/gamelogs/,
which takes a few minutes the first time):

    .venv\\Scripts\\python -m scripts.backtest_profiles

It isn't a test (pytest only looks in tests/), because it needs Sleeper and
the answer changes as the season goes on.

Results on 11/15 (weeks 1-4, 179 players), which is how LAST_SEASON_WEIGHT = 5
was chosen:

    All games  (1825 games)              His first 3 games of the season  (469)
      method            bias   miss        method            bias   miss
      old (last 25)    +0.58   6.31        old (last 25)    +1.38   6.50
      weight 3         +0.13   6.24        weight 3         +1.05   6.55
      weight 5         +0.23   6.22        weight 5         +1.18   6.51
      weight 8         +0.33   6.22        weight 8         +1.27   6.50
      weight 10        +0.38   6.23        weight 10        +1.30   6.49
      weight 15        +0.45   6.26        weight 15        +1.35   6.49

    After 3-9 games  (1088 games)        After 10+ games  (268 games)
      method            bias   miss        method            bias   miss
      old (last 25)    +0.50   6.28        old (last 25)    -0.46   6.08
      weight 3         -0.01   6.17        weight 3         -0.92   5.99
      weight 5         +0.10   6.16        weight 5         -0.86   5.98
      weight 8         +0.21   6.17        weight 8         -0.79   5.97
      weight 10        +0.26   6.19        weight 10        -0.75   5.96
      weight 15        +0.35   6.23        weight 15        -0.67   5.97

    Our roster only  (156 games)
      method            bias   miss
      old (last 25)    +0.46   6.33
      weight 3         +0.24   6.43
      weight 5         +0.29   6.38
      weight 8         +0.34   6.35
      weight 10        +0.37   6.33
      weight 15        +0.40   6.32

Findings that went with it:
- Every weighting beats the old average league-wide, but the gain is small:
  about a third of a point less bias and a tenth of a point smaller misses.
- Most of the bias is in a player's first 3 games of the season: players start
  slower than they finished last season, and no weighting can fix that with
  0-2 games to go on.
- 5 is the middle choice. It tied 8 for the smallest miss (6.22). 3 had less
  bias overall but runs lowest after 10 games (-0.92), and after 10 games
  every method forecasts low (players are scoring above their averages lately;
  worth watching as the season goes on). On our roster alone, 156 games is too
  few to choose by; the miss there is within 0.1 for every method.
- A fixed 5 that never fades did about as well (players with a last season:
  +0.34 bias, 6.24 miss), but it would keep counting last season about 17%
  forever, so the fading version was chosen.
- The swing checked out: 64% of games league-wide (68% on our roster) landed
  within one swing of the average; 68% is what a right swing gives.
- The forecast check in `review` (slots vs the plan's Exp) was -4.9 in week 2,
  +1.3 in week 3 (Wembanyama's 81.9 on 11/04 against an Exp of 45.8; -4.5
  without it) and +3.3 in week 4, 0.0 over all 17 slots. One slot misses Exp
  by about 11 points either way, so a 5-7 slot week swings 4-5 points by luck.
  Averages running about half a point high explain only a small part of it.
"""

from __future__ import annotations

import os
import time
from statistics import mean

from dotenv import load_dotenv

from fantasy_assistant import sleeper, sleeper_stats
from fantasy_assistant.profile import MIN_GAMES, RECENT_GAMES, weighted_average
from fantasy_assistant.scoring import fantasy_points

THIS_SEASON, LAST_SEASON = "2026-27", "2025-26"
STARTING_WEIGHTS = (3, 5, 8, 10, 15)


def old_average(this: list[float], last: list[float]) -> float:
    """The average before 11/15: his last 25 games, this season first."""
    return mean((this + last)[:RECENT_GAMES])


METHODS = {"old (last 25)": old_average,
           **{f"weight {w}": (lambda this, last, w=w: weighted_average(this, last, start=w))
              for w in STARTING_WEIGHTS}}


def season_points(pid: str, season: str, scoring: dict) -> list[float] | None:
    """His fantasy points per game played, most recent first (None if Sleeper fails)."""
    try:
        games = sleeper_stats.game_logs(pid, season, is_current=season == THIS_SEASON,
                                        max_age=6 * 3600)
    except sleeper_stats.SleeperStatsUnavailable:
        return None
    return [fantasy_points(b, scoring) for b in games]


def replay(this: list[float], last: list[float]) -> list[tuple[int, float, dict]]:
    """(games played before, actual score, {method: forecast}) for each game
    this season that the app would have had a profile for."""
    out = []
    for i, actual in enumerate(this):        # most recent first, so...
        before = this[i + 1:]                # ...the games after it in the list came first
        if len(before) + len(last) < MIN_GAMES:
            continue
        out.append((len(before), actual, {m: f(before, last) for m, f in METHODS.items()}))
    return out


def report(title: str, rows: list[tuple[int, float, dict]]) -> None:
    print(f"\n{title}  ({len(rows)} games)")
    print(f"  {'method':<15} {'bias':>6} {'miss':>6}")
    for m in METHODS:
        errors = [forecast[m] - actual for _, actual, forecast in rows]
        print(f"  {m:<15} {mean(errors):>+6.2f} {mean(abs(e) for e in errors):>6.2f}")


def main() -> None:
    load_dotenv()
    league_id = os.environ["SLEEPER_LEAGUE_ID"]
    scoring = sleeper.get_league(league_id)["scoring_settings"]
    rosters = sleeper.get_rosters(league_id)
    me = sleeper.get_user(os.environ["SLEEPER_USERNAME"])
    mine = set((sleeper.find_my_roster(rosters, me["user_id"]) or {}).get("players") or [])
    player_ids = sorted({pid for r in rosters for pid in (r.get("players") or [])})

    print(f"Replaying this season for {len(player_ids)} rostered players...")
    rows, my_rows = [], []
    for pid in player_ids:
        this = season_points(pid, THIS_SEASON, scoring)
        last = season_points(pid, LAST_SEASON, scoring)
        time.sleep(0.1)                      # be polite to Sleeper
        if this is None or last is None:
            continue
        player_rows = replay(this, last)
        rows += player_rows
        if pid in mine:
            my_rows += player_rows

    report("All games", rows)
    report("His first 3 games of the season", [r for r in rows if r[0] < 3])
    report("After 3-9 games", [r for r in rows if 3 <= r[0] < 10])
    report("After 10+ games", [r for r in rows if r[0] >= 10])
    report("Our roster only", my_rows)


if __name__ == "__main__":
    main()
