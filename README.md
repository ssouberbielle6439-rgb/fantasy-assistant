# fantasy-assistant

A fantasy basketball assistant built around Sleeper's **Lock-In Mode**, where only one game per player counts each week.

After each game you have to decide: **lock** this score, or **pass** and hope a later game is better? Passing is permanent, so this is a classic *optimal stopping* problem. This project solves it with math instead of gut feel, using my real league's settings and scoring.

> Status: early development. Step 1 (league connection, scoring, lock-in math) is in place.

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
  __main__.py   # prints my league and roster (step 1 check)
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
python -m fantasy_assistant # print your league and roster
```

## Roadmap

- [x] Connect to a Sleeper league and read its scoring
- [x] Score any stat line with the league's own settings
- [x] Lock-in thresholds via backward induction
- [ ] Pull real game logs and estimate each player's mean, spread, and chance to play
- [ ] Store data in DuckDB
- [ ] Web page (FastAPI) showing lock or pass for every player
- [ ] Plain-English explanations with Gemini
- [ ] Backtest on last season: does the engine beat gut feel?

## Data and credits

- League data: [Sleeper API](https://docs.sleeper.com/) (free, read-only)
- License: MIT
