# fantasy-assistant

A fantasy basketball assistant built around Sleeper's **Lock-In Mode**, where only one game per player counts each week.

After each game you have to decide: **lock** this score, or **pass** and hope a later game is better? Passing is permanent, so this is a classic *optimal stopping* problem. This project solves it with math instead of gut feel, using my real league's settings and scoring.

> Status: early development. The `advise` command gives LOCK / PASS for every lineup slot using the whole roster, this week's schedule, and injury statuses.

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

## How the lineup advice works

In my league, each of the 9 starting slots ends the week with one game. After a game you can lock it, or bench the player and give the slot to someone else on the roster. So the real question for a slot isn't "will this player do better later?" but "will anyone who can fill this slot do better later?"

The app answers two questions: **whether** to lock a score, and **where**. A player who qualifies for several slots (say PG/SG) should be locked into the slot where he's least needed later, so flexible slots like UTIL stay open for late big games.

The key modeling point: **each player can only be locked once per week**, so each open slot is really waiting on one player.

1. **Value each player's week.** For every available player, the lock-in math gives the score we'd expect to end up locking from his remaining games (more games and a higher ceiling mean more value).
2. **Match players to slots.** Pair players with open slots, one player per slot and respecting positions, to maximize the team's expected total. That's the classic *assignment problem*, solved exactly with the Hungarian algorithm (tested against brute force).
3. **Choose locks for the whole lineup.** For every player with a score in hand, try locking him into each slot he's eligible for and compare the team's total (his score plus the best matching of everyone else) against passing. Take the best lock and repeat. A player with no games left gets locked in his best slot for free, since passing can't gain anything.
4. **Respect the manager's rule of thumb.** Scores under a configurable minimum (`MIN_LOCK_POINTS`, default 25) are only locked when nobody else can fill the slot; otherwise the app shows the lock it would have made and leaves the call to you.
5. **Explain the moves in Sleeper terms.** A straight swap only works if the other player is eligible for the slot being vacated, and benching a player clears his last game, so the instructions follow those rules.

It also suggests **free rolls**: if a slot's planned player doesn't play today, drop in a spare who does. If he goes off, lock him; if not, nothing is lost.

## Checking the advice

Every `advise` run is saved to `data/advice_log.json`, and `review` grades it once the games are played:

- **LOCK** is graded on the player's *next* game, because passing would have meant that game replaced the score. Right if it was lower, or he sat.
- **PASS** is graded on how the slot finished: right if it ended at or above the score passed on.
- **HELD** (a lock the minimum blocked) is graded like a PASS, which shows whether the minimum pays off.

If I go against the advice it's marked separately instead of graded, and `note` lets me record why. A forecast check also compares each slot's expected value with what it really ended at.

An earlier version counted the same player's games toward several slots at once, which made closing a slot look cheap and led to recommending a 15-point lock. Real-league feedback caught it, and a test now guards against it.

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
  lineup.py     # lineup slots, who can fill them, and each slot's lock bar
  locks.py      # remembers which slots are locked this week
  advice_log.py # saves every piece of advice and grades it once the games are played
  __main__.py   # command-line entry point
tests/          # automated tests, including regression tests from real league situations
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
python -m fantasy_assistant advise    # LOCK or PASS for every lineup slot (saved to a log)
python -m fantasy_assistant review    # how this week's saved advice turned out
python -m fantasy_assistant note 3 PG locked him anyway   # note on saved advice #3
```

## Roadmap

- [x] Connect to a Sleeper league and read its scoring
- [x] Score any stat line with the league's own settings
- [x] Lock-in thresholds via backward induction
- [x] Pull real game logs and estimate each player's average, swing, and chance to play
- [x] Know each player's remaining games this week (NBA schedule)
- [x] Chance to play from Sleeper injury status, game by game
- [x] Lock or pass for each lineup slot, using the whole roster and saved locks
- [x] Advice log: save every recommendation and grade it against what happened
- [ ] Matchup-aware advice: play for win probability, not just points
- [ ] AI chat: ask questions in plain English (Gemini understands, the math decides)
- [ ] Weight this season's games more heavily as the season goes on
- [ ] Read injury news with Gemini to sharpen chance-to-play
- [ ] Store data in DuckDB
- [ ] Web page (FastAPI) showing lock or pass for every player
- [ ] Backtest on last season: does the engine beat gut feel?

## Data and credits

- League data: [Sleeper API](https://docs.sleeper.com/) (free, read-only)
- Game logs: NBA.com via [nba_api](https://github.com/swar/nba_api)
- License: MIT
