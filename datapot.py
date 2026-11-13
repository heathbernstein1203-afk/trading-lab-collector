"""Trading Lab data pot: tiny paper bets on every Kalshi sport, 24/7, to collect data.

A separate fake-money bankroll (state/data.json), kept apart from the main
strategy bankroll in state/paper.json. It is never copied to the Kalshi demo.

Every run, for each game that starts in the next few hours, it picks ONE outcome
(a fixed pseudo-random pick from the event ticker, so there is no favorite/underdog
bias) and buys about $2 of it at the ask, paying the Kalshi taker fee. If the pick is
a YES priced 10c or less it buys the NO side instead (playbook rule: never buy cheap YES).

What this measures, per sport and per price range:
  - how often Kalshi prices come true (calibration),
  - how much a "bet everything at the ask" bettor loses to spread + fees,
  - how prices move from our entry to the last price before the start (line movement).
Paper money only. This module never places a real or demo order.
"""
import datetime as dt
import hashlib
import json
import os
import urllib.parse

import collector as col

DATA_STATE = os.environ.get("BOT_DATA_STATE", "state/data.json")
DEFAULTS = {"on": True, "bankroll": 1000, "stake": 2.0, "min_hours": 0.25, "max_hours": 6,
            "max_open": 600, "min_volume": 100}


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


def _fee(qty, price):
    import math
    return math.ceil(0.07 * qty * price * (1 - price) * 100) / 100


def _pick(event, n):
    """Stable pseudo-random index for this event (same pick every run)."""
    return int(hashlib.sha256((event or "").encode()).hexdigest(), 16) % n


def _series(ticker):
    return (ticker or "").split("-", 1)[0]


def run(now, markets, cfg):
    d = dict(DEFAULTS, **(cfg.get("data_pot") or {}))
    if not d.get("on"):
        return []
    st = _load(DATA_STATE, {"cash": d["bankroll"], "bets": [], "seen_events": []})
    events = []

    # 1) track last price before start (close) for open bets
    for b in st["bets"]:
        if b["status"] != "open":
            continue
        m = markets.get(b["ticker"])
        start = col_time(b.get("start"))
        if m and start and now < start and m.get("bid") is not None and m.get("ask") is not None:
            mid_yes = (m["bid"] + m["ask"]) / 2
            b["last_seen"] = round(mid_yes if b["side"] == "yes" else 1 - mid_yes, 4)
        if start and now >= start and b.get("close") is None:
            b["close"] = b.get("last_seen")

    # 2) settle from Kalshi results
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
    # void bets on markets that never settled 3 days after start (postponed etc.): refund
    for b in st["bets"]:
        start = col_time(b.get("start"))
        if b["status"] == "open" and start and now > start + dt.timedelta(days=3):
            b["status"], b["pnl"], b["settled"] = "void", 0.0, now.isoformat()
            st["cash"] = round(st["cash"] + b["cost"] + b["fee"], 2)

    # 3) new tiny bets: one outcome per event starting soon
    exposure = sum(b["cost"] + b["fee"] for b in st["bets"] if b["status"] == "open")
    seen = set(st.get("seen_events", []))
    by_event = {}
    for m in markets.values():
        if m.get("status") in ("active", "open") and m.get("event"):
            by_event.setdefault(m["event"], []).append(m)
    lo, hi = now + dt.timedelta(hours=d["min_hours"]), now + dt.timedelta(hours=d["max_hours"])
    for ev, ms in sorted(by_event.items()):
        if ev in seen or len(ms) < 2:
            continue
        ms = sorted(ms, key=lambda x: x["ticker"])
        m = ms[_pick(ev, len(ms))]
        start = m.get("start_dt")
        if not start or not (lo <= start <= hi):
            continue
        if (m.get("volume") or 0) < d["min_volume"] or m.get("ask") is None or m.get("bid") is None:
            continue
        if not (0 < m["bid"] <= m["ask"] < 1):
            continue
        side, price = "yes", m["ask"]
        if price <= 0.10:  # never buy cheap YES: take the NO side instead
            side, price = "no", round(1 - m["bid"], 4)
        qty = max(1, int(d["stake"] // price))
        cost = round(qty * price, 2)
        fee = _fee(qty, price)
        if exposure + cost + fee > d["max_open"] or cost + fee > st["cash"]:
            break
        st["cash"] = round(st["cash"] - cost - fee, 2)
        exposure += cost + fee
        mid_yes = (m["bid"] + m["ask"]) / 2
        st["bets"].append({
            "id": f"{now:%Y%m%d%H%M}-{m['ticker']}-{side}", "ticker": m["ticker"], "event": ev,
            "series": _series(m["ticker"]), "league": m.get("league", ""), "team": m.get("team"),
            "side": side, "qty": qty, "price": price, "cost": cost, "fee": fee,
            "mid": round(mid_yes if side == "yes" else 1 - mid_yes, 4), "spread": round(m["ask"] - m["bid"], 4),
            "fair": None if m.get("fair") is None else round(m["fair"] if side == "yes" else 1 - m["fair"], 4),
            "volume": round(m.get("volume") or 0), "start": start.isoformat(), "placed": now.isoformat(),
            "last_seen": round(mid_yes if side == "yes" else 1 - mid_yes, 4), "close": None,
            "status": "open", "payout": None, "pnl": None, "settled": None,
        })
        seen.add(ev)
        events.append(f"data {m['ticker']} {side} x{qty} @ {price}")

    st["seen_events"] = sorted(seen)[-5000:]
    # keep the file a sane size: settled bets older than 120 days move to a yearly archive
    cutoff = (now - dt.timedelta(days=120)).isoformat()
    old = [b for b in st["bets"] if b["status"] != "open" and (b.get("settled") or "") < cutoff]
    if old:
        arch_path = f"state/data_archive_{now:%Y}.json"
        arch = _load(arch_path, [])
        _save(arch_path, arch + old)
        st["bets"] = [b for b in st["bets"] if b not in old]
    st["summary"] = summarize(st)
    st["updated"] = now.isoformat()
    _save(DATA_STATE, st)
    return [f"data pot: {len(events)} new, cash {st['cash']:.2f}"] if events else []


def col_time(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def summarize(st):
    """Per-series record, ROI and calibration buckets, for the daily review."""
    out = {}
    for b in st["bets"]:
        if b["status"] not in ("won", "lost"):
            continue
        s = out.setdefault(b["series"], {"n": 0, "won": 0, "staked": 0.0, "pnl": 0.0, "price_sum": 0.0,
                                         "clv_sum": 0.0, "clv_n": 0, "buckets": {}})
        s["n"] += 1
        s["won"] += b["status"] == "won"
        s["staked"] += b["cost"] + b["fee"]
        s["pnl"] += b["pnl"]
        s["price_sum"] += b["price"]
        if b.get("close") is not None:
            s["clv_sum"] += b["close"] - b["price"]
            s["clv_n"] += 1
        k = f"{int(b['mid'] * 10) * 10:02d}-{int(b['mid'] * 10) * 10 + 10}c"
        bk = s["buckets"].setdefault(k, [0, 0])
        bk[0] += 1
        bk[1] += b["status"] == "won"
    for s in out.values():
        s["win_rate"] = round(s["won"] / s["n"], 3)
        s["avg_price"] = round(s["price_sum"] / s["n"], 3)
        s["roi"] = round(s["pnl"] / s["staked"], 3) if s["staked"] else 0
        s["avg_clv"] = round(s["clv_sum"] / s["clv_n"], 4) if s["clv_n"] else None
        s["staked"], s["pnl"] = round(s["staked"], 2), round(s["pnl"], 2)
        for k in ("price_sum", "clv_sum"):
            s.pop(k)
    return out
