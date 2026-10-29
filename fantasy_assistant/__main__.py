"""Command-line entry point. Run from the project folder:

    python -m fantasy_assistant roster     # your league and roster
    python -m fantasy_assistant profiles   # each player's average, swing, and lock-in bars
    python -m fantasy_assistant week       # who plays which day this week
    python -m fantasy_assistant advise     # LOCK or PASS for every lineup slot (saved to a log)
    python -m fantasy_assistant review     # how the saved advice turned out
    python -m fantasy_assistant note 7 PG locked him anyway   # note on saved advice #7
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


def _sleeper_slots(league, roster, week_no: int) -> list:
    """Your starting slots for a week, with each slot's player and score from Sleeper."""
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

    from . import advice_log, locks, schedule
    from .lineup import (DEFAULT_MIN_LOCK, FutureGame, alternatives, apply_moves, can_fill,
                         depth_label, pass_values, plan_week, player_values,
                         recommend_locks)
    from .lockin import GameForecast
    from .status import play_chances

    _, league, roster, players = _load_my_team()
    week_no = sleeper.get_state("nba")["week"]
    slots = _sleeper_slots(league, roster, week_no)

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
        return _short_name(players.get(pid, {}))

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
        "min_lock": min_lock,
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


def _points_by_day(name: str, scoring: dict) -> dict:
    """date -> fantasy points for a player's games this season (fresh from NBA.com)."""
    from datetime import datetime

    from . import nba_stats
    from .scoring import fantasy_points

    nba_id = nba_stats.find_nba_id(name)
    if nba_id is None:
        print(f"  (can't check {name}: couldn't match to NBA.com)")
        return {}
    try:
        logs = nba_stats.game_logs(nba_id, CURRENT_SEASON, is_current=True, max_age=60 * 60)
    except nba_stats.NBAStatsUnavailable:
        print(f"  (can't check {name}: NBA.com refused the connection)")
        return {}
    return {datetime.strptime(b["game_date"], "%b %d, %Y").date(): fantasy_points(b, scoring)
            for b in logs if b.get("game_date")}


def cmd_review(args: list[str]) -> int:
    """review      -> grade this week's saved advice (or the latest week with any)
    review all  -> every week"""
    from datetime import date

    from . import advice_log, locks
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

    # How each slot stands now: Sleeper's score plus whether it's saved as locked.
    outcomes_by_week = {}
    for week_start, week_no in sorted({(e["week_start"], e["week"]) for e in entries}):
        saved = locks.load(date.fromisoformat(week_start))
        over = advice_log.week_is_over(week_start, today)
        outcomes = {}
        for s in _sleeper_slots(league, roster, week_no):
            is_locked = bool(s.player_id) and saved.get(s.label) == s.player_id
            outcomes[s.label] = SlotOutcome(s.player_id, s.score, is_locked, over or is_locked)
        outcomes_by_week[week_start] = outcomes

    # Box scores for everyone the advice was about.
    print("Checking game logs on NBA.com...")
    called = {c.player_id for e in entries for c in advice_log.calls_in(e)}
    points = {pid: _points_by_day(_player_name(players.get(pid, {})), league["scoring_settings"])
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
               "HELD": "held back by your minimum; right = the minimum paid off"}
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


def main() -> int:
    commands = {"roster": cmd_roster, "profiles": cmd_profiles, "week": cmd_week,
                "advise": cmd_advise}
    with_args = {"review": cmd_review, "note": cmd_note}  # these take extra words
    parser = argparse.ArgumentParser(prog="fantasy_assistant")
    parser.add_argument("command", nargs="?", default="roster",
                        choices=list(commands) + list(with_args))
    parser.add_argument("args", nargs="*", help="for note: <number> [slot] text; for review: all")
    args = parser.parse_args()
    if args.command in with_args:
        return with_args[args.command](args.args)
    return commands[args.command]()


if __name__ == "__main__":
    sys.exit(main())
