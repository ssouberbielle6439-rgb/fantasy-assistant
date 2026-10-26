"""Step 1 check: print your league and roster.

Run from the project folder:
    python -m fantasy_assistant
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from . import sleeper


def main() -> int:
    load_dotenv()
    username = os.getenv("SLEEPER_USERNAME")
    league_id = os.getenv("SLEEPER_LEAGUE_ID")
    if not username or not league_id:
        print("Set SLEEPER_USERNAME and SLEEPER_LEAGUE_ID in your .env file.")
        return 1

    user = sleeper.get_user(username)
    league = sleeper.get_league(league_id)
    print(f"League: {league['name']}  ({league['sport'].upper()} {league['season']}, "
          f"{league['total_rosters']} teams)")
    print(f"Lineup: {', '.join(p for p in league['roster_positions'] if p != 'BN')}")

    roster = sleeper.find_my_roster(sleeper.get_rosters(league_id), user["user_id"])
    if roster is None:
        print(f"Couldn't find a roster owned by {username} in this league.")
        return 1

    players = sleeper.get_players("nba")
    print(f"\n{user['display_name']}'s roster:")
    for pid in roster.get("players") or []:
        p = players.get(pid, {})
        name = p.get("full_name") or f"{p.get('first_name', '?')} {p.get('last_name', '')}"
        positions = "/".join(p.get("fantasy_positions") or [p.get("position") or "?"])
        team = p.get("team") or "FA"
        status = p.get("injury_status") or ""
        print(f"  {name:<26} {positions:<10} {team:<4} {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
