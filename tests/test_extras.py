"""Offline test for extras.py (wildcard pot + parlay pot). Run: python tests/test_extras.py"""
import os, sys, json, tempfile, datetime as dt, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
d = tempfile.mkdtemp()
os.environ.update(BOT_WILD_STATE=d + "/wild.json", BOT_PARLAY_STATE=d + "/parlay.json",
                  BOT_CATS_STATE=d + "/cats.json", DATA_DIR=d + "/data")
import collector as col, extras

now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)
close = "2026-11-21T12:00:00Z"


def mk(t, ev, bid, ask, vol=500, status="active", result="", title="t"):
    return {"ticker": t, "event_ticker": ev, "yes_bid_dollars": bid, "yes_ask_dollars": ask, "volume_fp": vol,
            "status": status, "result": result, "close_time": close, "title": title, "yes_sub_title": t,
            "last_price_dollars": ask}


WILD = [mk("KXTOPSONG-26NOV28-A", "KXTOPSONG-26NOV28", "0.40", "0.42"), mk("KXTOPSONG-26NOV28-B", "KXTOPSONG-26NOV28", "0.55", "0.57"),
        mk("KXAAAGASW-26NOV23-3.9", "KXAAAGASW-26NOV23", "0.03", "0.05"),  # cheap YES -> must buy NO
        mk("KXTSAW-26NOV22-2.8", "KXTSAW-26NOV22", "0.60", "0.62", vol=50),  # too little volume -> skip
        mk("KXNBAGAME-26NOV20BOSNYK-BOS", "KXNBAGAME-26NOV20BOSNYK", "0.5", "0.52")]  # covered by data pot -> skip


def combo(t, legs, lp, vol=10, status="active", result=""):
    return {"ticker": t, "event_ticker": "KXMVECROSSCATEGORY-" + t, "mve_selected_legs": legs, "last_price_dollars": lp,
            "volume_fp": vol, "status": status, "result": result, "close_time": close, "title": "combo",
            "settlement_ts": "2026-11-20T05:00:00Z", "yes_bid_dollars": "0", "yes_ask_dollars": "0"}


SGP = [{"event_ticker": "KXNHLSPREAD-26NOV19DALVGK", "market_ticker": "KXNHLSPREAD-26NOV19DALVGK-VGK2", "side": "yes"},
       {"event_ticker": "KXNHLTOTAL-26NOV19DALVGK", "market_ticker": "KXNHLTOTAL-26NOV19DALVGK-6", "side": "yes"}]
MIX = [{"event_ticker": f"KXNBAGAME-26NOV20G{i}", "market_ticker": f"KXNBAGAME-26NOV20G{i}-A", "side": "yes"} for i in range(8)]
SETTLED = [combo("KXMVES1", SGP, "0.30", result="no", status="finalized"), combo("KXMVES2", MIX, "0.02", result="yes", status="finalized"),
           combo("KXMVES3", MIX[:3], "0.20", vol=0, result="no", status="finalized")]  # never traded -> not logged
OPEN = [combo("KXMVEO1", SGP, "0.25"), combo("KXMVEO2", MIX, "0.03"), combo("KXMVEO3", MIX[:4], "0.10"), combo("KXMVEO4", MIX[:2], "0.97")]
RESULTS = {}
MARKETS = {m["ticker"]: m for m in WILD + OPEN}
TRADES = [{"ticker": m["ticker"], "taker_side": "yes", "yes_price_dollars": m["last_price_dollars"]} for m in OPEN]
TRADES.append({"ticker": "KXMVEOX", "taker_side": "no", "yes_price_dollars": "0.30"})  # someone selling a parlay: ignored


def fake(url, tries=3, headers=None):
    u = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(u.query))
    if "/series/" in u.path:
        s = u.path.rsplit("/", 1)[-1]
        return {"series": {"ticker": s, "category": "Sports" if s.startswith(("KXNHL", "KXNBA")) else "Entertainment" if "SONG" in s else "Economics"}}
    if u.path.endswith("/markets/trades"):
        return {"trades": TRADES, "cursor": ""}
    if "tickers" in q:
        return {"markets": [RESULTS.get(t) or MARKETS.get(t) or {"ticker": t, "status": "active"} for t in q["tickers"].split(",")]}
    if q.get("mve_filter") == "exclude":
        return {"markets": WILD, "cursor": ""}
    if q.get("mve_filter") == "only":
        assert q.get("status") == "settled", "open combos are found through the trade feed, not by listing"
        return {"markets": SETTLED, "cursor": ""}
    return {"markets": []}


col.get_json = fake
cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")))
cfg["parlays"]["csv_sample_pct"] = 100
skip = set(col.KALSHI_SERIES)
ev = extras.run(now, cfg, skip)
print(ev)
w = json.load(open(d + "/wild.json"))
got = sorted((b["series"], b["side"]) for b in w["bets"])
assert got == [("KXAAAGASW", "no"), ("KXTOPSONG", "yes")], got
gas = [b for b in w["bets"] if b["series"] == "KXAAAGASW"][0]
assert gas["price"] == 0.97 and gas["qty"] == 1 and gas["category"] == "Economics", gas
assert abs(w["cash"] - (1000 - sum(b["cost"] + b["fee"] for b in w["bets"]))) < 0.01

p = json.load(open(d + "/parlay.json"))
assert p["market"]["all"]["n"] == 2 and p["market"]["all"]["won"] == 1, p["market"]
assert p["market"]["2-3 legs · same game"]["n"] == 1 and p["market"]["7+ legs · mixed"]["won"] == 1
rows = open(d + f"/data/parlays/{now:%Y-%m-%d}.csv").read().strip().splitlines()
assert len(rows) == 3, rows  # header + 2 traded combos
buckets = sorted(b["bucket"] for b in p["bets"])
assert buckets == ["2-3 legs · same game", "4-6 legs · mixed", "7+ legs · mixed"], buckets  # O4 at 0.97 is too pricey
assert all(b["qty"] <= 20 for b in p["bets"])

# second run: nothing new to log, no double bets; then settle everything
extras.run(now + dt.timedelta(minutes=30), cfg, skip)
p2 = json.load(open(d + "/parlay.json"))
assert p2["market"]["all"]["n"] == 2 and len(p2["bets"]) == 3
w2 = json.load(open(d + "/wild.json"))
assert len(w2["bets"]) == 2, "no double bets on one event"

for b in w2["bets"] + p2["bets"]:
    RESULTS[b["ticker"]] = {"ticker": b["ticker"], "status": "finalized", "result": "yes" if "TOPSONG" in b["ticker"] or b["ticker"] == "KXMVEO1" else "no"}
extras.run(now + dt.timedelta(hours=30), cfg, skip)
w3, p3 = json.load(open(d + "/wild.json")), json.load(open(d + "/parlay.json"))
st = {b["ticker"]: b["status"] for b in w3["bets"] + p3["bets"]}
assert st["KXAAAGASW-26NOV23-3.9"] == "won" and st["KXMVEO1"] == "won" and st["KXMVEO2"] == "lost", st
o1 = [b for b in p3["bets"] if b["ticker"] == "KXMVEO1"][0]
assert o1["payout"] == o1["qty"] and abs(o1["pnl"] - (o1["qty"] - o1["cost"] - o1["fee"])) < 0.01
assert o1["close"] is not None  # last price recorded before it closed
print("extras OK")
