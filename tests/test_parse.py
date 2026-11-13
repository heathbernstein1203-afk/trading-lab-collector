import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collector as c

k = {"markets": [{"ticker": "KXNFLGAME-26NOV15NEDET-DET", "event_ticker": "KXNFLGAME-26NOV15NEDET", "yes_sub_title": "Detroit",
      "yes_bid_dollars": "0.5700", "yes_ask_dollars": "0.5800", "last_price_dollars": "0.5800", "volume_fp": "217522.00",
      "volume_24h_fp": "1000", "status": "active", "result": "", "expected_expiration_time": "2026-11-15T17:30:00Z"}], "cursor": ""}
assert c.kalshi_rows(k, "T")[0][3] == "Detroit" and len(c.kalshi_rows(k,"T")[0]) == len(c.KALSHI_HDR)

e = {"events": [{"id": "1", "date": "2026-11-15T14:30Z", "status": {"type": {"state": "pre"}},
      "competitions": [{"competitors": [
          {"homeAway": "home", "team": {"abbreviation": "DET", "location": "Detroit"}, "score": "0"},
          {"homeAway": "away", "team": {"abbreviation": "NE", "location": "New England"}, "score": "0"}],
        "odds": [{"details": "DET -2.5", "overUnder": 50.5, "moneyline": {"home": {"close": {"odds": "-148"}}, "away": {"close": {"odds": "+124"}}}}]}]}]}
r = c.espn_rows(e, "NFL", "T")[0]
assert abs(r[11] + r[12] - 1) < 1e-3 and 0.56 < r[12] < 0.58, r

p = [{"slug": "nfl-ne-det-2026-11-15", "startDate": "x", "markets": [{"sportsMarketType": "moneyline", "outcomes": "[\"Patriots\", \"Lions\"]",
      "outcomePrices": "[\"0.415\", \"0.585\"]", "bestBid": 0.41, "bestAsk": 0.42, "volume": "10", "closed": False}]}]
assert c.poly_rows(p, "nfl", "T")[0][4] == "Patriots/Lions"

s = {"SPY": {"timestamp": [1794600000], "close": [757.12]}}
assert c.stock_rows(s, "T")[0][1:] == ["SPY", 757.12]

with tempfile.TemporaryDirectory() as d:
    n = c.append_rows(d + "/x/a.csv", ["h"], [[1], [2]])
    n2 = c.append_rows(d + "/x/a.csv", ["h"], [[3]])
    assert open(d + "/x/a.csv").read().splitlines() == ["h", "1", "2", "3"]
print("all parse tests passed; DET fair", r[12])
