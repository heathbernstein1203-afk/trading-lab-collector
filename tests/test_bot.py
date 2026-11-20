import os, sys, json, tempfile, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
d = tempfile.mkdtemp()
os.environ.update(BOT_STATE=d + "/paper.json", BOT_DEMO_STATE=d + "/demo.json", DATA_DIR=d + "/data",
                  BOT_DATA_STATE=d + "/data.json", BOT_OTHER_STATE=d + "/other.json", BOT_WILD_STATE=d + "/wild.json",
                  BOT_PARLAY_STATE=d + "/parlay.json", BOT_CATS_STATE=d + "/cats.json")
import collector as col, bot

now = dt.datetime(2026, 11, 15, 12, 0, tzinfo=dt.timezone.utc)
start = "2026-11-15T18:00Z"
def krow(t, ev, team, bid, ask, vol=50000, status="active", result=""):
    return ["T", t, ev, team, bid, ask, ask, vol, 1000, status, result, "2026-11-15T21:00:00Z", ""]
def erow(away, an, home, hn, fa, fh):
    return ["T", "NFL", "1", start, "pre", away, an, home, hn, "", "", fa, fh, "", "", "", "", ""]

# Game A: Kalshi ATL ask 0.33 but sportsbook says 0.384 -> line-edge YES ATL
# Game B: favorite DET bid 0.80 ask 0.82, fair 0.83 -> fav-maker limit 0.81
# Game C: longshot TEN ask 0.06, fair 0.05 -> no-longshot NO limit 0.95
k1 = [krow("KXNFLGAME-26NOV15KCATL-ATL", "E1", "Atlanta", "0.32", "0.33"), krow("KXNFLGAME-26NOV15KCATL-KC", "E1", "Kansas City", "0.66", "0.68"),
      krow("KXNFLGAME-26NOV15NEDET-DET", "E2", "Detroit", "0.80", "0.82"), krow("KXNFLGAME-26NOV15NEDET-NE", "E2", "New England", "0.17", "0.19"),
      krow("KXNFLGAME-26NOV15JACTEN-TEN", "E3", "Tennessee", "0.05", "0.06"), krow("KXNFLGAME-26NOV15JACTEN-JAC", "E3", "Jacksonville", "0.93", "0.95")]
e1 = [erow("KC", "Kansas City", "ATL", "Atlanta", 0.616, 0.384), erow("NE", "New England", "DET", "Detroit", 0.17, 0.83),
      erow("JAX", "Jacksonville", "TEN", "Tennessee", 0.95, 0.05)]
col.get_json = lambda url, tries=3: {"markets": []}
cfg = json.load(open("config.json"))
bot.run(now, k1, e1)
st = json.load(open(d + "/paper.json"))
strat = sorted((b["strategy"], b["status"], b["side"]) for b in st["bets"])
print(strat)
assert strat == [("fav-maker", "pending", "yes"), ("line-edge", "open", "yes"), ("no-longshot", "pending", "no")], strat
le = [b for b in st["bets"] if b["strategy"] == "line-edge"][0]
assert le["qty"] >= 1 and le["fee"] > 0 and abs(st["cash"] - (1000 - le["cost"] - le["fee"])) < 0.01

# run 2: DET ask hits 0.81 (fill), TEN yes bid rises to 0.06 -> NO ask 0.94 (fill); ATL drifts to 0.36
k2 = [krow("KXNFLGAME-26NOV15KCATL-ATL", "E1", "Atlanta", "0.35", "0.37"), krow("KXNFLGAME-26NOV15KCATL-KC", "E1", "Kansas City", "0.63", "0.65"),
      krow("KXNFLGAME-26NOV15NEDET-DET", "E2", "Detroit", "0.79", "0.81"), krow("KXNFLGAME-26NOV15NEDET-NE", "E2", "New England", "0.19", "0.21"),
      krow("KXNFLGAME-26NOV15JACTEN-TEN", "E3", "Tennessee", "0.06", "0.07"), krow("KXNFLGAME-26NOV15JACTEN-JAC", "E3", "Jacksonville", "0.93", "0.94")]
bot.run(now + dt.timedelta(hours=1), k2, e1)
st = json.load(open(d + "/paper.json"))
assert all(b["status"] == "open" for b in st["bets"]), [(b["strategy"], b["status"]) for b in st["bets"]]
assert len(st["bets"]) == 3, "no double bets on same game"

# run 3: after start -> close recorded; settle: ATL yes wins, DET loses, TEN result no (NO wins)
res = {"KXNFLGAME-26NOV15KCATL-ATL": "yes", "KXNFLGAME-26NOV15NEDET-DET": "no", "KXNFLGAME-26NOV15JACTEN-TEN": "no"}
col.get_json = lambda url, tries=3: {"markets": [{"ticker": t, "result": r} for t, r in res.items()]}
bot.run(now + dt.timedelta(hours=7), [], [])
st = json.load(open(d + "/paper.json"))
for b in st["bets"]:
    print(b["strategy"], b["side"], b["status"], "price", b["price"], "close", b["close"], "pnl", b["pnl"])
assert {b["strategy"]: b["status"] for b in st["bets"]} == {"line-edge": "won", "fav-maker": "lost", "no-longshot": "won"}
assert all(b["close"] is not None for b in st["bets"])
eq = bot.equity(st)
assert abs(eq - (1000 + sum(b["pnl"] for b in st["bets"]))) < 0.02, (eq, st["cash"])
print("bot simulation passed; equity", round(eq, 2))

# Nov 20 fix: 'Georgia' is a prefix of 'Georgia Tech'; codes must decide which side gets which price.
def mk(t, ev, team):
    d = dict(zip(col.KALSHI_HDR, krow(t, ev, team, "0.2", "0.3")))
    d["code"] = t.rsplit("-", 1)[-1]
    return d
for order in (0, 1):
    ms = [mk("KXNCAAMBGAME-26NOV20UGAGT-UGA", "E9", "Georgia"), mk("KXNCAAMBGAME-26NOV20UGAGT-GT", "E9", "Georgia Tech")]
    if order:
        ms.reverse()
    g = bot.espn_games([["T", "NCAAMB", "9", "2026-11-15T20:00Z", "pre", "UGA", "Georgia", "GT", "Georgia Tech", "", "", 0.7568, 0.2432, "", "", "", "", ""]])
    bot.attach_fair({m["ticker"]: m for m in ms}, g)
    fair = {m["code"]: m.get("fair") for m in ms}
    assert fair == {"UGA": 0.7568, "GT": 0.2432}, fair
print("georgia/georgia tech ok")
