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

Your odds this week, after advise:

    opponent, problem = load_opponent(team, lineup.week_no, now)  # their team and lineup
    record_locks(opponent.team, opponent.lineup, newly_locked)    # their locks, as you typed them
    odds = win_probability(team, lineup, advice, opponent, now)   # simulated (winprob.py)

Also: lock_decision (one player and score: lock or wait?), player_profile,
week_grid, injury_report, saved_advice + review.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import advice_log, gamelogs, locks, schedule, sleeper, sleeper_stats, slot_scores, winprob
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
    players_as_of: datetime | None = None   # when `players` (with injury statuses) was downloaded

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

    @property
    def team_name(self) -> str:
        """The team's name in the league, or the owner's Sleeper name."""
        return ((self.user.get("metadata") or {}).get("team_name")
                or self.user.get("display_name") or f"Team {self.roster.get('roster_id', '?')}")

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
    players = sleeper.get_players("nba")
    return Team(user, league, roster, players, warnings, sleeper.players_downloaded("nba"))


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
    lock_file: str = locks.MINE        # whose lock file: yours, or locks.OPPONENT


def current_lineup(team: Team, now: datetime, lock_file: str = locks.MINE) -> Lineup:
    """Your lineup this week with every slot scored from box scores.

    Sleeper's matchup gives who's in each slot; the scores come from Sleeper's
    box scores (slot_scores.py). Saved locks re-score their saved game, so stat
    corrections come through.

    Works for any team in the league: your opponent's lineup is scored the
    same way, with their locks from `lock_file` (locks.OPPONENT).
    """
    week_no = sleeper.get_state("nba")["week"]
    slots = sleeper_slots(team, week_no)
    days = schedule.fantasy_week(now.date())
    saved = locks.load(days[0], lock_file)
    sleeper_points = {s.label: s.score for s in slots}
    games, warnings = games_this_week(
        team, [s.player_id for s in slots] + [lk["player_id"] for lk in saved.values()], days)
    notes = slot_scores.rescore_locks(saved, games, team.short)
    notes += locks.disagreements(slots, saved, team.short)
    slot_scores.score_open_slots(slots, games)
    notes += slot_scores.cross_check(slots, sleeper_points, games, saved, team.short)
    return Lineup(week_no, days, slots, saved, sleeper_points, games, notes, warnings, lock_file)


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
    locks.save(lineup.days[0], locks.from_slots(lineup.slots), lineup.lock_file)
    return notes


# ---------- Advice ----------

def target_label(target: float) -> str:
    """How advise marks a lock under your target: 'below your 25 target'."""
    return f"below your {target:.0f} target"


def floor_label(floor: float) -> str:
    """How advise marks a lock held back by the floor:
    "held back by today's 23-point floor"."""
    return f"held back by today's {floor:.0f}-point floor"


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
    playing_now: set[str] = field(default_factory=set)  # starters whose game is on right now

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
                      values=values, games_left=games_left, playing_now=live)

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
                  plan, values, games_left, log_entry, live)


def save_advice(advice: Advice, odds: WinOdds | None = None) -> int:
    """Add the advice to the advice log (for `review` and `note`), with the win
    probability when there is one (review doesn't grade it; it's saved so we
    can later check whether 60% weeks are won about 60% of the time).
    Returns its run number."""
    entry = dict(advice.log_entry)
    if odds is not None and odds.percent is not None:
        entry["win_probability"] = {
            "percent": odds.percent, "opponent": odds.opponent, "policy": odds.policy,
            "my_expected": round(odds.my_expected, 1),
            "their_expected": round(odds.their_expected, 1),
            "sims": odds.sims, "seed": odds.seed}
    return advice_log.append(entry)


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
    label: str | None = None    # advise's label: LOCK under the target, or HELD by the floor


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

    def call(verdict, slot, reason, gain=None, instruction=None, advice=None, label=None):
        games = advice.games_left.get(player_id, []) if advice else []
        bar = advice.values.get(player_id, 0.0) if advice else 0.0
        return LockCall(player_id, name, score, verdict, slot, gain, instruction, bar, games,
                        advice.floor if advice else lock_floor(now.date()),
                        advice.target if advice else LOCK_TARGET, reason,
                        lineup.warnings + (advice.warnings if advice else []), label)

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
            label = target_label(advice.target) if step.below_target else None
            marked = f" ({label})" if label else ""
            return call("LOCK", m.to_label,
                        f"Lock {name}'s {score:.1f} in {m.to_label}{marked}: it adds about "
                        f"{m.gain:.1f} expected points to the week vs. passing ({bar_text}).",
                        m.gain, step.instruction, advice, label)
    for m in advice.held:
        if m.player_id == player_id:
            return call("HELD", m.to_label,
                        f"The math would lock {name}'s {score:.1f} in {m.to_label} (+{m.gain:.1f}), "
                        f"but it's under today's {advice.floor:.0f}-point floor ({bar_text}). "
                        f"Your call.", m.gain, None, advice, floor_label(advice.floor))
    return call("PASS", seat.label,
                f"Pass on {name}'s {score:.1f}: the lineup expects more by keeping "
                f"{seat.label} open ({bar_text}).", advice=advice)


# ---------- Win probability ----------

SMART, PASSIVE = "smart", "passive"
POLICIES = (SMART, PASSIVE)
POLICY_TEXT = {SMART: "they play smart: the app's own advice, with your target and floor",
               PASSIVE: "they play passive: no locks, each slot keeps its last game"}


def opponent_policy(asked: str | None = None) -> tuple[str, str | None]:
    """(how the opponent is assumed to play, warning).

    `asked` if given (the chat), else OPPONENT_POLICY in .env, else smart.
    An unknown value falls back to smart, with a warning.
    """
    raw = (asked or os.getenv("OPPONENT_POLICY") or SMART).strip().lower()
    if raw in POLICIES:
        return raw, None
    return SMART, f"OPPONENT_POLICY={raw!r} isn't smart or passive, so using smart"


def opponent_team(team: Team, week_no: int) -> Team | None:
    """This week's opponent as a Team (same league and player list as yours),
    or None if you have no opponent this week (no matchup_id, or nobody else
    in your matchup)."""
    league_id = team.league["league_id"]
    matchups = sleeper.get_matchups(league_id, week_no)
    mine = next((m for m in matchups if m["roster_id"] == team.roster["roster_id"]), None)
    if not mine or mine.get("matchup_id") is None:
        return None
    other = next((m for m in matchups if m.get("matchup_id") == mine["matchup_id"]
                  and m["roster_id"] != mine["roster_id"]), None)
    if other is None:
        return None
    roster = next((r for r in sleeper.get_rosters(league_id)
                   if r["roster_id"] == other["roster_id"]), None)
    if roster is None:
        raise ValueError(f"Sleeper's rosters have no roster {other['roster_id']}")
    owner = next((u for u in sleeper.get_league_users(league_id)
                  if u.get("user_id") == roster.get("owner_id")), {})
    return Team(owner, team.league, roster, team.players, [], team.players_as_of)


@dataclass
class Opponent:
    """Your opponent this week: their team and their lineup, scored the same
    way as yours (box scores, complete games only), with the locks you told
    the app about (their lock file, locks.OPPONENT)."""
    team: Team
    lineup: Lineup


def load_opponent(team: Team, week_no: int, now: datetime) -> tuple[Opponent | None, str | None]:
    """(your opponent, None), or (None, one sentence why not).

    The win probability is extra, so nothing here stops advise: no opponent
    this week, or any failure loading them, comes back as the sentence.
    Their locks aren't marked yet (ask, or keep_saved_locks).
    """
    try:
        opp = opponent_team(team, week_no)
        if opp is None:
            return None, "No opponent this week, so no win probability."
        return Opponent(opp, current_lineup(opp, now, locks.OPPONENT)), None
    except Exception as err:   # the win probability is optional: never break advise
        return None, (f"Couldn't load your opponent's lineup ({type(err).__name__}: {err}), "
                      f"so no win probability this run.")


@dataclass
class LockOdds:
    """How one lock changes your win probability, in whole percents."""
    move: LockMove
    without: int     # recommended lock: skipping it (the rest of the advice taken);
                     # held lock: following the advice, which passes on it
    with_it: int     # taking it

    @property
    def change(self) -> int:
        """Worked out from the rounded percents, so '+4 (54% -> 58%)' adds up."""
        return self.with_it - self.without


@dataclass
class WinOdds:
    """Your chance of winning this week's matchup (winprob.py)."""
    opponent: str               # their team name
    policy: str                 # how they're assumed to play: smart or passive
    percent: int | None         # win probability following the advice; None once both weeks are done
    my_expected: float          # your average simulated total (your final total once done)
    their_expected: float       # theirs
    final: bool                 # both teams are done: the totals are final, nothing simulated
    locks: list[LockOdds] = field(default_factory=list)   # each recommended lock
    held: list[LockOdds] = field(default_factory=list)    # each lock held back by the floor (your call)
    sims: int = winprob.SIMULATIONS
    seed: int = winprob.SEED
    warnings: list[str] = field(default_factory=list)


def _lineup_after(slots: list[Slot], moves: list[LockMove],
                  positions: dict[str, set[str]]) -> list[Slot] | None:
    """The lineup after these locks, each carried out from wherever its player
    sits by then (an earlier lock can swap him into another slot, and with a
    lock skipped he may still be where he started). None if a lock can't be
    done (its slot is taken by an earlier lock)."""
    after = [Slot(s.label, s.kind, s.player_id, s.score, s.locked) for s in slots]
    for m in moves:
        src = next((s for s in after if s.player_id == m.player_id and not s.locked), None)
        dst = next((s for s in after if s.label == m.to_label), None)
        if src is None or dst is None or dst.locked:
            return None
        after, _ = apply_moves(after, [LockMove(m.player_id, src.label, m.to_label, m.score,
                                                m.gain)], positions)
    return after


def _app_week(slots: list[Slot], moves: list[LockMove], advice: Advice,
              positions: dict[str, set[str]]) -> winprob.TeamWeek | None:
    """The week if these locks are made now and the rest follows the app:
    each open slot waits on the player the week plan gives it (as advise's
    'Waiting on' column), who locks by his lock bars and the floor."""
    after = _lineup_after(slots, moves, positions)
    if after is None:
        return None
    locked_now = {m.player_id for m in moves}
    remaining = {p: v for p, v in advice.values.items() if p not in locked_now}
    plan = plan_week(after, pass_values(after, remaining), positions, advice.playing_now)
    week = []
    for s in after:
        if s.locked:
            week.append(winprob.SlotPlan(s.label, s.player_id, s.score, locked=True))
            continue
        pid = plan.assignment.get(s.label)
        # A planned player with no games left keeps the score he has (his pass value).
        kept = 0.0 if pid is None or pid in advice.games_left else plan.slot_value(s.label)
        week.append(winprob.SlotPlan(s.label, pid, kept))
    return winprob.TeamWeek(tuple(week))


def _passive_week(slots: list[Slot]) -> winprob.TeamWeek:
    """A set-and-forget manager's week: nobody moves or locks, so each slot
    ends with its player's last game. The locks you entered for them stand."""
    return winprob.TeamWeek(tuple(winprob.SlotPlan(s.label, s.player_id, s.score, s.locked,
                                                   winprob.PASSIVE) for s in slots))


def _fixed_week(slots: list[Slot]) -> winprob.TeamWeek:
    """A finished week: every slot keeps the score it has."""
    return winprob.TeamWeek(tuple(winprob.SlotPlan(s.label, None, s.score) for s in slots))


def win_probability(team: Team, lineup: Lineup, advice: Advice, opponent: Opponent,
                    now: datetime, policy: str | None = None,
                    sims: int | None = None, seed: int | None = None) -> WinOdds:
    """Your chance of beating your opponent this week if you follow `advice`.

    `lineup` and `opponent.lineup` have their locks marked (asked, or
    keep_saved_locks). The opponent plays `policy` (opponent_policy): smart
    runs the same engine on their lineup, with your target and floor (its
    recommended locks now, then the week plan); passive never locks.

    Also each recommended lock's change (following all the advice vs.
    skipping just that one) and each held lock's (taking it vs. following the
    advice, which passes on it). Every version of your week is played on the
    same simulated games (winprob.simulated_weeks), so a change is the lock's
    effect, not luck. Same inputs and seed, same answer.

    Once both teams are done, nothing is simulated: the totals are final.
    `sims` and `seed` default to winprob's, looked up when this runs (not
    fixed at import), so tests can change them.
    """
    sims = winprob.SIMULATIONS if sims is None else sims
    seed = winprob.SEED if seed is None else seed
    policy, problem = opponent_policy(policy)
    warnings = [problem] if problem else []
    theirs_advice = _advise(opponent.team, opponent.lineup, now)
    warnings += [f"opponent: {w}" for w in opponent.lineup.warnings + theirs_advice.warnings]
    games = {**advice.games_left, **theirs_advice.games_left}
    name = opponent.team.team_name

    # Your week: the advice, and the versions to compare it with.
    recommended = [step.move for step in advice.locks]
    if advice.final_scores:
        mine, skips, takes = _fixed_week(lineup.slots), [], []
    else:
        mine = _app_week(lineup.slots, recommended, advice, team.positions)
        skips = [(m, _app_week(lineup.slots, [x for x in recommended if x is not m], advice,
                               team.positions)) for m in recommended]
        takes = [(h, _app_week(lineup.slots, recommended + [h], advice, team.positions))
                 for h in advice.held]
        skips = [(m, w) for m, w in skips if w is not None]
        takes = [(h, w) for h, w in takes if w is not None]

    # Theirs.
    if theirs_advice.final_scores:
        theirs = _fixed_week(opponent.lineup.slots)
    elif policy == PASSIVE:
        theirs = _passive_week(opponent.lineup.slots)
    else:
        theirs = _app_week(opponent.lineup.slots, [s.move for s in theirs_advice.locks],
                           theirs_advice, opponent.team.positions)

    if mine.settled(games) and theirs.settled(games):
        return WinOdds(name, policy, None, mine.total_now(), theirs.total_now(), True,
                       sims=sims, seed=seed, warnings=warnings)

    odds = winprob.simulate([mine] + [w for _, w in skips] + [w for _, w in takes], theirs,
                            games, lock_floor, sims, seed)
    base, rest = odds[0], odds[1:]
    lock_odds = [LockOdds(m, o.percent, base.percent) for (m, _), o in zip(skips, rest)]
    held_odds = [LockOdds(h, base.percent, o.percent)
                 for (h, _), o in zip(takes, rest[len(skips):])]
    return WinOdds(name, policy, base.percent, base.mine, base.theirs, False, lock_odds,
                   held_odds, sims, seed, warnings)


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
    next_game: date | None = None    # his next game that hasn't started; None: none left


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
                            schedule.tipoff_note(to_come, today), live,
                            min(to_come) if to_come else None))
    return WeekGrid(days, today, rows, per_day, [problem] if problem else [])


# ---------- Injuries ----------

@dataclass
class InjuryRow:
    """One roster player's injury status and his chance to play his next game."""
    player_id: str
    name: str
    status: str | None          # Sleeper's injury status (Out, Questionable, ...); None: none listed
    on_ir: bool
    chance: float               # chance he plays his next game (status.py), this week or not
    next_game: date | None      # that game, if it's this week and hasn't started
    opponent: str | None        # "vs BOS" or "@DEN"
    playing_now: bool           # his game is on right now


@dataclass
class InjuryReport:
    rows: list[InjuryRow]
    as_of: datetime | None      # when Sleeper's player list, which holds the statuses, was downloaded
    warnings: list[str]


def injury_report(team: Team, now: datetime) -> InjuryReport:
    """Each roster player's Sleeper injury status and chance to play his next game.

    The chance is the same one advise uses for his next game (status.py:
    'Out' means out for the next game only). The statuses come with Sleeper's
    player list, which is downloaded at most once a day, so `as_of` says how
    old they are.
    """
    grid = week_grid(team, now)
    rows = [InjuryRow(r.player_id, r.name, r.injury_status, r.on_ir,
                      play_chances(r.injury_status, 1)[0], r.next_game,
                      r.games.get(r.next_game) if r.next_game else None, r.playing_now)
            for r in grid.rows]
    return InjuryReport(rows, team.players_as_of, grid.warnings)


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
