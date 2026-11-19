"""The app's engine: plain functions that fetch data, run the math, and
return the results as data.

Nothing in this module prints or asks questions. The command line
(__main__.py) calls these functions and prints what they return; the planned
web app (FastAPI) and the Gemini chat's tools will call the same functions.
So every caller gets the same answers, and "the math decides; the AI
explains" holds: the chat can only report numbers that come from here.

Things worth telling the user that aren't errors (a source that failed, a
player skipped) come back in a `warnings` list instead of being printed, so
each caller can show them its own way. They're plain sentences.

The main entry points, in the order `advise` uses them:

    team = load_team()                        # your league, roster, players
    lineup = current_lineup(team, now)        # slots with our scores, saved locks
    record_locks(team, lineup, newly_locked)  # after asking which slots are locked
    advice = advise(team, lineup, now)        # locks, the week's plan, today's moves
    save_advice(advice)                       # into the advice log for `review`

Also: lock_decision (one player and score: lock or wait?), player_profile,
week_grid, saved_advice + review.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import advice_log, gamelogs, locks, schedule, sleeper, sleeper_stats, slot_scores
from .advice_log import Call, SlotOutcome
from .lineup import (LOCK_TARGET, FutureGame, LockMove, Slot, TodayMove, WeekPlan, alternatives,
                     apply_moves, depth_label, final_scores, lock_floor, pass_values, plan_week,
                     player_values, recommend_locks, slot_labels, today_plan)
from .lockin import GameForecast, value_of_waiting
from .profile import PlayerProfile, build_profile
from .scoring import fantasy_points, unknown_scoring_keys
from .slot_scores import WeekGame, week_games
from .status import play_chances

CURRENT_SEASON = "2026-27"
LAST_SEASON = "2025-26"


class SetupError(Exception):
    """Something the user has to fix before the app can run (e.g. .env)."""


# ---------- Names ----------

def player_name(p: dict) -> str:
    """A Sleeper player record's full name: 'Jalen Brunson'."""
    return p.get("full_name") or f"{p.get('first_name', '?')} {p.get('last_name', '')}".strip()


def short_name(p: dict) -> str:
    """'Jalen Brunson' -> 'J. Brunson'."""
    first, last = p.get("first_name") or "", p.get("last_name") or ""
    return f"{first[:1]}. {last}" if first and last else player_name(p)


# ---------- Your team ----------

@dataclass
class Team:
    """Your Sleeper league, your roster in it, and Sleeper's player records."""
    user: dict
    league: dict
    roster: dict
    players: dict        # Sleeper player id -> player record (every NBA player)
    warnings: list[str] = field(default_factory=list)

    @property
    def scoring(self) -> dict:
        """The league's scoring settings, read live from Sleeper."""
        return self.league["scoring_settings"]

    @property
    def player_ids(self) -> list[str]:
        """Everyone on your roster (starters, bench and IR)."""
        return self.roster.get("players") or []

    @property
    def injured_reserve(self) -> set[str]:
        return set(self.roster.get("reserve") or [])

    @property
    def positions(self) -> dict[str, set[str]]:
        """Each roster player's Sleeper positions, which decide the slots he can fill."""
        return {pid: set(self.player(pid).get("fantasy_positions")
                         or [self.player(pid).get("position") or ""])
                for pid in self.player_ids}

    def player(self, pid: str | None) -> dict:
        return self.players.get(pid, {}) if pid else {}

    def name(self, pid: str | None) -> str:
        """Full name: 'Jamal Murray'."""
        return player_name(self.player(pid))

    def short(self, pid: str | None) -> str:
        """Short name for tables: 'J. Murray'."""
        return short_name(self.player(pid))


def load_team() -> Team:
    """Log in to Sleeper with the username and league in .env and load your team.

    Raises SetupError if .env is missing them or you have no roster in the league.
    """
    username = os.getenv("SLEEPER_USERNAME")
    league_id = os.getenv("SLEEPER_LEAGUE_ID")
    if not username or not league_id:
        raise SetupError("Set SLEEPER_USERNAME and SLEEPER_LEAGUE_ID in your .env file.")
    user = sleeper.get_user(username)
    league = sleeper.get_league(league_id)
    roster = sleeper.find_my_roster(sleeper.get_rosters(league_id), user["user_id"])
    if roster is None:
        raise SetupError(f"Couldn't find a roster owned by {username} in this league.")
    warnings = []
    unknown = unknown_scoring_keys(league.get("scoring_settings") or {})
    if unknown:
        warnings.append(f"your league has scoring rules this app doesn't handle yet: "
                        f"{', '.join(unknown)}. Fantasy points will be off until they're added.")
    return Team(user, league, roster, sleeper.get_players("nba"), warnings)


# ---------- Data helpers (each returns its warnings instead of printing) ----------

def today_statuses(today: date) -> tuple[dict[str, str] | None, str | None]:
    """({team: Sleeper's schedule status} for today's games, warning).

    The status says whether each game has tipped off (pre_game, in_game,
    complete). If the schedule can't be read: (None, a warning), and the
    7pm rule applies (schedule.still_to_come).
    """
    try:
        return sleeper_stats.team_statuses(CURRENT_SEASON, today), None
    except sleeper_stats.SleeperStatsUnavailable as err:
        return None, (f"Couldn't read Sleeper's schedule: {err}. Counting today's games as "
                      f"started from 7pm.")


def build_profiles(team: Team, player_ids: list[str]) -> tuple[dict[str, PlayerProfile], list[str]]:
    """({player id: PlayerProfile}, warnings) from cached game logs (Sleeper, or
    NBA.com as the fallback). Players with no game logs are skipped, with a warning."""
    profiles, warnings = {}, []
    for pid in player_ids:
        name = team.name(pid)
        try:
            this_season = gamelogs.season_games(pid, name, CURRENT_SEASON, is_current=True,
                                                note=warnings.append)
            last_season = gamelogs.season_games(pid, name, LAST_SEASON, is_current=False,
                                                note=warnings.append)
        except gamelogs.NoGameLogs as err:
            warnings.append(f"skipping {name}: {err}")
            continue
        prof = build_profile(name, this_season, last_season, team.scoring)
        if prof:
            profiles[pid] = prof
    return profiles, warnings


def sleeper_slots(team: Team, week_no: int) -> list[Slot]:
    """Your starting slots for a week, with each slot's player and score from
    Sleeper's matchup. Only the players are trusted: slot_scores replaces the
    scores with our own, worked out from box scores."""
    mine = next(m for m in sleeper.get_matchups(team.league["league_id"], week_no)
                if m["roster_id"] == team.roster["roster_id"])
    kinds = [p for p in team.league["roster_positions"] if p not in ("BN", "IR")]
    starters = mine.get("starters") or []
    points = mine.get("starters_points") or []
    slots = []
    for i, (label, kind) in enumerate(zip(slot_labels(kinds), kinds)):
        pid = starters[i] if i < len(starters) and starters[i] not in ("0", None) else None
        score = points[i] if i < len(points) and pid else 0.0
        slots.append(Slot(label, kind, pid, float(score or 0)))
    return slots


def games_this_week(team: Team, player_ids: list[str | None],
                    days: list[date]) -> tuple[dict[str, list[WeekGame] | None], list[str]]:
    """({player id: his games this fantasy week that have started}, warnings).

    A player whose Sleeper stats can't be read maps to None, and his slot keeps
    Sleeper's number. Slot scores need fresh stats (a game that ended minutes
    ago must count), so anything older than a minute is downloaded again.
    """
    by_player, warnings = {}, []
    for pid in dict.fromkeys(p for p in player_ids if p):  # each once, in order
        try:
            games, statuses = sleeper_stats.games_and_statuses(pid, CURRENT_SEASON,
                                                               is_current=True, max_age=60)
        except sleeper_stats.SleeperStatsUnavailable as err:
            warnings.append(f"Sleeper stats failed for {team.name(pid)}: {err}; "
                            f"using Sleeper's slot score")
            by_player[pid] = None
            continue
        by_player[pid] = week_games(games, statuses, days, team.scoring)
    return by_player, warnings


# ---------- The current lineup ----------

@dataclass
class Lineup:
    """Your starting slots right now, scored from Sleeper's box scores.

    `slots` aren't marked locked yet: which slots are locked is asked (the
    command line) or taken from the saved locks (keep_saved_locks).
    """
    week_no: int                       # Sleeper's fantasy week number
    days: list[date]                   # Monday to Sunday of this fantasy week
    slots: list[Slot]
    saved: dict[str, dict]             # saved locks: slot -> {"player_id", "score", "game_id"}
    sleeper_points: dict[str, float]   # Sleeper's own slot numbers, to cross-check ours
    games: dict[str, list[WeekGame] | None]  # each player's games this week so far
    notes: list[str]                   # where our scores and Sleeper's disagree, and why
    warnings: list[str]                # data that couldn't be read


def current_lineup(team: Team, now: datetime) -> Lineup:
    """Your lineup this week with every slot scored from box scores.

    Sleeper's matchup gives who's in each slot; the scores come from Sleeper's
    box scores (slot_scores.py). Saved locks re-score their saved game, so stat
    corrections come through.
    """
    week_no = sleeper.get_state("nba")["week"]
    slots = sleeper_slots(team, week_no)
    days = schedule.fantasy_week(now.date())
    saved = locks.load(days[0])
    sleeper_points = {s.label: s.score for s in slots}
    games, warnings = games_this_week(
        team, [s.player_id for s in slots] + [lk["player_id"] for lk in saved.values()], days)
    notes = slot_scores.rescore_locks(saved, games, team.short)
    notes += locks.disagreements(slots, saved, team.short)
    slot_scores.score_open_slots(slots, games)
    notes += slot_scores.cross_check(slots, sleeper_points, games, saved, team.short)
    return Lineup(week_no, days, slots, saved, sleeper_points, games, notes, warnings)


def keep_saved_locks(lineup: Lineup) -> None:
    """Mark the saved locks on the lineup, as pressing Enter at advise's
    question does. For callers that don't ask (the chat, the web app)."""
    locks.lock_slots(lineup.slots, list(lineup.saved), lineup.saved, kept=True)


def record_locks(team: Team, lineup: Lineup, newly_locked: list[Slot]) -> list[str]:
    """Settle the slots just locked and save this week's lock file.

    Each new lock gets the game whose score matches Sleeper's slot number
    (slot_scores.settle_new_lock). Returns notes for locks that needed a guess.
    """
    notes = []
    for s in newly_locked:
        note = slot_scores.settle_new_lock(s, lineup.games.get(s.player_id),
                                           lineup.sleeper_points[s.label], team.short)
        if note:
            notes.append(note)
    locks.save(lineup.days[0], locks.from_slots(lineup.slots))
    return notes


# ---------- Advice ----------

@dataclass
class LockStep:
    """One recommended lock, with the Sleeper moves that carry it out."""
    move: LockMove
    instruction: str       # e.g. "Lock J. Murray in PG (31.2)."
    below_target: bool     # under the user's target (labeled, still recommended)
    before_tipoff: bool    # his next game is today and hasn't started: lock before it


@dataclass
class SlotAdvice:
    """One row of advise's lineup table (after the recommended locks)."""
    label: str
    player_id: str | None
    score: float
    status: str                  # LOCK, LOCKED, PLAYING, PASS, WAIT or OPEN
    waiting_on: str | None = None    # the player this open slot is planned for
    expected: float | None = None    # what we expect to lock from him this week
    depth: str | None = None         # THIN, OK or DEEP: how many others could fill it
    alternates: list[str] = field(default_factory=list)  # best other options, best first

    def log_row(self) -> dict:
        """The row as saved in the advice log."""
        row = {"slot": self.label, "player_id": self.player_id, "score": self.score,
               "status": self.status}
        if self.expected is not None:
            row.update(waiting_on=self.waiting_on, expected=round(self.expected, 1))
        return row


@dataclass
class TodayStart:
    """Who starts in an open slot today, and against whom."""
    move: TodayMove
    opponent: str          # "vs BOS" or "@DEN"


@dataclass
class Advice:
    """Everything advise works out. If the week is done, only `final_scores`
    (and `warnings`) are filled in."""
    warnings: list[str]
    final_scores: list[str] | None = None  # "Week's done" lines, once no one has games left
    locks: list[LockStep] = field(default_factory=list)
    held: list[LockMove] = field(default_factory=list)  # locks the floor held back
    floor: float = 0.0                 # today's lowest score worth locking
    target: float = LOCK_TARGET        # the user's lock target
    slots: list[SlotAdvice] = field(default_factory=list)
    weakest: list[tuple[str, float]] = field(default_factory=list)  # (slot, Exp), weakest first
    today: list[TodayStart] = field(default_factory=list)
    plan: WeekPlan | None = None
    values: dict[str, float] = field(default_factory=dict)  # player -> week value
    games_left: dict[str, list[FutureGame]] = field(default_factory=dict)  # player -> games
    log_entry: dict = field(default_factory=dict)  # what save_advice writes to the log

    @property
    def total_gain(self) -> float:
        """Expected points the recommended locks add to the week vs. passing."""
        return sum(step.move.gain for step in self.locks)


def advise(team: Team, lineup: Lineup, now: datetime) -> Advice:
    """The lock-or-pass advice for your lineup, with the locks already marked
    on `lineup` (asked, or keep_saved_locks). Doesn't save anything; call
    save_advice to add it to the advice log."""
    return _advise(team, lineup, now)


def _advise(team: Team, lineup: Lineup, now: datetime,
            what_if: tuple[str, float] | None = None) -> Advice:
    """advise, optionally as if one player's latest game had scored `what_if`'s
    score (for lock_decision). That game counts as finished, even if it's on now."""
    slots = lineup.slots
    if what_if:
        pid, score = what_if
        slots = [Slot(s.label, s.kind, s.player_id, score if s.player_id == pid else s.score,
                      s.locked, s.game_id) for s in slots]

    # Who can still fill slots this week: not on IR, not already locked in.
    unavailable = team.injured_reserve | {s.player_id for s in slots if s.locked}
    candidates = [pid for pid in team.player_ids if pid not in unavailable]
    profiles, warnings = build_profiles(team, candidates)

    # Games still to come. A game today counts until it tips off, which
    # Sleeper's schedule shows (pre_game); without it, until 7pm.
    statuses, problem = today_statuses(now.date())
    if problem:
        warnings.append(problem)
    started_day = statuses is None and now.hour >= schedule.FALLBACK_TIPOFF_HOUR
    first_day = now.date() + timedelta(days=1) if started_day else now.date()
    days = [d for d in lineup.days if d >= first_day]
    season = schedule.season_games(CURRENT_SEASON)
    future: dict[date, list[FutureGame]] = {d: [] for d in days}
    positions = team.positions
    # A starter whose game is on right now ("playing now") can move between
    # starting slots but not to the bench, and his slot takes this game's score
    # when it ends, so the game counts like one still to come. A bench player's
    # live game doesn't count for him, so it's lost (moving him in won't help).
    starters = {s.player_id for s in slots if not s.locked and s.player_id}
    if what_if:
        starters.discard(what_if[0])  # the game asked about is treated as finished
    live: set[str] = set()
    for pid, prof in profiles.items():
        p = team.player(pid)
        nba_team = schedule.nba_team(p.get("team"))
        slate, is_live = (schedule.games_that_count(schedule.team_games(season, nba_team, days),
                                                    nba_team, now, statuses, pid in starters)
                          if nba_team else ({}, False))
        if is_live:
            live.add(pid)
        chances = play_chances(p.get("injury_status"), len(slate))
        for (day, game), chance in zip(sorted(slate.items()), chances):
            fc = GameForecast(prof.average, prof.swing, chance)
            future[day].append(FutureGame(pid, day, fc, game.opponent_of(nba_team)))

    lock_target = float(os.getenv("LOCK_TARGET_POINTS") or LOCK_TARGET)
    floor = lock_floor(now.date())
    values = player_values(future)
    games_left: dict[str, list[FutureGame]] = {}
    for d in days:
        for g in future[d]:
            games_left.setdefault(g.player_id, []).append(g)

    # Week's over for this lineup: no advice to give, just the final scores.
    done = final_scores(slots, values, team.short)
    if done:
        return Advice(warnings, final_scores=done, floor=floor, target=lock_target,
                      values=values, games_left=games_left)

    # 1. Which scores in hand to lock, and in which slot.
    moves, held = recommend_locks(slots, values, positions, floor, live)
    after, steps = apply_moves(slots, moves, positions, team.short)
    plays_today = {g.player_id for g in future.get(now.date(), [])} - live  # not started yet
    lock_steps = [LockStep(m, step, m.score < lock_target, m.player_id in plays_today)
                  for m, step in zip(moves, steps)]

    # 2. The plan for the rest of the week: which player each open slot is waiting on.
    locked_now = {m.player_id for m in moves}
    remaining = {p: v for p, v in values.items() if p not in locked_now}
    plan = plan_week(after, pass_values(after, remaining), positions, live)
    newly = {m.to_label for m in moves}
    in_hand = {x.player_id for x in slots if not x.locked and x.player_id and x.score > 0}
    in_hand -= live  # his game is on: the score shown is about to be replaced
    rows = []
    for x in after:
        if x.locked:
            rows.append(SlotAdvice(x.label, x.player_id, x.score,
                                   "LOCK" if x.label in newly else "LOCKED"))
            continue
        status = ("OPEN" if x.player_id is None else
                  "PLAYING" if x.player_id in live else
                  "PASS" if x.player_id in in_hand else "WAIT")
        rows.append(SlotAdvice(x.label, x.player_id, x.score, status,
                               plan.assignment.get(x.label), plan.slot_value(x.label),
                               depth_label(x, plan, positions),
                               alternatives(x, plan, positions)[:3]))

    # 3. The weakest open slots are where solid scores should get locked.
    # UTIL is left out: it takes whoever's left over, but it's also the slot
    # that can catch a late big game from anyone, so it should stay open longest.
    open_slots = sorted((x for x in after if not x.locked and x.kind != "UTIL"),
                        key=lambda x: plan.slot_value(x.label))
    weakest = [(x.label, plan.slot_value(x.label)) for x in open_slots[:3]]

    # 4. Today: start the planned player if he plays; otherwise a free roll.
    today = []
    if days and days[0] == now.date():
        playing_today = {g.player_id: g for g in future.get(now.date(), [])
                         if g.player_id not in locked_now}
        today = [TodayStart(m, playing_today[m.player_id].opponent)
                 for m in today_plan(after, plan, set(playing_today), values, positions, live)]

    # 5. What save_advice writes, so `review` can grade it once the games are played.
    def move_dict(m):
        return {"player_id": m.player_id, "from": m.from_label, "to": m.to_label,
                "score": m.score, "gain": m.gain}

    log_entry = {
        "time": now.isoformat(timespec="seconds"),
        "week": lineup.week_no,
        "week_start": lineup.days[0].isoformat(),
        "first_day": first_day.isoformat(),  # games from this day on were "still to come"
        "floor": floor,
        "target": lock_target,
        "slots": [r.log_row() for r in rows],
        "locks": [move_dict(m) for m in moves],
        "held": [move_dict(h) for h in held],
        "games_left": {pid: [g.day.isoformat() for g in gs] for pid, gs in games_left.items()},
        "names": {pid: team.short(pid) for pid in team.player_ids},
    }
    return Advice(warnings, None, lock_steps, held, floor, lock_target, rows, weakest, today,
                  plan, values, games_left, log_entry)


def save_advice(advice: Advice) -> int:
    """Add the advice to the advice log (for `review` and `note`). Returns its run number."""
    return advice_log.append(advice.log_entry)


# ---------- One lock decision ----------

@dataclass
class LockCall:
    """Lock or wait, for one player and one score.

    verdict: LOCK (lock now, in `slot`), HELD (the math likes it, but it's
    under today's floor; the user's call), PASS (keep the slot open),
    ALREADY_LOCKED, NOT_STARTING (he's on the bench or IR, so his game can't
    be locked), or WEEK_DONE (no one has games left).
    """
    player_id: str
    name: str
    score: float
    verdict: str
    slot: str | None            # LOCK/HELD: where to lock him; otherwise where he sits
    gain: float | None          # LOCK/HELD: expected points locking adds vs. passing
    instruction: str | None     # LOCK: the Sleeper moves
    bar: float                  # player only: what his own remaining games are worth
    games_left: list[FutureGame]
    floor: float
    target: float
    reason: str                 # one plain sentence with the numbers behind the verdict
    warnings: list[str] = field(default_factory=list)


def lock_decision(team: Team, player_id: str, score: float, now: datetime) -> LockCall:
    """"He just scored `score`: lock or wait?" answered for the whole lineup.

    Puts the score in his slot (as if his latest game scored it, and finished,
    even if it's on now), keeps the saved locks, and runs the same math as
    advise. So the answer always agrees with advise, and says where to lock.

    `bar` is the simpler player-only view for context: what passing is worth
    from his own remaining games (lockin.value_of_waiting). The whole-lineup
    call can differ from it: locking also closes a slot someone else could
    have filled, and the floor holds back low scores.
    """
    lineup = current_lineup(team, now)
    keep_saved_locks(lineup)
    name = team.short(player_id)
    seat = next((s for s in lineup.slots if s.player_id == player_id), None)

    def call(verdict, slot, reason, gain=None, instruction=None, advice=None):
        games = advice.games_left.get(player_id, []) if advice else []
        bar = advice.values.get(player_id, 0.0) if advice else 0.0
        return LockCall(player_id, name, score, verdict, slot, gain, instruction, bar, games,
                        advice.floor if advice else lock_floor(now.date()),
                        advice.target if advice else LOCK_TARGET, reason,
                        lineup.warnings + (advice.warnings if advice else []))

    if seat is None:
        return call("NOT_STARTING", None, f"{name} isn't in a starting slot, so his game "
                    f"can't be locked (a game played on the bench doesn't count).")
    if seat.locked:
        return call("ALREADY_LOCKED", seat.label, f"{name} is already locked in {seat.label}.")

    advice = _advise(team, lineup, now, what_if=(player_id, score))
    if advice.final_scores:
        return call("WEEK_DONE", seat.label, "Nobody has games left this week, so his score "
                    "stays either way.", advice=advice)
    bar = advice.values.get(player_id, 0.0)
    bar_text = (f"his remaining games are worth about {bar:.1f} on their own"
                if player_id in advice.games_left else "he has no games left")
    for step in advice.locks:
        if step.move.player_id == player_id:
            m = step.move
            return call("LOCK", m.to_label,
                        f"Lock {name}'s {score:.1f} in {m.to_label}: it adds about {m.gain:.1f} "
                        f"expected points to the week vs. passing ({bar_text}).",
                        m.gain, step.instruction, advice)
    for m in advice.held:
        if m.player_id == player_id:
            return call("HELD", m.to_label,
                        f"The math would lock {name}'s {score:.1f} in {m.to_label} (+{m.gain:.1f}), "
                        f"but it's under today's {advice.floor:.0f}-point floor ({bar_text}). "
                        f"Your call.", m.gain, None, advice)
    return call("PASS", seat.label,
                f"Pass on {name}'s {score:.1f}: the lineup expects more by keeping "
                f"{seat.label} open ({bar_text}).", advice=advice)


# ---------- Player profiles ----------

@dataclass
class ProfileView:
    """A player's average, swing and chance to play, and his lock bars."""
    player_id: str
    name: str
    profile: PlayerProfile | None    # None: no game logs, or not enough games yet
    lock_bars: list[float]           # lock if at least this, with 1 / 2 / 3 games left
    no_logs: str | None = None       # why his game logs couldn't be loaded
    warnings: list[str] = field(default_factory=list)


def player_profile(team: Team, player_id: str) -> ProfileView:
    """A player's profile from his game logs (this season, then last season).

    The lock bars are the lock-in math for a typical game of his: with 1, 2
    or 3 games left, lock any score at least this high.
    """
    name = team.name(player_id)
    warnings: list[str] = []
    try:
        this_season = gamelogs.season_games(player_id, name, CURRENT_SEASON, is_current=True,
                                            note=warnings.append)
        last_season = gamelogs.season_games(player_id, name, LAST_SEASON, is_current=False,
                                            note=warnings.append)
    except gamelogs.NoGameLogs as err:
        return ProfileView(player_id, name, None, [], str(err), warnings)
    prof = build_profile(name, this_season, last_season, team.scoring)
    if prof is None:
        return ProfileView(player_id, name, None, [], None, warnings)
    game = prof.forecast()
    bars = [value_of_waiting([game] * n) for n in (1, 2, 3)]
    return ProfileView(player_id, name, prof, bars, None, warnings)


# ---------- The week grid ----------

@dataclass
class WeekRow:
    """One roster player's week: his games, and what's left."""
    player_id: str
    name: str
    injury_status: str | None
    on_ir: bool
    games: dict[date, str]       # day -> opponent ("vs BOS", "@DEN")
    left: int                    # games still to come
    next_chance: float | None    # chance he plays his next game; None: no games left
    tipoff_note: str             # "plays today, lock before tip-off", or ""
    playing_now: bool            # his game is on right now


@dataclass
class WeekGrid:
    days: list[date]             # Monday to Sunday
    today: date
    rows: list[WeekRow]
    playing_per_day: dict[date, int]   # players not on IR with a game, today on
    warnings: list[str]


def week_grid(team: Team, now: datetime) -> WeekGrid:
    """Who on your roster plays which day this fantasy week.

    It doesn't know who's starting, so a game on now isn't counted in `left`
    (it only counts for a starter; see advise).
    """
    season = schedule.season_games(CURRENT_SEASON)
    today = now.date()
    days = schedule.fantasy_week(today)
    statuses, problem = today_statuses(today)
    ir = team.injured_reserve
    per_day = {d: 0 for d in days}
    rows = []
    for pid in team.player_ids:
        p = team.player(pid)
        nba_team = schedule.nba_team(p.get("team"))
        slate = schedule.team_games(season, nba_team, days) if nba_team else {}
        for d, g in slate.items():
            if d >= today and pid not in ir:
                per_day[d] += 1
        to_come = schedule.games_to_come(slate, nba_team, now, statuses) if nba_team else {}
        chance = play_chances(p.get("injury_status"), len(to_come))
        live = bool(nba_team and today in slate
                    and schedule.playing_now(today, now, (statuses or {}).get(nba_team)))
        rows.append(WeekRow(pid, team.name(pid), p.get("injury_status"), pid in ir,
                            {d: g.opponent_of(nba_team) for d, g in sorted(slate.items())},
                            len(to_come), chance[0] if chance else None,
                            schedule.tipoff_note(to_come, today), live))
    return WeekGrid(days, today, rows, per_day, [problem] if problem else [])


# ---------- Review ----------

def saved_advice(all_weeks: bool = False) -> list[dict]:
    """The saved advice runs to review: the latest week's (or every week's)."""
    entries = advice_log.load()
    if entries and not all_weeks:
        latest_week = entries[-1]["week_start"]
        entries = [e for e in entries if e["week_start"] == latest_week]
    return entries


@dataclass
class Review:
    """How the saved advice turned out."""
    entries: list[dict]
    calls: list[Call]                      # each distinct call once, graded
    tally: dict[str, dict[str, int]]       # kind -> verdict -> count
    overrides: tuple[int, int]             # (your overrides that beat the app, graded overrides)
    forecast_pairs: list[tuple[float, float]]  # (Exp, actual) per finished open slot
    lineup_warnings: list[str]             # while scoring the slots
    log_warnings: list[str]                # while loading game logs to grade the calls

    def calls_in_run(self, run_id: int) -> list[Call]:
        return [c for c in self.calls if c.run_id == run_id]

    @property
    def forecast_gap(self) -> float | None:
        """How far finished slots ended above (+) or below (-) the plan's Exp, on average."""
        if not self.forecast_pairs:
            return None
        return sum(actual - exp for exp, actual in self.forecast_pairs) / len(self.forecast_pairs)


def review(team: Team, entries: list[dict], today: date) -> Review:
    """Grade the saved advice `entries` (from saved_advice) against what happened.

    Each slot is scored the same way as advise: Sleeper's lineup, our scores
    from box scores, and saved locks with their saved game. Every call is
    graded on the player's next game (advice_log.review).
    """
    outcomes_by_week, lineup_warnings = {}, []
    for week_start, week_no in sorted({(e["week_start"], e["week"]) for e in entries}):
        monday = date.fromisoformat(week_start)
        saved = locks.load(monday)
        over = advice_log.week_is_over(week_start, today)
        slots = sleeper_slots(team, week_no)
        games, warnings = games_this_week(
            team, [s.player_id for s in slots] + [lk["player_id"] for lk in saved.values()],
            [monday + timedelta(days=i) for i in range(7)])
        lineup_warnings += warnings
        slot_scores.rescore_locks(saved, games)
        slot_scores.score_open_slots(slots, games)
        locks.lock_slots(slots, list(saved), saved, kept=True)
        outcomes = {}
        for s in slots:
            is_locked = s.locked and bool(s.player_id)
            outcomes[s.label] = SlotOutcome(s.player_id, s.score, is_locked, over or is_locked)
        outcomes_by_week[week_start] = outcomes

    # Box scores for everyone the advice was about.
    log_warnings: list[str] = []
    called = {c.player_id for e in entries for c in advice_log.calls_in(e)}
    points = {pid: points_by_day(team, pid, log_warnings) for pid in called}
    calls = advice_log.review(entries, outcomes_by_week, points, today)
    return Review(entries, calls, advice_log.tally(calls), advice_log.override_record(calls),
                  advice_log.forecast_check(entries, outcomes_by_week),
                  lineup_warnings, log_warnings)


def points_by_day(team: Team, pid: str, warnings: list[str]) -> dict[date, float]:
    """date -> fantasy points for a player's games this season (refreshed within
    the hour). Problems are added to `warnings`."""
    name = team.name(pid)
    try:
        logs = gamelogs.season_games(pid, name, CURRENT_SEASON, is_current=True,
                                     max_age=60 * 60, note=warnings.append)
    except gamelogs.NoGameLogs as err:
        warnings.append(f"can't check {name}: {err}")
        return {}
    return {datetime.strptime(b["game_date"], "%b %d, %Y").date(): fantasy_points(b, team.scoring)
            for b in logs if b.get("game_date")}
