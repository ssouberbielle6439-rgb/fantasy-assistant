"""Command-line entry point. Run from the project folder:

    python -m fantasy_assistant roster     # your league and roster
    python -m fantasy_assistant profiles   # each player's average, swing, and lock-in bars
    python -m fantasy_assistant week       # who plays which day this week
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


def main() -> int:
    commands = {"roster": cmd_roster, "profiles": cmd_profiles, "week": cmd_week}
    parser = argparse.ArgumentParser(prog="fantasy_assistant")
    parser.add_argument("command", nargs="?", default="roster", choices=list(commands))
    args = parser.parse_args()
    return commands[args.command]()


if __name__ == "__main__":
    sys.exit(main())
