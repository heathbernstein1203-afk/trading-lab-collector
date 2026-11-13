# Trading Lab bot (paper money)

Runs on GitHub Actions every 30 minutes, 24/7, with no laptop and no Claude app needed.

Each run:
1. Saves Kalshi, Polymarket and ESPN/DraftKings sports prices and daily stock closes to `data/`.
2. Runs the paper-trading bot (`bot.py`) with a fake $1,000 bankroll:
   - **line-edge**: buys when the sportsbook's no-vig price beats Kalshi's price by 3¢+ after fees
   - **fav-maker**: limit orders on favorites (70–92¢) that the sportsbook agrees with
   - **no-longshot**: limit orders on NO for contracts priced 10¢ or less
   It fills or cancels its limit orders, records the last price before each game (closing line value),
   and settles finished games from Kalshi's results. Everything is in `state/paper.json`.
3. Optionally copies new bets to your **Kalshi demo** account (fake money, up to $100/day), if you add
   your demo API key as repo secrets. The code only talks to `demo-api.kalshi.co`.

Settings live in `config.json`. Claude's daily review reads the results and tunes them.

## Setup
1. Create a repo named `trading-lab-collector` and upload everything in this folder (including the
   hidden `.github` folder: press Cmd+Shift+. in Finder to see it).
   A **public** repo gets unlimited free Actions minutes. A private one gets 2,000 minutes a month,
   which may run out at every 30 minutes; if so, change the cron line to `"7 * * * *"` (hourly).
2. Settings, Actions, General: set Workflow permissions to **Read and write**.
3. Actions tab, "Trading Lab bot", **Run workflow** to start it.
4. Optional, for the Kalshi demo: on demo.kalshi.co go to Account, API keys, create a key, and save the
   private key file. In the repo: Settings, Secrets and variables, Actions, add `KALSHI_DEMO_KEY_ID`
   (the key ID) and `KALSHI_DEMO_PRIVATE_KEY` (the whole private key text). Never use a real-money key.

Tests: `python tests/test_parse.py`, `python tests/test_bot.py`, `python tests/test_demo_sign.py`.
