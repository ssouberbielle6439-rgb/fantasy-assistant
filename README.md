# fantasy-assistant

A fantasy basketball assistant built around Sleeper's **Lock-In Mode**, where only one game per player counts each week.

After each game you have to decide: **lock** this score, or **pass** and hope a later game is better? Passing is permanent, so this is a classic *optimal stopping* problem. This project solves it with math instead of gut feel, using my real league's settings and scoring.

> Status: early development. League connection, scoring, lock-in math, and player profiles from real game logs are in place.

## How the lock-in math works

Work backward from the player's last game of the week:

1. **Last game:** you take whatever he scores, so its value is just what you expect him to score (counting the chance he sits).
2. **Earlier games:** lock only if the score beats the *value of waiting*, meaning what you'd expect to end up with by passing and following this same rule on the games that are left.

Example: a player averaging 40 fantasy points, with typical swings of about 10 and a 95% chance to play each game.

| Games left after this one | Lock if he scored at least |
|---|---|
| 1 | 38.0 |
| 2 | 42.8 |
| 3 | 45.4 |

The bar rises when more games are left, because more chances to beat a score means you can afford to be pickier.

## Project layout

```
fantasy_assistant/
  sleeper.py    # talks to Sleeper's free API (league, rosters, players)
  scoring.py    # turns a box score into fantasy points using MY league's scoring
  lockin.py     # the lock-or-pass engine
  nba_stats.py  # downloads game logs from NBA.com (cached in data/)
  profile.py    # turns game logs into each player's average, swing, and chance to play
  schedule.py   # the NBA season schedule (who plays which day)
  status.py     # chance to play from Sleeper injury status (Out = next game only)
  __main__.py   # command-line entry point
tests/          # automated tests for scoring and lock-in math
```

## Setup (Windows, PowerShell)

```powershell
cd $HOME\code\projects\fantasy-assistant
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then open .env and fill in your values
python -m pytest            # run the tests
python -m fantasy_assistant roster    # print your league and roster
python -m fantasy_assistant profiles  # each player's numbers and lock-in bars
python -m fantasy_assistant week      # who plays which day this week
```

## Roadmap

- [x] Connect to a Sleeper league and read its scoring
- [x] Score any stat line with the league's own settings
- [x] Lock-in thresholds via backward induction
- [x] Pull real game logs and estimate each player's average, swing, and chance to play
- [x] Know each player's remaining games this week (NBA schedule)
- [x] Chance to play from Sleeper injury status, game by game
- [ ] Weekly simulator: lock or pass for each lineup slot, using the whole roster
- [ ] Weight this season's games more heavily as the season goes on
- [ ] Read injury news with Gemini to sharpen chance-to-play
- [ ] Store data in DuckDB
- [ ] Web page (FastAPI) showing lock or pass for every player
- [ ] Backtest on last season: does the engine beat gut feel?

## Data and credits

- League data: [Sleeper API](https://docs.sleeper.com/) (free, read-only)
- Game logs: NBA.com via [nba_api](https://github.com/swar/nba_api)
- License: MIT
