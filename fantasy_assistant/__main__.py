"""Command-line entry point. Run from the project folder:

    python -m fantasy_assistant roster     # your league and roster
    python -m fantasy_assistant profiles   # each player's average, swing, and lock-in bars
    python -m fantasy_assistant week       # who plays which day this week
    python -m fantasy_assistant advise     # LOCK or PASS for every lineup slot (saved to a log)
    python -m fantasy_assistant review     # how the saved advice turned out
    python -m fantasy_assistant note 7 PG locked him anyway   # note on saved advice #7
    python -m fantasy_assistant merge data # merge another computer's locks and advice log
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from . import sleeper, state

CURRENT_SEASON = "2026-27"
LAST_SEASON = "2025-26"


def _player_name(p: dict) -> str:
    return p.get("full_name") or f"{p.get('first_name', '?')} {p.get('last_name', '')}".strip()


def _load_my_team():
    """League, your roster's player ids, and Sleeper's player info."""
    username = os.getenv("SLEEPER_USERNAME")
    league_id = os.getenv("SLEEPER_LEAGUE_ID")
    if not username or not league_id:
        sys.exit("Set SLEEPER_USERNAME and SLEEPER_LEAGUE_ID in your .env file.")

    user = sleeper.get_user(username)
    league = sleeper.get_league(league_id)
    roster = sleeper.find_my_roster(sleeper.get_rosters(league_id), user["user_id"])
    if roster is None:
        sys.exit(f"Couldn't find a roster owned by {username} in this league.")
    from .scoring import unknown_scoring_keys

    unknown = unknown_scoring_keys(league.get("scoring_settings") or {})
    if unknown:
        print(f"WARNING: your league has scoring rules this app doesn't handle yet: "
              f"{', '.join(unknown)}. Fantasy points will be off until they're added.\n")
    return user, league, roster, sleeper.get_players("nba")


def cmd_roster() -> int:
    user, league, roster, players = _load_my_team()
    player_ids = roster.get("players") or []
    print(f"League: {league['name']}  ({league['sport'].upper()} {league['season']}, "
          f"{league['total_rosters']} teams)")
    print(f"Lineup: {', '.join(p for p in league['roster_positions'] if p != 'BN')}")
    print(f"\n{user['display_name']}'s roster:")
    for pid in player_ids:
        p = players.get(pid, {})
        positions = "/".join(p.get("fantasy_positions") or [p.get("position") or "?"])
        print(f"  {_player_name(p):<26} {positions:<10} {p.get('team') or 'FA':<4} "
              f"{p.get('injury_status') or ''}")
    return 0


def cmd_profiles() -> int:
    from . import gamelogs
    from .lockin import value_of_waiting
    from .profile import build_profile

    _, league, roster, players = _load_my_team()
    player_ids = roster.get("players") or []
    scoring = league["scoring_settings"]

    print("Pulling game logs from Sleeper...\n")
    print(f"{'Player':<24} {'Games':>5} {'Avg':>6} {'Swing':>6} {'Plays':>6}"
          f"   Lock if at least, with 1 / 2 / 3 games left")
    failed = 0
    for pid in player_ids:
        name = _player_name(players.get(pid, {}))
        try:
            this_season = gamelogs.season_games(pid, name, CURRENT_SEASON, is_current=True)
            last_season = gamelogs.season_games(pid, name, LAST_SEASON, is_current=False)
        except gamelogs.NoGameLogs as err:
            print(f"{name:<24}  no game logs: {err}")
            failed += 1
            continue
        prof = build_profile(name, this_season + last_season, scoring, len(last_season))
        if prof is None:
            print(f"{name:<24}  not enough games yet")
            continue
        game = prof.forecast()
        bars = [value_of_waiting([game] * n) for n in (1, 2, 3)]
        print(f"{name:<24} {prof.games_used:>5} {prof.average:>6.1f} {prof.swing:>6.1f} "
              f"{prof.play_prob:>6.0%}   {bars[0]:5.1f} / {bars[1]:5.1f} / {bars[2]:5.1f}")

    if failed:
        print(f"\n{failed} player(s) couldn't be downloaded. Anything already downloaded "
              "is saved, so rerunning only retries the missing ones.")
    return 0


def _today_statuses(today) -> dict[str, str] | None:
    """{team: Sleeper's schedule status} for today's games, which says whether
    each has tipped off. None (with a note) if the schedule can't be read."""
    from . import sleeper_stats

    try:
        return sleeper_stats.team_statuses(CURRENT_SEASON, today)
    except sleeper_stats.SleeperStatsUnavailable as err:
        print(f"  (Couldn't read Sleeper's schedule: {err}. Counting today's games as "
              f"started from 7pm.)")
        return None


def cmd_week() -> int:
    from datetime import datetime

    from . import schedule
    from .status import play_chances

    _, _, roster, players = _load_my_team()
    games = schedule.season_games(CURRENT_SEASON)
    now = datetime.now()
    today = now.date()
    week = schedule.fantasy_week(today)
    today_statuses = _today_statuses(today)
    injured_reserve = set(roster.get("reserve") or [])

    header = "".join(f"{d.strftime('%a %m/%d'):>10}" for d in week)
    print(f"Week of {week[0]:%b %d} - {week[-1]:%b %d}   (* = today)\n")
    print(f"{'Player':<22} {'Status':<7}{header}   Left  Next game")
    per_day = {d: 0 for d in week}
    for pid in roster.get("players") or []:
        p = players.get(pid, {})
        team = schedule.nba_team(p.get("team"))
        slate = schedule.team_games(games, team, week) if team else {}
        status = p.get("injury_status")
        cells = ""
        for d in week:
            g = slate.get(d)
            text = g.opponent_of(team) if g else "-"
            if g and d == today:
                text = "*" + text
            cells += f"{text:>10}"
            if g and d >= today and pid not in injured_reserve:
                per_day[d] += 1
        to_come = schedule.games_to_come(slate, team, now, today_statuses) if team else {}
        left = len(to_come)
        chance = play_chances(status, left)
        next_txt = f"{chance[0]:.0%} to play" if chance else "no games left"
        tipoff = schedule.tipoff_note(to_come, today)
        if tipoff:
            next_txt += f"; {tipoff}"
        label = (status or "") + (" IR" if pid in injured_reserve else "")
        print(f"{_player_name(p)[:22]:<22} {label:<7}{cells}   {left:>4}  {next_txt}")

    counts = "".join(f"{(str(per_day[d]) if d >= today else ''):>10}" for d in week)
    print(f"\n{'Playing (not on IR)':<30}{counts}")
    return 0


def _build_profiles(player_ids, players, scoring) -> dict:
    """Sleeper player id -> PlayerProfile, from cached game logs (Sleeper, or NBA.com)."""
    from . import gamelogs
    from .profile import build_profile

    profiles = {}
    for pid in player_ids:
        name = _player_name(players.get(pid, {}))
        try:
            this_season = gamelogs.season_games(pid, name, CURRENT_SEASON, is_current=True)
            last_season = gamelogs.season_games(pid, name, LAST_SEASON, is_current=False)
        except gamelogs.NoGameLogs as err:
            print(f"  (skipping {name}: {err})")
            continue
        prof = build_profile(name, this_season + last_season, scoring, len(last_season))
        if prof:
            profiles[pid] = prof
    return profiles


def _ask_locks(slots, saved: dict[str, dict]) -> list:
    """Ask which slots are locked and mark them (with the right score) on `slots`.
    Returns the slots locked just now (not saved before, no score typed)."""
    from .locks import clean, lock_slots, parse_answer, typed_scores, unlocked_by

    labels = [s.label for s in slots]
    # Saved locks are never dropped silently: if Sleeper disagrees, it was
    # already printed above, and Enter still keeps them.
    kept = [lab for lab in saved if lab in labels]
    hint = ", ".join(kept) if kept else "none"
    while True:
        answer = _read(f"\nWhich slots are locked? (e.g. PG F UTIL2, F=27.9 if Sleeper shows "
                       f"the wrong score, 'none', or Enter to keep: {hint}) > ")
        chosen = parse_answer(answer, labels, kept)
        if chosen is None:
            print(f"Didn't recognize that. Slot names are: {' '.join(labels)}")
            continue
        # Locks are permanent in Sleeper, so removing a saved one needs a 'yes'.
        # With piped input there's no second line, so the saved locks stay.
        removing = unlocked_by(chosen, kept)
        if removing:
            sure = _read(f"That removes {', '.join(removing)}, saved as locked. Locks are permanent "
                         f"in Sleeper, so this is usually a typo. Type yes to remove "
                         f"{'it' if len(removing) == 1 else 'them'}, or Enter to answer again > ")
            if clean(sure).lower() not in ("yes", "y"):
                continue
        return lock_slots(slots, chosen, saved, typed_scores(answer, labels),
                          kept=not clean(answer))


def _read(prompt: str) -> str:
    """input(), but the end of piped input counts as pressing Enter."""
    try:
        return input(prompt)
    except EOFError:
        return ""


def _week_games(player_ids, days, scoring: dict, players: dict) -> dict:
    """Sleeper player id -> his games this fantasy week that have started
    (slot_scores.WeekGame), or None if Sleeper's stats couldn't be read, in
    which case his slot keeps Sleeper's number.

    Slot scores need fresh stats (a game that ended minutes ago must count), so
    anything older than a minute is downloaded again: the schedule once, then
    each player, since his cache is now older than the schedule.
    """
    from . import sleeper_stats
    from .slot_scores import week_games

    by_player = {}
    for pid in dict.fromkeys(p for p in player_ids if p):  # each once, in order
        try:
            games, statuses = sleeper_stats.games_and_statuses(pid, CURRENT_SEASON,
                                                               is_current=True, max_age=60)
        except sleeper_stats.SleeperStatsUnavailable as err:
            print(f"  (Sleeper stats failed for {_player_name(players.get(pid, {}))}: {err}; "
                  f"using Sleeper's slot score)")
            by_player[pid] = None
            continue
        by_player[pid] = week_games(games, statuses, days, scoring)
    return by_player


def _sleeper_slots(league, roster, week_no: int) -> list:
    """Your starting slots for a week, with each slot's player and score from Sleeper.
    Only the players are trusted; slot_scores replaces the scores with our own."""
    from .lineup import Slot, slot_labels

    mine = next(m for m in sleeper.get_matchups(league["league_id"], week_no)
                if m["roster_id"] == roster["roster_id"])
    kinds = [p for p in league["roster_positions"] if p not in ("BN", "IR")]
    labels = slot_labels(kinds)
    starters = mine.get("starters") or []
    points = mine.get("starters_points") or []
    slots = []
    for i, (label, kind) in enumerate(zip(labels, kinds)):
        pid = starters[i] if i < len(starters) and starters[i] not in ("0", None) else None
        score = points[i] if i < len(points) and pid else 0.0
        slots.append(Slot(label, kind, pid, float(score or 0)))
    return slots


def _short_name(p: dict) -> str:
    """'Jalen Brunson' -> 'J. Brunson'."""
    first, last = p.get("first_name") or "", p.get("last_name") or ""
    return f"{first[:1]}. {last}" if first and last else _player_name(p)


def cmd_advise() -> int:
    from datetime import datetime, timedelta

    from . import advice_log, locks, schedule, slot_scores
    from .lineup import (LOCK_TARGET, FutureGame, alternatives, apply_moves, depth_label,
                         final_scores, lock_floor, pass_values, plan_week, player_values,
                         recommend_locks, today_line, today_plan)
    from .lockin import GameForecast
    from .status import play_chances

    _, league, roster, players = _load_my_team()
    week_no = sleeper.get_state("nba")["week"]
    slots = _sleeper_slots(league, roster, week_no)

    now = datetime.now()
    week = schedule.fantasy_week(now.date())
    saved = locks.load(week[0])

    def short(pid):
        return _short_name(players.get(pid, {}))

    # Sleeper's matchup gives the lineup; the scores come from Sleeper's box
    # scores (slot_scores.py). Sleeper's own numbers are kept to cross-check.
    sleeper_points = {s.label: s.score for s in slots}
    by_player = _week_games([s.player_id for s in slots] + [lk["player_id"] for lk in saved.values()],
                            week, league["scoring_settings"], players)
    notes = slot_scores.rescore_locks(saved, by_player, short)
    notes += locks.disagreements(slots, saved, short)
    slot_scores.score_open_slots(slots, by_player)
    notes += slot_scores.cross_check(slots, sleeper_points, by_player, saved, short)

    print(f"Week {week_no}: your lineup right now (scores from Sleeper's box scores; "
          f"locked slots show the saved game)\n")
    for s in slots:
        pid, score = locks.shown(s, saved)
        name = _player_name(players.get(pid, {})) if pid else "(empty)"
        mark = "  [saved as locked]" if s.label in saved else ""
        print(f"  {s.label:<6} {name:<24} {score:6.1f}{mark}")
    for line in notes:
        print(f"  Note: {line}")
    for s in _ask_locks(slots, saved):
        note = slot_scores.settle_new_lock(s, by_player.get(s.player_id),
                                           sleeper_points[s.label], short)
        if note:
            print(f"  Note: {note}")
    locks.save(week[0], locks.from_slots(slots))

    # Who can still fill slots this week: not on IR, not already locked in.
    unavailable = set(roster.get("reserve") or []) | {s.player_id for s in slots if s.locked}
    candidates = [pid for pid in roster.get("players") or [] if pid not in unavailable]
    print("\nLoading player profiles (cached game logs)...")
    profiles = _build_profiles(candidates, players, league["scoring_settings"])

    # Games still to come. A game today counts until it tips off, which
    # Sleeper's schedule shows (pre_game); without it, until 7pm.
    today_statuses = _today_statuses(now.date())
    started_day = today_statuses is None and now.hour >= schedule.FALLBACK_TIPOFF_HOUR
    first_day = now.date() + timedelta(days=1) if started_day else now.date()
    days = [d for d in week if d >= first_day]
    games = schedule.season_games(CURRENT_SEASON)
    future: dict = {d: [] for d in days}
    positions = {pid: set(players.get(pid, {}).get("fantasy_positions")
                          or [players.get(pid, {}).get("position") or ""])
                 for pid in roster.get("players") or []}
    for pid, prof in profiles.items():
        p = players.get(pid, {})
        team = schedule.nba_team(p.get("team"))
        slate = (schedule.games_to_come(schedule.team_games(games, team, days), team, now,
                                        today_statuses) if team else {})
        chances = play_chances(p.get("injury_status"), len(slate))
        for (day, game), chance in zip(sorted(slate.items()), chances):
            fc = GameForecast(prof.average, prof.swing, chance)
            future[day].append(FutureGame(pid, day, fc, game.opponent_of(team)))

    lock_target = float(os.getenv("LOCK_TARGET_POINTS") or LOCK_TARGET)
    floor = lock_floor(now.date())
    values = player_values(future)

    # Week's over for this lineup: no advice to give, just the final scores.
    done = final_scores(slots, values, short)
    if done:
        print("\n" + "\n".join(done))
        return 0

    # 1. Which scores in hand to lock, and in which slot.
    moves, held = recommend_locks(slots, values, positions, floor)
    after, steps = apply_moves(slots, moves, positions, short)
    plays_today = {g.player_id for g in future.get(now.date(), [])}  # games not started yet
    if moves:
        print("\nRecommended locks (in this order):")
        for move, step in zip(moves, steps):
            below = f"  [below your {lock_target:.0f} target]" if move.score < lock_target else ""
            soon = (f"  [{schedule.LOCK_BEFORE_TIPOFF}]" if move.player_id in plays_today
                    else "")
            print(f"  - {step}{below}{soon}")
        gain = sum(m.gain for m in moves)
        print(f"  These add about {gain:.1f} expected points to your week vs. passing.")
    else:
        print("\nNo locks recommended right now: every score in hand is worth passing on.")
    for h in held:
        print(f"  (Held back by today's {floor:.0f}-point floor: locking {short(h.player_id)}'s "
              f"{h.score:.1f} in {h.to_label} would add about {h.gain:.1f}. Your call.)")

    # 2. The plan for the rest of the week: which player each open slot is waiting on.
    locked_now = {m.player_id for m in moves}
    remaining = {p: v for p, v in values.items() if p not in locked_now}
    plan = plan_week(after, pass_values(after, remaining), positions)
    newly = {m.to_label for m in moves}
    in_hand = {x.player_id for x in slots if not x.locked and x.player_id and x.score > 0}

    print(f"\n{'Slot':<6} {'Now':<13} {'Score':>6}  {'Status':<6}  {'Waiting on':<13} {'Exp':>5}  "
          f"{'Depth':<5} Alternates")
    rows = []  # the same table, saved to the advice log
    for x in after:
        name = short(x.player_id) if x.player_id else "(empty)"
        row = {"slot": x.label, "player_id": x.player_id, "score": x.score}
        rows.append(row)
        if x.locked:
            row["status"] = "LOCK" if x.label in newly else "LOCKED"
            print(f"{x.label:<6} {name:<13} {x.score:6.1f}  {row['status']}")
            continue
        status = ("OPEN" if x.player_id is None else
                  "PASS" if x.player_id in in_hand else "WAIT")
        target = plan.assignment.get(x.label)
        row.update(status=status, waiting_on=target,
                   expected=round(plan.slot_value(x.label), 1))
        alts = ", ".join(short(p) for p in alternatives(x, plan, positions)[:3]) or "none"
        print(f"{x.label:<6} {name:<13} {x.score:6.1f}  {status:<6}  "
              f"{(short(target) if target else 'nobody'):<13} {plan.slot_value(x.label):5.1f}  "
              f"{depth_label(x, plan, positions):<5} {alts}")

    # 3. Priority: the weakest open slots are where solid scores should get locked.
    # UTIL is left out: it takes whoever's left over, but it's also the slot
    # that can catch a late big game from anyone, so it should stay open longest.
    open_slots = sorted((x for x in after if not x.locked and x.kind != "UTIL"),
                        key=lambda x: plan.slot_value(x.label))
    if open_slots:
        weakest = ", ".join(f"{x.label} ({plan.slot_value(x.label):.1f})" for x in open_slots[:3])
        print(f"\nWeakest open slots this week: {weakest}. Lock solid scores here first;"
              f"\nkeep UTIL open as long as you can for late big games from anyone.")

    # 4. Today: start the planned player if he plays; otherwise a free roll.
    # Each line says where the player comes from and who makes room (lineup.today_plan).
    if days and days[0] == now.date():
        playing_today = {g.player_id: g for g in future.get(now.date(), [])
                         if g.player_id not in locked_now}
        today = today_plan(after, plan, set(playing_today), values, positions)
        if today:
            print("\nToday's plan for open slots:")
            for m in today:
                print("  " + today_line(m, playing_today[m.player_id].opponent, short))

    print(f"\nWaiting on = the player planned for that slot; Exp = what you'd expect to lock"
          f" from him this week.\nEach player can only be locked once, so each slot waits on"
          f" a different player.\nLOCK = lock now (steps above). PASS = keep the slot open. "
          f"WAIT = his game hasn't happened. OPEN = empty.")

    # 5. Save this advice so `review` can check it once the games are played.
    def move_dict(m):
        return {"player_id": m.player_id, "from": m.from_label, "to": m.to_label,
                "score": m.score, "gain": m.gain}

    games_left: dict[str, list[str]] = {}  # player -> days he still had games
    for d in days:
        for g in future[d]:
            games_left.setdefault(g.player_id, []).append(d.isoformat())

    entry = {
        "time": now.isoformat(timespec="seconds"),
        "week": week_no,
        "week_start": week[0].isoformat(),
        "first_day": first_day.isoformat(),  # games from this day on were "still to come"
        "floor": floor,
        "target": lock_target,
        "slots": rows,
        "locks": [move_dict(m) for m in moves],
        "held": [move_dict(h) for h in held],
        "games_left": games_left,
        "names": {pid: short(pid) for pid in roster.get("players") or []},
    }
    run_id = advice_log.append(entry)
    print(f"\nSaved as advice #{run_id}. Disagree? python -m fantasy_assistant note {run_id} "
          f"[slot] your reason")
    return 0


def cmd_note(args: list[str]) -> int:
    """note                      -> list this week's saved advice
    note 7 PG locked him anyway -> attach a note to run #7 (slot is optional)
    note last too passive        -> 'last' means the most recent run"""
    from . import advice_log

    entries = advice_log.load()
    if not entries:
        print("No advice saved yet. Run `advise` first.")
        return 1
    if not args:
        latest_week = entries[-1]["week_start"]
        print(f"Saved advice for the week of {latest_week}:\n")
        for e in entries:
            if e["week_start"] == latest_week:
                when = _time_text(e["time"])
                notes = f"  [{len(e['notes'])} note(s)]" if e.get("notes") else ""
                print(f"  #{e['id']:<3} {when}  {advice_log.one_line(e)}{notes}")
        print("\nAdd a note: python -m fantasy_assistant note <number or 'last'> [slot] your reason")
        return 0

    run_id = entries[-1]["id"] if args[0].lower() == "last" else _int_or_none(args[0])
    entry = next((e for e in entries if e["id"] == run_id), None)
    if entry is None:
        print(f"No saved advice #{args[0]}. Run `note` with no arguments to see the list.")
        return 1
    rest = args[1:]
    slot_names = {r["slot"].lower(): r["slot"] for r in entry["slots"]}
    slot = slot_names.get(rest[0].lower()) if rest else None
    if slot:
        rest = rest[1:]
    text = " ".join(rest).strip()
    if not text:
        print("Write the note after the number, e.g.: note 7 PG locked him anyway, he was hot")
        return 1
    advice_log.add_note(entry["id"], text, slot)
    print(f"Note added to advice #{entry['id']}" + (f" ({slot})" if slot else "") + ".")
    return 0


def _int_or_none(text: str) -> int | None:
    try:
        return int(text.lstrip("#"))
    except ValueError:
        return None


def _time_text(iso: str) -> str:
    """'2026-10-29T14:05:00' -> 'Thu 10/29 2:05pm'."""
    from datetime import datetime

    t = datetime.fromisoformat(iso)
    return f"{t:%a %m/%d} {t.hour % 12 or 12}:{t:%M}{'am' if t.hour < 12 else 'pm'}"


def _points_by_day(pid: str, name: str, scoring: dict) -> dict:
    """date -> fantasy points for a player's games this season (refreshed within the hour)."""
    from datetime import datetime

    from . import gamelogs
    from .scoring import fantasy_points

    try:
        logs = gamelogs.season_games(pid, name, CURRENT_SEASON, is_current=True,
                                     max_age=60 * 60)
    except gamelogs.NoGameLogs as err:
        print(f"  (can't check {name}: {err})")
        return {}
    return {datetime.strptime(b["game_date"], "%b %d, %Y").date(): fantasy_points(b, scoring)
            for b in logs if b.get("game_date")}


def cmd_review(args: list[str]) -> int:
    """review      -> grade this week's saved advice (or the latest week with any)
    review all  -> every week"""
    from datetime import date, timedelta

    from . import advice_log, locks, slot_scores
    from .advice_log import IGNORED, PENDING, RIGHT, WRONG, SlotOutcome

    entries = advice_log.load()
    if not entries:
        print("No advice saved yet. Run `advise` first; each run is saved automatically.")
        return 1
    if not (args and args[0].lower() == "all"):
        latest_week = entries[-1]["week_start"]
        entries = [e for e in entries if e["week_start"] == latest_week]

    _, league, roster, players = _load_my_team()
    today = date.today()

    # How each slot stands now, scored the same way as advise: Sleeper's lineup,
    # our scores from box scores, and saved locks with their saved game.
    outcomes_by_week = {}
    for week_start, week_no in sorted({(e["week_start"], e["week"]) for e in entries}):
        monday = date.fromisoformat(week_start)
        saved = locks.load(monday)
        over = advice_log.week_is_over(week_start, today)
        outcomes = {}
        slots = _sleeper_slots(league, roster, week_no)
        by_player = _week_games([s.player_id for s in slots]
                                + [lk["player_id"] for lk in saved.values()],
                                [monday + timedelta(days=i) for i in range(7)],
                                league["scoring_settings"], players)
        slot_scores.rescore_locks(saved, by_player)
        slot_scores.score_open_slots(slots, by_player)
        locks.lock_slots(slots, list(saved), saved, kept=True)
        for s in slots:
            is_locked = s.locked and bool(s.player_id)
            outcomes[s.label] = SlotOutcome(s.player_id, s.score, is_locked, over or is_locked)
        outcomes_by_week[week_start] = outcomes

    # Box scores for everyone the advice was about.
    print("Checking game logs on Sleeper...")
    called = {c.player_id for e in entries for c in advice_log.calls_in(e)}
    points = {pid: _points_by_day(pid, _player_name(players.get(pid, {})),
                                  league["scoring_settings"])
              for pid in called}

    calls = advice_log.review(entries, outcomes_by_week, points, today)
    by_run: dict[int, list] = {}
    for c in calls:
        by_run.setdefault(c.run_id, []).append(c)

    for e in entries:
        print(f"\n#{e['id']}  {_time_text(e['time'])}  (week {e['week']})")
        mine = by_run.get(e["id"], [])
        if not mine:
            print("  nothing new (same calls as an earlier run, or no scores in hand)")
        for c in mine:
            what = f"{c.player} {c.score:.1f}" + (f" in {c.lock_slot}" if c.lock_slot else f" ({c.slot})")
            later = ", ".join(g.text() for g in c.later) or "no games left"
            repeat = f"  [repeated in #{', #'.join(map(str, c.also_in))}]" if c.also_in else ""
            print(f"  {c.kind:<4}  {what:<26} later: {later}")
            print(f"        -> {c.verdict}: {c.detail}{repeat}")
        for n in e.get("notes", []):
            print(f"  note{' (' + n['slot'] + ')' if n.get('slot') else ''}: {n['text']}")

    # Summary
    counts = advice_log.tally(calls)
    print("\nHow the advice did (each call counted once):")
    meaning = {"LOCK": "graded on his next game",
               "PASS": "graded on how the slot finished",
               "HELD": "held back by the floor; right = the floor paid off"}
    total_right = total_graded = 0
    for kind in ("LOCK", "PASS", "HELD"):
        k = counts.get(kind)
        if not k:
            continue
        right, graded = k.get(RIGHT, 0), k.get(RIGHT, 0) + k.get(WRONG, 0)
        total_right, total_graded = total_right + right, total_graded + graded
        score = f"{right} of {graded} right ({right / graded:.0%})" if graded else "none graded yet"
        extras = [f"{k[v]} {label}" for v, label in
                  ((PENDING, "pending"), (IGNORED, "you went the other way"),
                   (advice_log.FREE, "free (no games left)")) if k.get(v)]
        print(f"  {kind:<4}  {score}" + (f"; {', '.join(extras)}" if extras else "")
              + f"   ({meaning[kind]})")
    if total_graded:
        print(f"  Overall: {total_right} of {total_graded} graded calls right "
              f"({total_right / total_graded:.0%})")

    pairs = advice_log.forecast_check(entries, outcomes_by_week)
    if pairs:
        diff = sum(actual - exp for exp, actual in pairs) / len(pairs)
        print(f"  Forecast check: {len(pairs)} finished slot(s) ended {abs(diff):.1f} points "
              f"{'above' if diff >= 0 else 'below'} the plan's Exp on average")
    n_notes = sum(len(e.get("notes", [])) for e in entries)
    if n_notes:
        print(f"  Notes: {n_notes} (shown under their runs)")
    print("\nPASS and HELD calls stay pending until the slot is locked (as saved by `advise`) "
          "or the week ends.")
    return 0


def cmd_merge(args: list[str]) -> int:
    """Merge another folder's lock files and advice log into this computer's
    state folder, e.g. `merge data` after setting STATE_DIR=notes/state."""
    if len(args) != 1:
        print("Usage: python -m fantasy_assistant merge <folder>   (e.g. merge data)")
        return 1
    source, target = state.state_dir(args[0]), state.state_dir(os.getenv("STATE_DIR"))
    if source.resolve() == target.resolve():
        print(f"{source} is already where your locks and advice log are saved. Set STATE_DIR "
              f"in .env to the folder you want them in, then merge the old folder into it.")
        return 1
    if not source.is_dir():
        print(f"No folder at {source}.")
        return 1
    print(f"Merging {source} into {target}:")
    for line in state.merge_folder(source, target) or ["nothing to merge"]:
        print(f"  {line}")
    print(f"\nThe originals in {source} weren't changed. Once this looks right, you can "
          f"delete the locks_*.json and advice_log.json files there.")
    return 0


def main() -> int:
    load_dotenv()
    state.use(state.state_dir(os.getenv("STATE_DIR")))  # where locks and the advice log live
    commands = {"roster": cmd_roster, "profiles": cmd_profiles, "week": cmd_week,
                "advise": cmd_advise}
    with_args = {"review": cmd_review, "note": cmd_note,  # these take extra words
                 "merge": cmd_merge}
    parser = argparse.ArgumentParser(prog="fantasy_assistant")
    parser.add_argument("command", nargs="?", default="roster",
                        choices=list(commands) + list(with_args))
    parser.add_argument("args", nargs="*",
                        help="for note: <number> [slot] text; for review: all; for merge: <folder>")
    args = parser.parse_args()
    if args.command in with_args:
        return with_args[args.command](args.args)
    return commands[args.command]()


if __name__ == "__main__":
    sys.exit(main())
