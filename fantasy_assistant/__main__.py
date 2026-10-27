"""Command-line entry point. Run from the project folder:

    python -m fantasy_assistant roster     # your league and roster
    python -m fantasy_assistant profiles   # each player's average, swing, and lock-in bars
    python -m fantasy_assistant week       # who plays which day this week
    python -m fantasy_assistant advise     # LOCK or PASS for every lineup slot
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from . import sleeper

CURRENT_SEASON = "2026-27"
LAST_SEASON = "2025-26"


def _player_name(p: dict) -> str:
    return p.get("full_name") or f"{p.get('first_name', '?')} {p.get('last_name', '')}".strip()


def _load_my_team():
    """League, your roster's player ids, and Sleeper's player info."""
    load_dotenv()
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
    from . import nba_stats
    from .lockin import value_of_waiting
    from .profile import build_profile

    _, league, roster, players = _load_my_team()
    player_ids = roster.get("players") or []
    scoring = league["scoring_settings"]

    print("Pulling game logs from NBA.com (first run takes a minute)...\n")
    print(f"{'Player':<24} {'Games':>5} {'Avg':>6} {'Swing':>6} {'Plays':>6}"
          f"   Lock if at least, with 1 / 2 / 3 games left")
    unmatched = []
    failed = 0
    for pid in player_ids:
        name = _player_name(players.get(pid, {}))
        nba_id = nba_stats.find_nba_id(name)
        if nba_id is None:
            unmatched.append(name)
            continue
        try:
            this_season = nba_stats.game_logs(nba_id, CURRENT_SEASON, is_current=True)
            last_season = nba_stats.game_logs(nba_id, LAST_SEASON, is_current=False)
        except nba_stats.NBAStatsUnavailable:
            print(f"{name:<24}  NBA.com refused the connection, try again later")
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
    if unmatched:
        print("\nCouldn't match these names to NBA.com (add them to NAME_OVERRIDES "
              "in nba_stats.py):")
        for name in unmatched:
            print(f"  {name}")
    return 0


def cmd_week() -> int:
    from datetime import date

    from . import schedule
    from .status import play_chances

    _, _, roster, players = _load_my_team()
    games = schedule.season_games(CURRENT_SEASON)
    today = date.today()
    week = schedule.fantasy_week(today)
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
        left = sum(1 for d in slate if d >= today)
        chance = play_chances(status, left)
        next_txt = f"{chance[0]:.0%} to play" if chance else "no games left"
        label = (status or "") + (" IR" if pid in injured_reserve else "")
        print(f"{_player_name(p)[:22]:<22} {label:<7}{cells}   {left:>4}  {next_txt}")

    counts = "".join(f"{(str(per_day[d]) if d >= today else ''):>10}" for d in week)
    print(f"\n{'Playing (not on IR)':<30}{counts}")
    return 0


def _build_profiles(player_ids, players, scoring) -> dict:
    """Sleeper player id -> PlayerProfile, from cached NBA.com game logs."""
    from . import nba_stats
    from .profile import build_profile

    profiles = {}
    for pid in player_ids:
        name = _player_name(players.get(pid, {}))
        nba_id = nba_stats.find_nba_id(name)
        if nba_id is None:
            print(f"  (skipping {name}: couldn't match to NBA.com)")
            continue
        try:
            this_season = nba_stats.game_logs(nba_id, CURRENT_SEASON, is_current=True)
            last_season = nba_stats.game_logs(nba_id, LAST_SEASON, is_current=False)
        except nba_stats.NBAStatsUnavailable:
            print(f"  (skipping {name}: NBA.com refused the connection)")
            continue
        prof = build_profile(name, this_season + last_season, scoring, len(last_season))
        if prof:
            profiles[pid] = prof
    return profiles


def _ask_locks(slots, saved: dict[str, str]) -> list[str]:
    from .locks import parse_answer

    labels = [s.label for s in slots]
    # A saved lock only counts if the same player is still in that slot.
    still_valid = [lab for lab, pid in saved.items()
                   if any(s.label == lab and s.player_id == pid for s in slots)]
    hint = ", ".join(still_valid) if still_valid else "none"
    while True:
        try:
            answer = input(f"\nWhich slots are locked? (e.g. PG F UTIL2, 'none', "
                           f"or Enter to keep: {hint}) > ")
        except EOFError:
            answer = ""
        chosen = parse_answer(answer, labels, still_valid)
        if chosen is not None:
            return chosen
        print(f"Didn't recognize that. Slot names are: {' '.join(labels)}")


def cmd_advise() -> int:
    from datetime import datetime, timedelta

    from . import locks, schedule
    from .lineup import (DEFAULT_MIN_LOCK, FutureGame, Slot, alternatives, apply_moves, can_fill,
                         depth_label, pass_values, plan_week, player_values,
                         recommend_locks, slot_labels)
    from .lockin import GameForecast
    from .status import play_chances

    _, league, roster, players = _load_my_team()
    league_id = league["league_id"]
    week_no = sleeper.get_state("nba")["week"]
    mine = next(m for m in sleeper.get_matchups(league_id, week_no)
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

    now = datetime.now()
    week = schedule.fantasy_week(now.date())
    saved = locks.load(week[0])

    print(f"Week {week_no}: your lineup right now (scores from Sleeper)\n")
    for s in slots:
        name = _player_name(players.get(s.player_id, {})) if s.player_id else "(empty)"
        mark = "  [saved as locked]" if saved.get(s.label) == s.player_id and s.player_id else ""
        print(f"  {s.label:<6} {name:<24} {s.score:6.1f}{mark}")
    locked = _ask_locks(slots, saved)
    for s in slots:
        s.locked = s.label in locked
    locks.save(week[0], {s.label: s.player_id for s in slots if s.locked and s.player_id})

    # Who can still fill slots this week: not on IR, not already locked in.
    unavailable = set(roster.get("reserve") or []) | {s.player_id for s in slots if s.locked}
    candidates = [pid for pid in roster.get("players") or [] if pid not in unavailable]
    print("\nLoading player profiles (cached game logs)...")
    profiles = _build_profiles(candidates, players, league["scoring_settings"])

    # Games still to come. Today's games count if it's before 7pm (tip-off).
    first_day = now.date() if now.hour < 19 else now.date() + timedelta(days=1)
    days = [d for d in week if d >= first_day]
    games = schedule.season_games(CURRENT_SEASON)
    future: dict = {d: [] for d in days}
    positions = {pid: set(players.get(pid, {}).get("fantasy_positions")
                          or [players.get(pid, {}).get("position") or ""])
                 for pid in roster.get("players") or []}
    for pid, prof in profiles.items():
        p = players.get(pid, {})
        team = schedule.nba_team(p.get("team"))
        slate = schedule.team_games(games, team, days) if team else {}
        chances = play_chances(p.get("injury_status"), len(slate))
        for (day, game), chance in zip(sorted(slate.items()), chances):
            fc = GameForecast(prof.average, prof.swing, chance)
            future[day].append(FutureGame(pid, day, fc, game.opponent_of(team)))

    def short(pid):
        p = players.get(pid, {})
        first, last = p.get("first_name") or "", p.get("last_name") or ""
        return f"{first[:1]}. {last}" if first and last else _player_name(p)

    min_lock = float(os.getenv("MIN_LOCK_POINTS") or DEFAULT_MIN_LOCK)
    values = player_values(future)

    # 1. Which scores in hand to lock, and in which slot.
    moves, held = recommend_locks(slots, values, positions, min_lock)
    after, steps = apply_moves(slots, moves, positions, short)
    if moves:
        print("\nRecommended locks (in this order):")
        for step in steps:
            print(f"  - {step}")
        gain = sum(m.gain for m in moves)
        print(f"  These add about {gain:.1f} expected points to your week vs. passing.")
    else:
        print("\nNo locks recommended right now: every score in hand is worth passing on.")
    for h in held:
        print(f"  (Held back by your {min_lock:.0f}-point minimum: locking {short(h.player_id)}'s "
              f"{h.score:.1f} in {h.to_label} would add about {h.gain:.1f}. Your call.)")

    # 2. The plan for the rest of the week: which player each open slot is waiting on.
    locked_now = {m.player_id for m in moves}
    remaining = {p: v for p, v in values.items() if p not in locked_now}
    plan = plan_week(after, pass_values(after, remaining), positions)
    newly = {m.to_label for m in moves}
    in_hand = {x.player_id for x in slots if not x.locked and x.player_id and x.score > 0}

    print(f"\n{'Slot':<6} {'Now':<13} {'Score':>6}  {'Status':<6}  {'Waiting on':<13} {'Exp':>5}  "
          f"{'Depth':<5} Alternates")
    for x in after:
        name = short(x.player_id) if x.player_id else "(empty)"
        if x.locked:
            print(f"{x.label:<6} {name:<13} {x.score:6.1f}  {'LOCK' if x.label in newly else 'LOCKED'}")
            continue
        status = ("OPEN" if x.player_id is None else
                  "PASS" if x.player_id in in_hand else "WAIT")
        target = plan.assignment.get(x.label)
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
    if days and days[0] == now.date():
        playing_today = {g.player_id: g for g in future.get(now.date(), [])}
        planned = {p for p in plan.assignment.values() if p}
        spares = sorted((p for p in playing_today if p not in planned and p not in locked_now),
                        key=lambda p: values.get(p, 0), reverse=True)
        lines = []
        for x in after:
            if x.locked:
                continue
            target = plan.assignment.get(x.label)
            if target in playing_today:
                lines.append(f"  {x.label:<6} <- {short(target)} ({playing_today[target].opponent})")
                continue
            spare = next((p for p in spares if can_fill(x.kind, positions.get(p, set()))), None)
            if spare:
                spares.remove(spare)
                later = f", otherwise put {short(target)} in before his game" if target else ""
                lines.append(f"  {x.label:<6} <- {short(spare)} ({playing_today[spare].opponent})"
                             f"  free roll: lock him if he goes off{later}")
        if lines:
            print("\nToday's plan for open slots:")
            print("\n".join(lines))

    print(f"\nWaiting on = the player planned for that slot; Exp = what you'd expect to lock"
          f" from him this week.\nEach player can only be locked once, so each slot waits on"
          f" a different player.\nLOCK = lock now (steps above). PASS = keep the slot open. "
          f"WAIT = his game hasn't happened. OPEN = empty.")
    return 0


def main() -> int:
    commands = {"roster": cmd_roster, "profiles": cmd_profiles, "week": cmd_week,
                "advise": cmd_advise}
    parser = argparse.ArgumentParser(prog="fantasy_assistant")
    parser.add_argument("command", nargs="?", default="roster", choices=list(commands))
    args = parser.parse_args()
    return commands[args.command]()


if __name__ == "__main__":
    sys.exit(main())
