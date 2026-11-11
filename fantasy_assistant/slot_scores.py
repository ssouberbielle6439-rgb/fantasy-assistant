"""Each lineup slot's score, worked out from Sleeper's per-game stats.

Sleeper's matchup call shows a score for every slot, but on Sunday 11/01 it
couldn't be trusted: it flipped between a player's games every minute, for
locked slots (Mobley's F showed 25.3 or 27.9) and open ones (Clingan's UTIL2
showed 22.8, his Thursday game, though his last game was Saturday's 11.4).
So we take only the lineup (who is in each slot) from the matchup, and score
the slots ourselves:

  * Open slot: the player's latest complete game this fantasy week. If he
    sat that game out, the slot is 0 (rule 3). No game yet: empty (0).
  * Locked slot: the game saved with the lock, re-scored every run, so
    Sleeper's stat corrections (which land days later) come through.

What we can't see is when a player moved into a slot (rule 4: benching a
player clears his game). When our score and Sleeper's differ, advise prints
both and uses ours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .scoring import fantasy_points
from .sleeper_stats import to_box

SAME_SCORE = 0.05  # scores closer than this count as the same (Sleeper shows one decimal)

# Schedule statuses of a game that has tipped off.
STARTED = ("in_game", "complete")


@dataclass(frozen=True)
class WeekGame:
    """One of a player's games this week that has started."""
    game_id: str
    day: date
    final: bool      # Sleeper's schedule says complete
    points: float    # 0 if he sat out (or, mid-game, hasn't checked in yet)


def week_games(games: dict[str, dict], statuses: dict[str, str], days: list[date],
               scoring: dict) -> list[WeekGame]:
    """A player's games this fantasy week that have started, oldest first.

    `games` and `statuses` come from sleeper_stats.games_and_statuses. Games
    not played yet (or postponed) are left out.
    """
    week = []
    for g in games.values():
        day = date.fromisoformat(g["date"])
        status = statuses.get(g["game_id"])
        if day not in days or status not in STARTED:
            continue
        points = fantasy_points(to_box(g), scoring) if g.get("stats") else 0.0
        week.append(WeekGame(g["game_id"], day, status == "complete", points))
    week.sort(key=lambda g: g.day)
    return week


def latest_final(games: list[WeekGame]) -> WeekGame | None:
    """The game an open slot holds: his latest complete game (a live one doesn't count yet)."""
    final = [g for g in games if g.final]
    return final[-1] if final else None


def score_open_slots(slots, by_player: dict[str, list[WeekGame] | None]) -> None:
    """Set each slot's score and game from its player's latest complete game.

    A player whose stats couldn't be read (None) keeps Sleeper's number.
    Locked slots get the same treatment here; lock_slots then puts their
    saved games back.
    """
    for s in slots:
        if not s.player_id or by_player.get(s.player_id) is None:
            continue
        game = latest_final(by_player[s.player_id])
        s.score = game.points if game else 0.0
        s.game_id = game.game_id if game else None


def rescore_locks(saved: dict[str, dict], by_player: dict[str, list[WeekGame] | None],
                  name=str) -> list[str]:
    """Re-score each saved lock from its saved game, in place, so stat corrections
    come through. Returns a line for each score that changed.

    Locks without a game (older files, typed scores) keep their saved score.
    """
    lines = []
    for label, lock in saved.items():
        game_id = lock.get("game_id")
        games = by_player.get(lock["player_id"]) or []
        game = next((g for g in games if g.game_id == game_id), None) if game_id else None
        if game is None:
            continue
        old = lock["score"]
        lock["score"] = game.points
        if old is not None and abs(old - game.points) >= SAME_SCORE:
            lines.append(f"{label} ({name(lock['player_id'])}) re-scored {old:.1f} -> "
                         f"{game.points:.1f}: Sleeper corrected his {game.day:%a %m/%d} stats.")
    return lines


def cross_check(slots, sleeper_points: dict[str, float],
                by_player: dict[str, list[WeekGame] | None], skip=(), name=str) -> list[str]:
    """Slots where our score and Sleeper's slot number differ, in words.

    Sleeper's number can be a stale game, but it can also be right where we
    can't see why: if he was moved in after his game, the slot never got it
    (rule 4). `skip` is the saved locks, which locks.disagreements covers.
    """
    lines = []
    for s in slots:
        theirs = sleeper_points.get(s.label, 0.0)
        if s.label in skip or not s.player_id or abs(s.score - theirs) < SAME_SCORE:
            continue
        games = by_player.get(s.player_id) or []
        live = " His latest game isn't final yet." if games and not games[-1].final else ""
        lines.append(f"{s.label} ({name(s.player_id)}): our score {s.score:.1f}, Sleeper's "
                     f"{theirs:.1f}. Using {s.score:.1f}.{live} If you lock it and Sleeper's "
                     f"is right, type {s.label}={theirs:.1f}.")
    return lines


def settle_new_lock(slot, games: list[WeekGame] | None, sleeper_score: float,
                    name=str) -> str | None:
    """Pick the game a slot was just locked on, and set its score and game.

    Sleeper only lets you lock after a game ends, so the locked game is the
    one whose score matches Sleeper's slot number now. That also works when
    Sleeper's schedule still says in_game a few minutes after the final, or
    when the lock was made in the morning and told to the app later. If no
    game matches (Sleeper's number is stale), his latest started game is
    used, with a note to check. Returns that note, or None.
    """
    if games is None:
        return None  # stats unavailable: keep Sleeper's number, no game saved
    if not games:
        return f"{slot.label} ({name(slot.player_id)}) has no game this week yet. Check that lock."
    matches = [g for g in games if abs(g.points - sleeper_score) < SAME_SCORE]
    game = (matches or games)[-1]
    slot.score, slot.game_id = game.points, game.game_id
    if matches:
        return None
    return (f"{slot.label} ({name(slot.player_id)}): none of his games this week match "
            f"Sleeper's {sleeper_score:.1f}, so the lock was saved with his latest game "
            f"({game.day:%a %m/%d}, {game.points:.1f}). Check it in Sleeper; type "
            f"{slot.label}=score if it's off.")
