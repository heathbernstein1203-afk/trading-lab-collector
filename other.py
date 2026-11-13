"""Trading Lab "other markets" pot: Kalshi weather, crypto and economics, paper money only.

A separate fake $1,000 bankroll in state/other.json. Never copied to the Kalshi demo.
Each run:
  1. saves live prices for the series below to data/other/<date>.csv,
  2. saves the National Weather Service daily-high forecast for each city (data/weather/<date>.csv),
  3. settles finished bets from Kalshi results,
  4. data bets: about $2 on one random live outcome of each event (weather city-day,
     crypto hour, economics release), to measure how accurate Kalshi prices are,
  5. weather-forecast strategy: compares each temperature bucket's price with a simple
     forecast model (actual high ~ normal around the NWS forecast) and buys when the
     model beats the price by 8c+ after fees.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import os
import time
import urllib.parse

import collector as col

STATE = os.environ.get("BOT_OTHER_STATE", "state/other.json")
DEFAULTS = {"on": True, "bankroll": 1000, "data_stake": 2.0, "max_open": 500, "min_volume": 100,
            "weather": {"on": True, "min_edge": 0.08, "stake": 5.0, "sd_today": 2.0, "sd_tomorrow": 3.0, "max_per_day": 1}}

# Kalshi series -> (kind, how many days ahead to look)
SERIES = {
    "KXHIGHNY": ("weather", 2), "KXHIGHCHI": ("weather", 2), "KXHIGHMIA": ("weather", 2),
    "KXHIGHAUS": ("weather", 2), "KXHIGHDEN": ("weather", 2), "KXHIGHLAX": ("weather", 2),
    "KXHIGHPHIL": ("weather", 2),
    "KXBTCD": ("crypto", 1), "KXETHD": ("crypto", 1),
    "KXCPI": ("econ", 45), "KXPAYROLLS": ("econ", 45), "KXFEDDECISION": ("econ", 60),
}
# NWS stations Kalshi settles on (lat, lon)
CITIES = {
    "KXHIGHNY": (40.7789, -73.9692),    # Central Park
    "KXHIGHCHI": (41.7868, -87.7522),   # Chicago Midway
    "KXHIGHMIA": (25.7959, -80.2870),   # Miami Intl
    "KXHIGHAUS": (30.1945, -97.6699),   # Austin Bergstrom
    "KXHIGHDEN": (39.8561, -104.6737),  # Denver Intl
    "KXHIGHLAX": (33.9425, -118.4081),  # Los Angeles Intl
    "KXHIGHPHIL": (39.8729, -75.2437),  # Philadelphia Intl
}
# when to place the one data bet per event: hours before the market closes
DATA_WINDOW = {"weather": (2, 36), "crypto": (0.4, 1.5), "econ": (2, 24 * 7)}
HDR = ["ts", "series", "ticker", "event", "sub", "strike_type", "floor", "cap", "bid", "ask", "last",
       "volume", "close", "occurrence"]


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)


def _t(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fee(qty, price):
    return math.ceil(0.07 * qty * price * (1 - price) * 100) / 100


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bucket_prob(strike_type, floor, cap, mu, sd):
    """P(the day's high lands in this Kalshi bucket), whole degrees, high ~ Normal(mu, sd)."""
    if strike_type == "greater" and floor is not None:          # ">53" = 54 or above
        return 1 - ncdf((floor + 0.5 - mu) / sd)
    if strike_type == "less" and cap is not None:               # "<46" = 45 or below
        return ncdf((cap - 0.5 - mu) / sd)
    if strike_type == "between" and floor is not None and cap is not None:  # 52-53
        return ncdf((cap + 0.5 - mu) / sd) - ncdf((floor - 0.5 - mu) / sd)
    return None


# ---------------- collection ----------------
def fetch_markets(now):
    out = []
    for s, (kind, days) in SERIES.items():
        max_close = int((now + dt.timedelta(days=days)).timestamp())
        cursor = ""
        for _ in range(5):
            q = {"series_ticker": s, "status": "open", "limit": 1000, "max_close_ts": max_close}
            if cursor:
                q["cursor"] = cursor
            p = col.get_json(col.KALSHI + "?" + urllib.parse.urlencode(q)) or {}
            for m in p.get("markets", []):
                out.append({"series": s, "kind": kind, "ticker": m.get("ticker"), "event": m.get("event_ticker"),
                            "sub": m.get("yes_sub_title"), "strike_type": m.get("strike_type"),
                            "floor": _num(m.get("floor_strike")), "cap": _num(m.get("cap_strike")),
                            "bid": _num(m.get("yes_bid_dollars")), "ask": _num(m.get("yes_ask_dollars")),
                            "last": _num(m.get("last_price_dollars")), "volume": _num(m.get("volume_fp")) or 0,
                            "close": m.get("close_time"), "occurrence": m.get("occurrence_datetime") or ""})
            cursor = p.get("cursor") or ""
            if not cursor:
                break
            time.sleep(0.3)
        time.sleep(0.3)
    return out


def save_snapshot(now, markets):
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for m in markets:
        live = m["bid"] is not None and m["ask"] is not None and (m["bid"] >= 0.02 or m["ask"] <= 0.98) \
            and not (m["bid"] == 0 and m["ask"] >= 0.99)
        if m["kind"] == "weather" or live:  # crypto/econ: keep only strikes that are still in play
            rows.append([ts] + [m.get(k) for k in HDR[1:]])
    col.append_rows(f"{col.DATA}/other/{now:%Y-%m-%d}.csv", HDR, rows)
    return len(rows)


def weather_forecasts(now, st):
    """Daily high forecast per city and date from api.weather.gov (free, needs a User-Agent)."""
    grid = st.setdefault("nws_grid", {})
    out = {}
    rows = []
    for s, (lat, lon) in CITIES.items():
        url = grid.get(s)
        if not url:
            p = col.get_json(f"https://api.weather.gov/points/{lat},{lon}")
            url = ((p or {}).get("properties") or {}).get("forecast")
            if url:
                grid[s] = url
        if not url:
            continue
        f = col.get_json(url) or {}
        for per in (f.get("properties") or {}).get("periods", []):
            if not per.get("isDaytime"):
                continue
            day = (per.get("startTime") or "")[:10]
            temp = per.get("temperature")
            if day and isinstance(temp, (int, float)):
                out[(s, day)] = float(temp)
                rows.append([now.strftime("%Y-%m-%dT%H:%M:%SZ"), s, day, temp])
        time.sleep(0.3)
    col.append_rows(f"{col.DATA}/weather/{now:%Y-%m-%d}.csv", ["ts", "series", "date", "forecast_high"], rows)
    return out


def event_date(ev):
    """KXHIGHNY-26NOV14 -> 2026-11-14"""
    try:
        part = ev.split("-")[1][:7]
        return dt.datetime.strptime(part, "%y%b%d").strftime("%Y-%m-%d")
    except (IndexError, ValueError):
        return None


# ---------------- book ----------------
def place(st, now, m, side, price, qty, strategy, reason, fair=None):
    cost = round(qty * price, 2)
    f = fee(qty, price)
    st["cash"] = round(st["cash"] - cost - f, 2)
    mid = (m["bid"] + m["ask"]) / 2
    b = {"id": f"{now:%Y%m%d%H%M}-{m['ticker']}-{side}", "ticker": m["ticker"], "event": m["event"],
         "series": m["series"], "kind": m["kind"], "sub": m["sub"], "side": side, "qty": qty, "price": price,
         "cost": cost, "fee": f, "mid": round(mid if side == "yes" else 1 - mid, 4),
         "fair": None if fair is None else round(fair if side == "yes" else 1 - fair, 4),
         "strategy": strategy, "reason": reason, "close_time": m["close"], "placed": now.isoformat(),
         "last_seen": round(mid if side == "yes" else 1 - mid, 4), "close": None,
         "status": "open", "payout": None, "pnl": None, "settled": None}
    st["bets"].append(b)
    return b


def settle(st, now, by_ticker):
    for b in st["bets"]:  # last price before the market closes
        if b["status"] != "open":
            continue
        m = by_ticker.get(b["ticker"])
        ct = _t(b.get("close_time"))
        if m and m["bid"] is not None and m["ask"] is not None and (not ct or now < ct):
            mid = (m["bid"] + m["ask"]) / 2
            b["last_seen"] = round(mid if b["side"] == "yes" else 1 - mid, 4)
        if ct and now >= ct and b["close"] is None:
            b["close"] = b["last_seen"]
    open_t = sorted({b["ticker"] for b in st["bets"] if b["status"] == "open"})
    for i in range(0, len(open_t), 50):
        p = col.get_json(col.KALSHI + "?" + urllib.parse.urlencode({"tickers": ",".join(open_t[i:i + 50]), "limit": 200}))
        res = {m["ticker"]: (m.get("result") or "") for m in (p or {}).get("markets", [])}
        for b in st["bets"]:
            r = res.get(b["ticker"], "")
            if b["status"] != "open" or r not in ("yes", "no"):
                continue
            won = r == b["side"]
            b["payout"] = float(b["qty"]) if won else 0.0
            b["pnl"] = round(b["payout"] - b["cost"] - b["fee"], 2)
            b["status"] = "won" if won else "lost"
            b["settled"] = now.isoformat()
            st["cash"] = round(st["cash"] + b["payout"], 2)
            if b["close"] is None:
                b["close"] = b["last_seen"]


def summarize(st):
    out = {}
    for b in st["bets"]:
        if b["status"] not in ("won", "lost"):
            continue
        key = f"{b['strategy']}:{b['kind']}"
        s = out.setdefault(key, {"n": 0, "won": 0, "staked": 0.0, "pnl": 0.0, "price": 0.0, "fair": 0.0,
                                 "fair_n": 0, "clv": 0.0, "clv_n": 0, "buckets": {}})
        s["n"] += 1
        s["won"] += b["status"] == "won"
        s["staked"] += b["cost"] + b["fee"]
        s["pnl"] += b["pnl"]
        s["price"] += b["price"]
        if b.get("fair") is not None:
            s["fair"] += b["fair"]; s["fair_n"] += 1
        if b.get("close") is not None:
            s["clv"] += b["close"] - b["price"]; s["clv_n"] += 1
        k = f"{int(b['mid'] * 10) * 10:02d}-{int(b['mid'] * 10) * 10 + 10}c"
        bk = s["buckets"].setdefault(k, [0, 0]); bk[0] += 1; bk[1] += b["status"] == "won"
    for s in out.values():
        s["win_rate"] = round(s["won"] / s["n"], 3)
        s["avg_price"] = round(s.pop("price") / s["n"], 3)
        s["avg_fair"] = round(s.pop("fair") / s["fair_n"], 3) if s["fair_n"] else None
        s["avg_clv"] = round(s.pop("clv") / s["clv_n"], 4) if s["clv_n"] else None
        s["roi"] = round(s["pnl"] / s["staked"], 3) if s["staked"] else 0
        s["staked"], s["pnl"] = round(s["staked"], 2), round(s["pnl"], 2)
    return out


def run(now, cfg, markets=None, forecasts=None):
    d = dict(DEFAULTS, **(cfg.get("other_markets") or {}))
    d["weather"] = dict(DEFAULTS["weather"], **((cfg.get("other_markets") or {}).get("weather") or {}))
    if not d.get("on"):
        return []
    st = _load(STATE, {"cash": d["bankroll"], "bets": [], "seen_events": [], "nws_grid": {}})
    if markets is None:
        markets = fetch_markets(now)
        save_snapshot(now, markets)
    if forecasts is None:
        forecasts = weather_forecasts(now, st) if d["weather"]["on"] else {}
    by_ticker = {m["ticker"]: m for m in markets}
    settle(st, now, by_ticker)

    exposure = sum(b["cost"] + b["fee"] for b in st["bets"] if b["status"] == "open")
    seen = set(st.get("seen_events", []))
    events = []
    by_event = {}
    for m in markets:
        by_event.setdefault(m["event"], []).append(m)

    def ok(m):
        return m["bid"] is not None and m["ask"] is not None and 0 < m["bid"] <= m["ask"] < 1 \
            and m["volume"] >= d["min_volume"]

    # weather-forecast strategy
    W = d["weather"]
    wx_today = {}
    for b in st["bets"]:
        if b["strategy"] == "weather-forecast":
            wx_today[b["event"]] = wx_today.get(b["event"], 0) + 1
    if W.get("on"):
        for ev, ms in sorted(by_event.items()):
            if ms[0]["kind"] != "weather" or wx_today.get(ev, 0) >= W["max_per_day"]:
                continue
            day = event_date(ev)
            mu = forecasts.get((ms[0]["series"], day))
            ct = _t(ms[0]["close"])
            if mu is None or not day or not ct or ct - now > dt.timedelta(hours=36) or ct <= now + dt.timedelta(hours=1):
                continue
            # Only bet days that have not started yet (US local time). On the day itself the high may
            # already have happened (e.g. warm just after midnight before a cold front), which the
            # afternoon forecast does not show, so same-day forecasts are not a fair price.
            if day <= (now - dt.timedelta(hours=5)).strftime("%Y-%m-%d"):
                continue
            sd = W["sd_tomorrow"]
            best = None
            for m in ms:
                if not ok(m):
                    continue
                p = bucket_prob(m["strike_type"], m["floor"], m["cap"], mu, sd)
                if p is None:
                    continue
                for side, prob, ask in (("yes", p, m["ask"]), ("no", 1 - p, round(1 - m["bid"], 4))):
                    if ask <= 0.10 or ask >= 0.97:  # never buy cheap YES/NO lottery tickets or 97c+ favorites
                        continue
                    edge = prob - ask - 0.07 * ask * (1 - ask)
                    if edge >= W["min_edge"] and (best is None or edge > best[0]):
                        best = (edge, m, side, ask, p)
            if best:
                edge, m, side, ask, p = best
                qty = max(1, int(W["stake"] // ask))
                if exposure + qty * ask > d["max_open"] or qty * ask > st["cash"]:
                    continue
                b = place(st, now, m, side, ask, qty, "weather-forecast",
                          f"NWS forecast high {mu:.0f}F (sd {sd}); model {p if side == 'yes' else 1 - p:.2f} vs ask {ask:.2f}, edge {edge * 100:.0f}c after fees", p)
                exposure += b["cost"] + b["fee"]
                wx_today[ev] = wx_today.get(ev, 0) + 1
                events.append(f"weather {m['ticker']} {side} x{qty} @ {ask}")

    # data bets: one random live outcome per event, inside the event's time window
    for ev, ms in sorted(by_event.items()):
        if ev in seen:
            continue
        ct = _t(ms[0]["close"])
        lo, hi = DATA_WINDOW[ms[0]["kind"]]
        if not ct or not (now + dt.timedelta(hours=lo) <= ct <= now + dt.timedelta(hours=hi)):
            continue
        live = sorted([m for m in ms if ok(m) and 0.05 <= (m["bid"] + m["ask"]) / 2 <= 0.95], key=lambda x: x["ticker"])
        if not live:
            continue
        m = live[int(hashlib.sha256(ev.encode()).hexdigest(), 16) % len(live)]
        side, price = "yes", m["ask"]
        if price <= 0.10:
            side, price = "no", round(1 - m["bid"], 4)
        qty = max(1, int(d["data_stake"] // price))
        if exposure + qty * price > d["max_open"] or qty * price > st["cash"]:
            break
        b = place(st, now, m, side, price, qty, "data", "random live outcome for calibration")
        exposure += b["cost"] + b["fee"]
        seen.add(ev)
        events.append(f"data {m['ticker']} {side} x{qty} @ {price}")

    st["seen_events"] = sorted(seen)[-5000:]
    cutoff = (now - dt.timedelta(days=120)).isoformat()
    old = [b for b in st["bets"] if b["status"] != "open" and (b.get("settled") or "") < cutoff]
    if old:
        ap = f"state/other_archive_{now:%Y}.json"
        _save(ap, _load(ap, []) + old)
        st["bets"] = [b for b in st["bets"] if b not in old]
    st["summary"] = summarize(st)
    st["updated"] = now.isoformat()
    _save(STATE, st)
    return [f"other markets: {len(events)} new, cash {st['cash']:.2f}"] if events else []
