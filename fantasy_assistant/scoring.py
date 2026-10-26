"""Turn a box-score line into fantasy points using a league's own scoring.

Sleeper hands us scoring settings as a dict like {"pts": 0.5, "reb": 1.0,
...}. This module applies those weights to one game's stats, so the app
always scores players exactly the way YOUR league does.
"""

from __future__ import annotations

# Sleeper scoring key -> how to read that stat from a box-score dict.
# Box-score keys follow nba_api naming (PTS, REB, AST, ...), lowercased.
_COUNTING_STATS = {
    "pts": "pts",
    "reb": "reb",
    "ast": "ast",
    "stl": "stl",
    "blk": "blk",
    "to": "tov",
    "pf": "pf",
    "fgm": "fgm",
    "tpm": "fg3m",
    "oreb": "oreb",
}

# Categories that count toward a double-double / triple-double.
_DOUBLE_CATS = ("pts", "reb", "ast", "stl", "blk")


def fantasy_points(box: dict, scoring: dict) -> float:
    """Fantasy points for one game.

    `box` is a stat line, e.g. {"pts": 30, "reb": 10, "fta": 8, "ftm": 6, ...}.
    `scoring` is the league's scoring_settings from Sleeper.
    Missing stats count as 0; scoring keys we don't recognize are ignored.
    """
    total = 0.0

    for key, stat in _COUNTING_STATS.items():
        total += scoring.get(key, 0) * box.get(stat, 0)

    # Free throws missed = attempted - made.
    ft_missed = box.get("fta", 0) - box.get("ftm", 0)
    total += scoring.get("ftmi", 0) * ft_missed

    # Technical and flagrant fouls.
    total += scoring.get("tf", 0) * box.get("tf", 0)
    total += scoring.get("ff", 0) * box.get("ff", 0)

    # Double-double / triple-double.
    # ASSUMPTION to verify against Sleeper: a triple-double earns BOTH bonuses.
    tens = sum(1 for c in _DOUBLE_CATS if box.get(c, 0) >= 10)
    if tens >= 2:
        total += scoring.get("dd", 0)
    if tens >= 3:
        total += scoring.get("td", 0)

    # Big-game bonuses.
    # ASSUMPTION to verify: a 50-point game earns both the 40+ and 50+ bonus.
    pts = box.get("pts", 0)
    if pts >= 40:
        total += scoring.get("bonus_pt_40p", 0)
    if pts >= 50:
        total += scoring.get("bonus_pt_50p", 0)
    if box.get("reb", 0) >= 20:
        total += scoring.get("bonus_reb_20p", 0)

    return round(total, 2)
