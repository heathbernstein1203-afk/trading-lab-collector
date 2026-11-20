"""Trading Lab "extras": two more fake-money data pots, paper money only (added Nov 20 at Heath's request).

1. Wildcard pot (state/wild.json, fake $1,000): about $1 on one random outcome of each liquid Kalshi
   event that the other pots don't cover - pop culture (Billboard, Spotify, Netflix, Rotten Tomatoes),
   AI and tech (model market share, token use), gas prices, TSA, rain, mentions, sports props (spreads,
   totals, halves), anything closing in the next few days. Measures how accurate those prices are.

2. Parlay pot (state/parlay.json, fake $500):
   a) every run it logs Kalshi combo (parlay) markets that traded and settled to data/parlays/<date>.csv
      and keeps running totals of what a buyer at the last trade price got back, by number of legs and
      same-game vs mixed - a free, large data set on how much parlays cost;
   b) about $1 paper bets on combos that real buyers just bought (from Kalshi's public trade feed), at the
      price they paid plus the taker fee, spread across 2-3 / 4-6 / 7+ legs and same-game vs mixed.
   Combos are quoted by request-for-quote, so a real recent trade price stands in for a quote.

Neither pot is ever copied to the Kalshi demo. Standard library only.
"""
import datetime as dt
import hashlib
import json
import math
import os
import urllib.parse

import collector as col

WILD_STATE = os.environ.get("BOT_WILD_STATE", "state/wild.json")
PARLAY_STATE = os.environ.get("BOT_PARLAY_STATE", "state/parlay.json")
CATS_STATE = os.environ.get("BOT_CATS_STATE", "state/series_cats.json")
API = "https://api.elections.kalshi.com/trade-api/v2"

WILD_DEFAULTS = {"on": True, "bankroll": 1000, "stake": 1.0, "max_open": 400, "min_volume": 200,
                 "max_new_per_run": 12, "lookahead_hours": 72, "min_hours": 0.5, "max_pages": 6,
                 "max_cat_lookups": 25}
PARLAY_DEFAULTS = {"on": True, "bankroll": 500, "stake": 1.0, "max_open": 150, "max_new_per_run": 6,
                   "min_price": 0.01, "max_price": 0.95, "log_pages": 2, "trade_pages": 2, "csv_sample_pct": 10}
PARLAY_HDR = ["ts", "ticker", "legs", "same_game", "topic", "last_price", "volume", "result", "settled", "leg_markets"]


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


def fee(qty, price):
    return math.ceil(0.07 * qty * price * (1 - price) * 100) / 100


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _t(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _h(s):
    return int(hashlib.sha256((s or "").encode()).hexdigest(), 16)


def series_of(ticker):
    return (ticker or "").split("-", 1)[0]


def _pages(params, max_pages):
    out, cursor = [], None
    for _ in range(max_pages):
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        p = col.get_json(f"{API}/markets?" + urllib.parse.urlencode(q))
        if not p:
            break
        out += p.get("markets", [])
        cursor = p.get("cursor")
        if not cursor:
            break
    return out


# ---------- series -> category (cached forever, a few lookups per run) ----------
def categories(series_list, max_lookups):
    cats = _load(CATS_STATE, {})
    looked = 0
    for s in sorted(set(series_list)):
        if s in cats or looked >= max_lookups:
            continue
        p = col.get_json(f"{API}/series/{urllib.parse.quote(s)}")
        looked += 1
        ser = (p or {}).get("series") or {}
        cats[s] = ser.get("category") or ("?" if p is None else "Other")
        if p is None:
            cats.pop(s)  # network failure: try again next run
    if looked:
        _save(CATS_STATE, cats)
    return cats


def _settle(st, now, kind):
    """Settle open bets from Kalshi results; track the last mid while trading (CLV)."""
    open_t = sorted({b["ticker"] for b in st["bets"] if b["status"] == "open"})
    for i in range(0, len(open_t), 50):
        p = col.get_json(f"{API}/markets?" + urllib.parse.urlencode({"tickers": ",".join(open_t[i:i + 50]), "limit": 200}))
        if p is None:
            continue
        info = {m["ticker"]: m for m in p.get("markets", [])}
        for b in st["bets"]:
            m = info.get(b["ticker"])
            if b["status"] != "open" or not m:
                continue
            bid, ask = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
            status, res = m.get("status"), m.get("result") or ""
            if status == "active" and bid is not None and ask is not None and 0 < bid <= ask < 1:
                mid = (bid + ask) / 2
                b["last_seen"] = round(mid if b["side"] == "yes" else 1 - mid, 4)
            elif status == "active" and kind == "parlay":
                lp = _num(m.get("last_price_dollars"))
                if lp:
                    b["last_seen"] = round(lp if b["side"] == "yes" else 1 - lp, 4)
            if status != "active" and b.get("close") is None:
                b["close"] = b.get("last_seen")
            if res in ("yes", "no"):
                won = res == b["side"]
                b["payout"] = float(b["qty"]) if won else 0.0
                b["status"] = "won" if won else "lost"
            elif res == "scalar" or (status in ("finalized", "settled") and _num(m.get("settlement_value_dollars")) is not None and res not in ("yes", "no")):
                v = _num(m.get("settlement_value_dollars")) or 0.0
                b["payout"] = round(b["qty"] * (v if b["side"] == "yes" else 1 - v), 2)
                b["status"] = "push"
            else:
                continue
            b["pnl"] = round(b["payout"] - b["cost"] - b["fee"], 2)
            b["settled"] = now.isoformat()
            st["cash"] = round(st["cash"] + b["payout"], 2)
    # refund bets whose market never resolved 10 days after it was due to close
    for b in st["bets"]:
        due = _t(b.get("close_time"))
        if b["status"] == "open" and due and now > due + dt.timedelta(days=10):
            b["status"], b["pnl"], b["settled"] = "void", 0.0, now.isoformat()
            st["cash"] = round(st["cash"] + b["cost"] + b["fee"], 2)


def _archive(st, now, name):
    cutoff = (now - dt.timedelta(days=120)).isoformat()
    old = [b for b in st["bets"] if b["status"] != "open" and (b.get("settled") or "") < cutoff]
    if old:
        path = f"state/{name}_archive_{now:%Y}.json"
        _save(path, _load(path, []) + old)
        st["bets"] = [b for b in st["bets"] if b not in old]


def _buy(st, now, m, side, price, qty, strategy, extra):
    cost = round(qty * price, 2)
    f = fee(qty, price)
    st["cash"] = round(st["cash"] - cost - f, 2)
    b = {"id": f"{now:%Y%m%d%H%M}-{m['ticker']}-{side}", "ticker": m["ticker"], "event": m.get("event_ticker"),
         "series": series_of(m.get("event_ticker") or m["ticker"]), "strategy": strategy, "side": side, "qty": qty,
         "price": price, "cost": cost, "fee": f, "fair": None, "placed": now.isoformat(),
         "close_time": m.get("close_time"), "title": (m.get("title") or "")[:160], "sub": (m.get("yes_sub_title") or "")[:120],
         "status": "open", "payout": None, "pnl": None, "settled": None, "close": None}
    b.update(extra)
    st["bets"].append(b)
    return b


# ---------- wildcard pot ----------
def run_wild(now, cfg, skip_series):
    d = dict(WILD_DEFAULTS, **(cfg.get("wild") or {}))
    if not d.get("on"):
        return []
    st = _load(WILD_STATE, {"cash": d["bankroll"], "start": d["bankroll"], "bets": [], "seen_events": []})
    _settle(st, now, "wild")

    lo = now + dt.timedelta(hours=d["min_hours"])
    ms = _pages({"status": "open", "mve_filter": "exclude", "limit": 1000,
                 "min_close_ts": int(lo.timestamp()),
                 "max_close_ts": int((now + dt.timedelta(hours=d["lookahead_hours"])).timestamp())}, d["max_pages"])
    by_ev = {}
    for m in ms:
        ev = m.get("event_ticker")
        s = series_of(ev)
        if not ev or s in skip_series or s.startswith("KXMVE"):
            continue
        by_ev.setdefault(ev, []).append(m)
    cats = categories([series_of(e) for e in by_ev], d["max_cat_lookups"])

    seen = set(st.get("seen_events", []))
    open_bets = [b for b in st["bets"] if b["status"] == "open"]
    exposure = sum(b["cost"] + b["fee"] for b in open_bets)
    per_cat = {}
    for b in st["bets"]:
        if b["status"] != "void":
            per_cat[b.get("category", "?")] = per_cat.get(b.get("category", "?"), 0) + 1
    cands = []
    for ev, group in by_ev.items():
        if ev in seen:
            continue
        vol = sum(_num(x.get("volume_fp")) or 0 for x in group)
        if vol < d["min_volume"]:
            continue
        ok = [x for x in group if 0 < (_num(x.get("yes_bid_dollars")) or 0) <= (_num(x.get("yes_ask_dollars")) or 1) < 1]
        if not ok:
            continue
        ok.sort(key=lambda x: x["ticker"])
        m = ok[_h(ev) % len(ok)]
        cat = cats.get(series_of(ev))
        if not cat:
            continue  # category not looked up yet: wait for a later run
        cands.append((per_cat.get(cat, 0), _h(ev + now.strftime("%Y%m%d")) % 1000, ev, m, cat, vol))
    cands.sort(key=lambda c: (c[0], c[1]))  # least-covered categories first

    events, new_cat = [], {}
    for n_cat, _, ev, m, cat, vol in cands:
        if len(events) >= d["max_new_per_run"]:
            break
        if new_cat.get(cat, 0) >= 3:
            continue  # at most 3 new bets per category per run
        bid, ask = _num(m["yes_bid_dollars"]), _num(m["yes_ask_dollars"])
        side, price = "yes", ask
        if price <= 0.10:  # playbook rule: never buy YES at 10c or less
            side, price = "no", round(1 - bid, 4)
        if price >= 0.99:
            continue
        qty = max(1, int(d["stake"] // price))
        cost = qty * price + fee(qty, price)
        if exposure + cost > d["max_open"] or cost > st["cash"]:
            break
        mid = (bid + ask) / 2
        _buy(st, now, m, side, price, qty, "wild", {
            "category": cat, "mid": round(mid if side == "yes" else 1 - mid, 4), "spread": round(ask - bid, 4),
            "volume": round(vol), "last_seen": round(mid if side == "yes" else 1 - mid, 4)})
        exposure += cost
        seen.add(ev)
        new_cat[cat] = new_cat.get(cat, 0) + 1
        events.append(f"wild {m['ticker']} {side} x{qty} @ {price} ({cat})")

    st["seen_events"] = sorted(seen)[-20000:]
    _archive(st, now, "wild")
    st["updated"] = now.isoformat()
    _save(WILD_STATE, st)
    return [f"wildcard pot: {len(events)} new, cash {st['cash']:.2f}"] if events else []


# ---------- parlay pot ----------
def combo_info(m):
    legs = m.get("mve_selected_legs") or []
    games = {(l.get("event_ticker") or "").split("-", 1)[-1] for l in legs}
    n = len(legs)
    return {"legs": n, "same_game": bool(n >= 2 and len(games) == 1),
            "leg_series": sorted({series_of(l.get("event_ticker")) for l in legs})}


def leg_bucket(n):
    return "2-3 legs" if n <= 3 else "4-6 legs" if n <= 6 else "7+ legs"


def topic(leg_series, cats):
    ts = {cats.get(s, "?") for s in leg_series}
    return ts.pop() if len(ts) == 1 else "Mixed"


def run_parlay(now, cfg):
    d = dict(PARLAY_DEFAULTS, **(cfg.get("parlays") or {}))
    if not d.get("on"):
        return []
    st = _load(PARLAY_STATE, {"cash": d["bankroll"], "start": d["bankroll"], "bets": [], "logged": [], "market": {}})
    _settle(st, now, "parlay")

    # a) log settled combos that traded: what did buyers at the last price get back?
    settled = [m for m in _pages({"status": "settled", "mve_filter": "only", "limit": 1000}, d["log_pages"])
               if (_num(m.get("volume_fp")) or 0) > 0 and m.get("result") in ("yes", "no")]
    # b) candidates for paper bets: combos bought (taker took YES) in the latest public trades
    trades = []
    cursor = None
    for _ in range(d["trade_pages"]):
        q = {"limit": 1000}
        if cursor:
            q["cursor"] = cursor
        p = col.get_json(f"{API}/markets/trades?" + urllib.parse.urlencode(q))
        if not p:
            break
        trades += [t for t in p.get("trades", []) if (t.get("ticker") or "").startswith("KXMVE") and t.get("taker_side") == "yes"]
        cursor = p.get("cursor")
        if not cursor:
            break
    have = {b["ticker"] for b in st["bets"]}
    price_of = {}
    for t in trades:  # newest first: keep the latest trade price per combo
        yp = _num(t.get("yes_price_dollars"))
        if t["ticker"] not in have and t["ticker"] not in price_of and yp and d["min_price"] <= yp <= d["max_price"]:
            price_of[t["ticker"]] = yp
    pick = sorted(price_of, key=lambda k: _h(k))[:100]
    opened = []
    for i in range(0, len(pick), 50):
        p = col.get_json(f"{API}/markets?" + urllib.parse.urlencode({"tickers": ",".join(pick[i:i + 50]), "limit": 200}))
        opened += [m for m in (p or {}).get("markets", []) if m.get("status") == "active"]

    all_series = set()
    for m in settled + opened:
        all_series.update(combo_info(m)["leg_series"])
    cats = categories(all_series, 25)
    logged = set(st.get("logged", []))
    rows = []
    for m in settled:
        if m["ticker"] in logged:
            continue
        ci, lp, vol = combo_info(m), _num(m.get("last_price_dollars")) or 0, _num(m.get("volume_fp")) or 0
        if not (0 < lp < 1):
            continue
        won = m["result"] == "yes"
        tp = topic(ci["leg_series"], cats)
        if _h(m["ticker"]) % 100 < d["csv_sample_pct"]:  # keep the CSV a sane size: a sample of rows
            rows.append([now.isoformat(timespec="seconds"), m["ticker"], ci["legs"], int(ci["same_game"]), tp, lp, round(vol, 2),
                         m["result"], m.get("settlement_ts", ""), ";".join(f"{l.get('market_ticker')}:{l.get('side')}" for l in m.get("mve_selected_legs") or [])])
        for key in (f"{leg_bucket(ci['legs'])} · {'same game' if ci['same_game'] else 'mixed'}", f"topic · {tp}", "all"):
            a = st["market"].setdefault(key, {"n": 0, "won": 0, "price": 0.0, "vol": 0.0, "vol_cost": 0.0, "vol_paid": 0.0})
            a["n"] += 1
            a["won"] += won
            a["price"] += lp
            a["vol"] += vol
            a["vol_cost"] += vol * lp  # dollars buyers paid
            a["vol_paid"] += vol * won  # dollars paid back
        logged.add(m["ticker"])
    col.append_rows(f"{col.DATA}/parlays/{now:%Y-%m-%d}.csv", PARLAY_HDR, rows)
    st["logged"] = sorted(logged)[-15000:]

    # c) paper bets: ~$1 per combo at the price a real buyer just paid, balanced across buckets
    exposure = sum(b["cost"] + b["fee"] for b in st["bets"] if b["status"] == "open")
    count = {}
    for b in st["bets"]:
        count[b["bucket"]] = count.get(b["bucket"], 0) + 1
    cands = []
    for m in opened:
        lp = price_of.get(m["ticker"])
        ct = _t(m.get("close_time"))
        if lp is None or (ct and ct < now + dt.timedelta(minutes=30)):
            continue
        ci = combo_info(m)
        if ci["legs"] < 2:
            continue
        bk = f"{leg_bucket(ci['legs'])} · {'same game' if ci['same_game'] else 'mixed'}"
        cands.append((count.get(bk, 0), _h(m["ticker"]) % 1000, m, ci, bk, lp))
    cands.sort(key=lambda c: (c[0], c[1]))
    events, used = [], set()
    n_buckets = len({c[4] for c in cands})
    for _, _, m, ci, bk, lp in cands:
        if len(events) >= d["max_new_per_run"]:
            break
        if bk in used and len(used) < n_buckets:
            continue  # one per bucket per run while other buckets have candidates
        qty = min(20, max(1, int(d["stake"] // max(lp, 0.05))))
        cost = qty * lp + fee(qty, lp)
        if exposure + cost > d["max_open"] or cost > st["cash"]:
            break
        _buy(st, now, m, "yes", lp, qty, "parlay", {
            "bucket": bk, "legs": ci["legs"], "same_game": ci["same_game"], "topic": topic(ci["leg_series"], cats),
            "mid": lp, "last_seen": lp, "volume": round(_num(m.get("volume_fp")) or 0, 2)})
        exposure += cost
        used.add(bk)
        events.append(f"parlay {m['ticker']} x{qty} @ {lp} ({bk})")

    _archive(st, now, "parlay")
    st["updated"] = now.isoformat()
    _save(PARLAY_STATE, st)
    out = [f"parlay pot: {len(rows)} settled combos to csv, {len(events)} new bets, cash {st['cash']:.2f}"] if rows or events else []
    return out


def run(now, cfg, skip_series=()):
    events = []
    try:
        events += run_wild(now, cfg, set(skip_series))
    except Exception as e:  # never stop the main bot
        print("wildcard pot error:", type(e).__name__, e)
    try:
        events += run_parlay(now, cfg)
    except Exception as e:
        print("parlay pot error:", type(e).__name__, e)
    return events
