"""Trading Lab stock backtests: replay the playbook's stock strategies on past prices.

Runs once a week (or when state/backtest.json is missing) from bot.py on GitHub Actions.
Downloads daily adjusted closes from Yahoo Finance, simulates each strategy with a
0.05% cost per trade, and writes state/backtest.json with return, worst drop,
Sharpe and worst year for each strategy next to SPY over the same dates.

Caveat written into the results: the momentum, dip-buy and high-beta lists are today's
well-known stocks, picked with hindsight, so their backtests look better than a real
investor would have done (survivorship bias). Use these numbers to compare risk and to
spot broken ideas, not as a forecast.
"""
import datetime as dt
import json
import math
import os
import time

import collector as col

OUT = os.environ.get("BACKTEST_OUT", "state/backtest.json")
COST = 0.0005
MOMENTUM = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "JPM", "COST",
            "NFLX", "DIS", "NKE", "PLTR", "UBER", "DKNG", "SPOT"]
SECTORS = ["XLK", "XLE", "XLF", "XLV", "XLY", "XLI", "XLP", "XLU", "SMH"]
HIGH_BETA = ["COIN", "HOOD", "RKLB", "MSTR", "SOFI", "PLTR", "TSLA", "AMD"]
OTHER = ["SPY", "QQQ", "SCHD", "USMV", "BIL", "TQQQ"]
ALL = sorted(set(MOMENTUM + SECTORS + HIGH_BETA + OTHER))


# ---------------- data ----------------
def fetch(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=25y&interval=1d&events=div,split"
    p = col.get_json(url) or {}
    r = ((p.get("chart") or {}).get("result") or [None])[0]
    if not r:
        return {}
    ts = r.get("timestamp") or []
    ind = r.get("indicators") or {}
    adj = ((ind.get("adjclose") or [{}])[0] or {}).get("adjclose") or ((ind.get("quote") or [{}])[0] or {}).get("close") or []
    return {dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"): c for t, c in zip(ts, adj) if c}


def load_prices():
    px = {}
    for s in ALL:
        px[s] = fetch(s)
        time.sleep(0.4)
    return px


# ---------------- helpers ----------------
class Series:
    """Fast lookups on one symbol's closes aligned to the master calendar."""
    def __init__(self, dates, prices):
        self.c = [prices.get(d) for d in dates]
        last = None
        for i, v in enumerate(self.c):  # forward-fill gaps after the first price
            if v is None:
                self.c[i] = last
            else:
                last = v
        self.first = next((i for i, v in enumerate(self.c) if v is not None), None)

    def ok(self, i, need=0):
        return self.first is not None and i - need >= self.first and self.c[i] is not None

    def ret(self, i, n):
        return self.c[i] / self.c[i - n] - 1

    def sma(self, i, n):
        return sum(self.c[i - n + 1:i + 1]) / n

    def rsi(self, i, n=14):
        g = l = 0.0
        for k in range(i - n + 1, i + 1):
            d = self.c[k] - self.c[k - 1]
            g += max(d, 0); l += max(-d, 0)
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    def vol(self, i, n=20):
        r = [math.log(self.c[k] / self.c[k - 1]) for k in range(i - n + 1, i + 1)]
        m = sum(r) / n
        return math.sqrt(sum((x - m) ** 2 for x in r) / n * 252)


def run_weights(dates, S, start, pick, rebalance):
    """Generic daily simulator. pick(i) -> {sym: weight}; weights summing < 1 sit in cash
    (earning BIL when available). rebalance(i) -> bool. Returns daily equity list."""
    eq, w, out = 1.0, {}, []
    bil = S.get("BIL")
    for i in range(start, len(dates)):
        if i > start:  # apply yesterday's weights to today's move
            day = 0.0
            for s, x in w.items():
                c0, c1 = S[s].c[i - 1], S[s].c[i]
                if c0 and c1:
                    day += x * (c1 / c0 - 1)
            cash = 1 - sum(w.values())
            if cash > 1e-9 and bil and bil.c[i - 1] and bil.c[i]:
                day += cash * (bil.c[i] / bil.c[i - 1] - 1)
            eq *= 1 + day
        if i == start or rebalance(i):
            nw = pick(i)
            turnover = sum(abs(nw.get(s, 0) - w.get(s, 0)) for s in set(nw) | set(w))
            eq *= 1 - COST * turnover
            w = nw
        out.append(eq)
    return out


def stats(dates, eq, start):
    ds = dates[start:start + len(eq)]
    years = max((dt.date.fromisoformat(ds[-1]) - dt.date.fromisoformat(ds[0])).days / 365.25, 1e-9)
    rets = [eq[k] / eq[k - 1] - 1 for k in range(1, len(eq))]
    m = sum(rets) / len(rets) if rets else 0
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets)) if rets else 0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    yearly = {}
    for d, v in zip(ds, eq):
        yearly[d[:4]] = v  # last value of each year
    ys = sorted(yearly)
    yr = {}
    base = eq[0]
    for y in ys:
        yr[y] = round(yearly[y] / base - 1, 4)
        base = yearly[y]
    return {"from": ds[0], "to": ds[-1], "years": round(years, 1),
            "total_return": round(eq[-1] / eq[0] - 1, 4),
            "cagr": round((eq[-1] / eq[0]) ** (1 / years) - 1, 4),
            "max_drawdown": round(mdd, 4),
            "sharpe": round(m / sd * math.sqrt(252), 2) if sd else None,
            "worst_year": min(yr.items(), key=lambda x: x[1]) if yr else None,
            "by_year": yr}


def first_index(dates, S, syms, need):
    idx = [S[s].first + need for s in syms if s in S and S[s].first is not None]
    return max(idx) if idx else None


# ---------------- strategies ----------------
def strategies(dates, S):
    mondays = {i for i, d in enumerate(dates) if dt.date.fromisoformat(d).weekday() == 0 or
               (i > 0 and dt.date.fromisoformat(d).isocalendar()[1] != dt.date.fromisoformat(dates[i - 1]).isocalendar()[1])}
    month_start = {i for i in range(1, len(dates)) if dates[i][:7] != dates[i - 1][:7]}
    every = lambda i: True
    res = {}

    def add(name, syms, need, pick, reb, note=""):
        st = first_index(dates, S, syms, need)
        if st is None or st >= len(dates) - 60:
            res[name] = {"error": "not enough history"}
            return
        eq = run_weights(dates, S, st, pick, reb)
        spy = run_weights(dates, S, st, lambda i: {"SPY": 1.0}, lambda i: False)
        res[name] = {"note": note, "strategy": stats(dates, eq, st), "spy_same_dates": stats(dates, spy, st)}

    # SPY 200-day trend rule alone, longest history (shows 2000-02 and 2008)
    add("spy-200day", ["SPY"], 200, lambda i: {"SPY": 1.0} if S["SPY"].c[i] > S["SPY"].sma(i, 200) else {},
        every, "Hold SPY above its 200-day average, otherwise T-bills (BIL) or cash.")

    # core-low as in the playbook: half SPY-or-T-bills, quarter SCHD, quarter USMV
    def core(i):
        w = {"SCHD": 0.25, "USMV": 0.25}
        if S["SPY"].c[i] > S["SPY"].sma(i, 200):
            w["SPY"] = 0.5
        return w
    add("core-low", ["SPY", "SCHD", "USMV"], 200, core, every, "50% SPY (T-bills below the 200-day), 25% SCHD, 25% USMV.")

    def momentum(top, look, trend=50):
        def pick(i):
            c = [s for s in MOMENTUM if S[s].ok(i, max(look, trend) + 1) and S[s].c[i] > S[s].sma(i, trend)]
            c.sort(key=lambda s: -S[s].ret(i, look))
            c = c[:top]
            return {s: 1 / top for s in c}
        return pick
    for top in (3, 5, 10):
        for look in (63, 126, 252):
            name = f"momentum-top{top}-{look}d" + (" (playbook)" if (top, look) == (5, 63) else "")
            add(name, ["AAPL", "MSFT", "AMZN"], 260, momentum(top, look), lambda i: i in mondays,
                f"Top {top} of the momentum list by {look}-day return, above 50-day average, weekly. Hindsight list.")

    def sectors(i):
        c = [s for s in SECTORS if S[s].ok(i, 253) and S[s].c[i] > S[s].sma(i, 200)]
        c.sort(key=lambda s: -(S[s].c[i - 21] / S[s].c[i - 252] - 1))
        return {s: 1 / 3 for s in c[:3]}
    add("sector-rotation", ["XLK", "XLE", "XLF", "SMH"], 253, sectors, lambda i: i in month_start,
        "Monthly: 3 sector ETFs with the best 12-minus-1-month return that are above their 200-day average.")

    # dip-buy: 10 slots, RSI(14) < 35 buy; sell RSI > 55, 15 days, or -10%
    def dip_sim():
        st = first_index(dates, S, ["AAPL", "MSFT", "AMZN"], 30)
        eq, cash, pos, out = 1.0, 1.0, {}, []
        for i in range(st, len(dates)):
            val = cash + sum(q * S[s].c[i] for s, (q, _, _) in pos.items())
            for s in list(pos):
                q, px, day = pos[s]
                c = S[s].c[i]
                if S[s].rsi(i) > 55 or i - day >= 15 or c / px - 1 <= -0.10:
                    cash += q * c * (1 - COST); del pos[s]
            for s in MOMENTUM:
                if len(pos) >= 10 or s in pos or not S[s].ok(i, 15):
                    continue
                if S[s].rsi(i) < 35:
                    amt = min(val / 10, cash)
                    if amt > 0:
                        pos[s] = (amt * (1 - COST) / S[s].c[i], S[s].c[i], i); cash -= amt
            out.append(cash + sum(q * S[s].c[i] for s, (q, _, _) in pos.items()))
        spy = run_weights(dates, S, st, lambda i: {"SPY": 1.0}, lambda i: False)
        return {"note": "10 equal slots across the momentum list. Hindsight list.",
                "strategy": stats(dates, out, st), "spy_same_dates": stats(dates, spy, st)}
    res["dip-buy"] = dip_sim()

    # high-beta: top 3 by 1-month return above 50-day, weekly, with -12% stop
    def hb_sim():
        st = first_index(dates, S, ["COIN", "HOOD", "RKLB", "SOFI"], 60)
        if st is None:
            return {"error": "not enough history"}
        held, out, eq, w = {}, [], 1.0, {}
        for i in range(st, len(dates)):
            if i > st:
                eq *= 1 + sum(x * (S[s].c[i] / S[s].c[i - 1] - 1) for s, x in w.items())
            for s in list(held):  # stop loss
                if S[s].c[i] / held[s] - 1 <= -0.12:
                    eq *= 1 - COST * w.get(s, 0); w.pop(s, None); held.pop(s)
            if i in mondays or i == st:
                c = [s for s in HIGH_BETA if S[s].ok(i, 51) and S[s].c[i] > S[s].sma(i, 50)]
                c.sort(key=lambda s: -S[s].ret(i, 21))
                nw = {s: 1 / 3 for s in c[:3]}
                eq *= 1 - COST * sum(abs(nw.get(s, 0) - w.get(s, 0)) for s in set(nw) | set(w))
                for s in nw:
                    if s not in w:
                        held[s] = S[s].c[i]
                held = {s: p for s, p in held.items() if s in nw}
                w = nw
            out.append(eq)
        spy = run_weights(dates, S, st, lambda i: {"SPY": 1.0}, lambda i: False)
        return {"note": "Short history (most of these listed in 2020-21). Hindsight list.",
                "strategy": stats(dates, out, st), "spy_same_dates": stats(dates, spy, st)}
    res["high-beta"] = hb_sim()

    add("tqqq-trend (playbook)", ["QQQ", "TQQQ"], 200,
        lambda i: {"TQQQ": 1.0} if S["QQQ"].c[i] > S["QQQ"].sma(i, 200) and S["QQQ"].vol(i) < 0.35 else {},
        every, "TQQQ only while QQQ is above its 200-day average and QQQ's 20-day volatility is under 35%.")
    add("tqqq-trend-no-vol-filter", ["QQQ", "TQQQ"], 200,
        lambda i: {"TQQQ": 1.0} if S["QQQ"].c[i] > S["QQQ"].sma(i, 200) else {}, every,
        "TQQQ while QQQ is above its 200-day average.")
    add("tqqq-buy-and-hold", ["TQQQ"], 1, lambda i: {"TQQQ": 1.0}, lambda i: False,
        "For comparison: holding TQQQ through everything.")
    return res


def run_all(px):
    dates = sorted(set(d for s in ("SPY",) for d in px.get(s, {})))
    S = {s: Series(dates, px.get(s, {})) for s in ALL}
    S = {s: v for s, v in S.items() if v.first is not None}
    for s in ALL:
        S.setdefault(s, Series(dates, {}))
    res = strategies(dates, S)
    return {"generated": dt.datetime.now(dt.timezone.utc).isoformat(), "cost_per_trade": COST,
            "caveat": "Momentum, dip-buy and high-beta use today's well-known stocks (picked with hindsight), "
                      "so their results are better than a real investor would have seen. Compare risk and "
                      "relative results, not exact returns.",
            "symbols_loaded": {s: (S[s].first is not None) for s in ALL},
            "results": res}


def maybe_run(now, force=False):
    prev = None
    try:
        with open(OUT) as f:
            prev = json.load(f)
    except (FileNotFoundError, ValueError):
        pass
    if prev and not force and os.environ.get("FORCE_BACKTEST") is None:
        age = now - dt.datetime.fromisoformat(prev["generated"])
        if age < dt.timedelta(days=7):
            return []
    px = load_prices()
    if not px.get("SPY"):
        return ["backtest skipped: no SPY prices"]
    out = run_all(px)
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    return ["backtest updated"]
