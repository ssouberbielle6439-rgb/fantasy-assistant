"""Chance to play, from a player's Sleeper injury status.

Starting values come from Santiago's experience with Sleeper statuses.
"Out" only covers the NEXT game, not the whole week, so each status has
one chance for the next game and another for games later in the week.
"""

from __future__ import annotations

# status: (next game, later games this week)
PLAY_CHANCE = {
    None:            (0.97, 0.97),  # regular; not 100%, surprise rest nights happen
    "Probable":      (0.90, 0.97),
    "Questionable":  (0.65, 0.85),
    "GTD":           (0.65, 0.85),  # game-time decision: most likely 50-75%
    "DTD":           (0.45, 0.70),  # day-to-day: around 50% or below
    "Doubtful":      (0.15, 0.50),
    "Out":           (0.00, 0.45),  # out for the next game, then DTD-level
    "Sus":           (0.00, 0.00),  # suspension length unknown; assume the week
}


def _key(status: str | None) -> str | None:
    if not status:
        return None
    s = status.strip().lower()
    for key in PLAY_CHANCE:
        if key and key.lower() == s:
            return key
    return {"game time decision": "GTD", "day to day": "DTD", "q": "Questionable",
            "d": "Doubtful", "o": "Out", "p": "Probable", "suspended": "Sus"}.get(s, "GTD")


def play_chances(status: str | None, n_games: int) -> list[float]:
    """Chance to play in each of his next `n_games` games."""
    next_game, later = PLAY_CHANCE[_key(status)]
    return [next_game] + [later] * (n_games - 1) if n_games else []
