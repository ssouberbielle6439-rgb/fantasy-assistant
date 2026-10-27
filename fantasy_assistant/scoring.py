"""Turn a box-score line into fantasy points using a league's own scoring.

Sleeper hands us scoring settings as a dict like {"pts": 0.5, "reb": 1.0,
...}. This module applies those weights to one game's stats, so the app
always scores players exactly the way YOUR league does.
"""

from __future__ import annotations

import re

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

# Big-game bonuses follow a pattern: bonus_<stat>_<N>p, e.g. bonus_pt_40p
# (40+ points) or bonus_ast_15p (15+ assists). Any bonus in this pattern works
# automatically, so new ones a commissioner adds need no code changes.
_BONUS_PATTERN = re.compile(r"^bonus_([a-z]+)_(\d+)p$")
_BONUS_STATS = {"pt": "pts", "pts": "pts", "reb": "reb", "ast": "ast",
                "stl": "stl", "blk": "blk", "tpm": "fg3m"}

_OTHER_KEYS = {"ftmi", "tf", "ff", "dd", "td"}


def unknown_scoring_keys(scoring: dict) -> list[str]:
    """Scoring settings this module doesn't know how to apply."""
    unknown = []
    for key, value in scoring.items():
        if not value or key in _COUNTING_STATS or key in _OTHER_KEYS:
            continue
        match = _BONUS_PATTERN.match(key)
        if match and match.group(1) in _BONUS_STATS:
            continue
        unknown.append(key)
    return sorted(unknown)


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
    # Confirmed in Sleeper: a triple-double earns BOTH bonuses (they stack).
    tens = sum(1 for c in _DOUBLE_CATS if box.get(c, 0) >= 10)
    if tens >= 2:
        total += scoring.get("dd", 0)
    if tens >= 3:
        total += scoring.get("td", 0)

    # Big-game bonuses (40+ points, 20+ rebounds, 15+ assists, ...).
    # Confirmed in Sleeper: a 50-point game earns both the 40+ and 50+ bonus (they stack).
    for key, value in scoring.items():
        match = _BONUS_PATTERN.match(key)
        if match and match.group(1) in _BONUS_STATS:
            if box.get(_BONUS_STATS[match.group(1)], 0) >= int(match.group(2)):
                total += value

    return round(total, 2)
