"""Trading Lab price collector.

Runs on GitHub Actions every 30 minutes (free). Saves snapshots of
Kalshi, Polymarket and ESPN/DraftKings prices, plus daily stock closes,
as CSV files in data/. Paper trading only: this script never places orders.
Standard library only, so there is nothing to install.
"""
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "trading-lab-collector/1.0 (paper trading research)"}
# some sites (Polymarket's CDN) refuse unknown agents; use a normal browser string there
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36", "Accept": "application/json"}
ERRORS = []  # short failure notes, saved to last_run.json
DATA = os.environ.get("DATA_DIR", "data")

KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_HDR = ["ts", "ticker", "event", "team", "bid", "ask", "last", "volume", "volume_24h", "status", "result", "expected_end", "start"]
ESPN_HDR = ["ts", "league", "event_id", "start", "state", "away", "away_name", "home", "home_name",
            "ml_away", "ml_home", "fair_away", "fair_home", "spread", "total", "score_away", "score_home", "winner"]
KALSHI_SERIES = [
    # tier 1: leagues with sportsbook lines on ESPN
    "KXNFLGAME", "KXNCAAFGAME", "KXNBAGAME", "KXNHLGAME", "KXNCAAMBGAME", "KXUFCFIGHT",
    # soccer
    "KXEPLGAME", "KXUCLGAME", "KXLALIGAGAME", "KXSERIEAGAME", "KXBUNDESLIGAGAME",
    "KXLIGUE1GAME", "KXMLSGAME", "KXCHNSLGAME",
    # tennis and table tennis
    "KXATPMATCH", "KXWTAGAME", "KXTTELITEMATCH", "KXTABLETENNISMATCH", "KXITTFMENMATCH",
]
ESPN_LEAGUES = {
    "NFL": "football/nfl",
    "CFB": "football/college-football",
    "NBA": "basketball/nba",
    "NHL": "hockey/nhl",
    "NCAAMB": "basketball/mens-college-basketball",
}
POLY_TAGS = ["nfl", "nba", "nhl", "cfb", "soccer", "ufc", "tennis"]
STOCKS = [
    "SPY", "QQQ", "VTI", "SCHD", "USMV", "BND", "SGOV", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "AMD", "AVGO", "JPM", "COST", "NFLX", "DIS", "NKE", "PLTR", "UBER", "DKNG",
    "SPOT", "XLK", "XLE", "XLF", "XLV", "XLY", "XLI", "XLP", "XLU", "SMH", "COIN", "HOOD",
    "RKLB", "MSTR", "SOFI", "TQQQ",
]


def get_json(url, tries=3, headers=None):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network hiccup or rate limit: wait and retry
            if i == tries - 1:
                print(f"FAILED {url}: {e}", file=sys.stderr)
                ERRORS.append(f"{urllib.parse.urlsplit(url).netloc}: {type(e).__name__} {getattr(e, 'code', '')}".strip())
                return None
            time.sleep(2 * (i + 1))


def append_rows(path, header, rows):
    if not rows:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)
    return len(rows)


# ---------- Kalshi ----------
def kalshi_rows(payload, ts):
    out = []
    for m in (payload or {}).get("markets", []):
        out.append([
            ts, m.get("ticker"), m.get("event_ticker"), m.get("yes_sub_title"),
            m.get("yes_bid_dollars"), m.get("yes_ask_dollars"), m.get("last_price_dollars"),
            m.get("volume_fp"), m.get("volume_24h_fp"), m.get("status"), m.get("result") or "",
            m.get("expected_expiration_time") or m.get("close_time"),
            m.get("occurrence_datetime") or "",
        ])
    return out


def collect_kalshi(ts, now):
    max_close = int((now + dt.timedelta(days=4)).timestamp())
    rows = []
    for s in KALSHI_SERIES:
        cursor = ""
        for _ in range(5):  # up to 1,000 markets per series
            q = {"series_ticker": s, "status": "open", "limit": 200, "max_close_ts": max_close}
            if cursor:
                q["cursor"] = cursor
            p = get_json(KALSHI + "?" + urllib.parse.urlencode(q))
            rows += kalshi_rows(p, ts)
            cursor = (p or {}).get("cursor") or ""
            if not cursor:
                break
            time.sleep(0.3)
        time.sleep(0.3)
    hdr = KALSHI_HDR
    append_rows(f"{DATA}/kalshi/{now:%Y-%m-%d}.csv", hdr, rows)
    return rows


# ---------- ESPN / DraftKings ----------
def american_to_prob(a):
    try:
        a = float(str(a).replace("+", ""))
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return -a / (-a + 100) if a < 0 else 100 / (a + 100)


def power_devig(pa, pb):
    """Remove the sportsbook margin with the power method: find k so pa^k + pb^k = 1."""
    lo, hi = 1.0, 4.0
    for _ in range(60):
        k = (lo + hi) / 2
        if pa ** k + pb ** k > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return pa ** k, pb ** k


def espn_rows(payload, league, ts):
    out = []
    for e in (payload or {}).get("events", []):
        c = (e.get("competitions") or [{}])[0]
        teams = c.get("competitors") or []
        home = next((t for t in teams if t.get("homeAway") == "home"), None)
        away = next((t for t in teams if t.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        o = (c.get("odds") or [{}])[0] or {}
        def ml(side):
            m = (o.get("moneyline") or {}).get(side) or {}
            v = (m.get("close") or {}).get("odds") or (m.get("open") or {}).get("odds")
            return v if v is not None else (o.get(side + "TeamOdds") or {}).get("moneyLine")
        ma, mh = ml("away"), ml("home")
        pa, ph = american_to_prob(ma), american_to_prob(mh)
        fa = fh = ""
        if pa and ph:
            a, h = power_devig(pa, ph)
            fa, fh = round(a, 4), round(h, 4)
        st = (e.get("status") or {}).get("type") or {}
        out.append([
            ts, league, e.get("id"), e.get("date"), st.get("state"),
            away["team"].get("abbreviation"), away["team"].get("location"),
            home["team"].get("abbreviation"), home["team"].get("location"),
            ma or "", mh or "", fa, fh, o.get("details") or "", o.get("overUnder") or "",
            away.get("score") or "", home.get("score") or "",
            "away" if away.get("winner") else "home" if home.get("winner") else "",
        ])
    return out


def collect_espn(ts, now):
    rows = []
    for league, path in ESPN_LEAGUES.items():
        for d in (now, now + dt.timedelta(days=1)):
            extra = "&groups=80&limit=300" if league == "CFB" else ("&groups=50&limit=300" if league == "NCAAMB" else "")
            url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={d:%Y%m%d}{extra}"
            rows += espn_rows(get_json(url), league, ts)
            time.sleep(0.3)
    append_rows(f"{DATA}/espn/{now:%Y-%m-%d}.csv", ESPN_HDR, rows)
    return rows


# ---------- Polymarket ----------
def poly_rows(payload, tag, ts):
    out = []
    for ev in payload or []:
        for m in ev.get("markets") or []:
            if m.get("sportsMarketType") != "moneyline":
                continue
            try:
                outcomes = json.loads(m.get("outcomes") or "[]")
                prices = json.loads(m.get("outcomePrices") or "[]")
            except ValueError:
                continue
            out.append([ts, tag, ev.get("slug"), ev.get("startDate"), "/".join(outcomes), "/".join(prices),
                        m.get("bestBid"), m.get("bestAsk"), m.get("volume"), m.get("closed")])
    return out


def collect_poly(ts, now):
    rows = []
    q = {"closed": "false", "limit": 100,
         "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "end_date_max": (now + dt.timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for tag in POLY_TAGS:
        q["tag_slug"] = tag
        rows += poly_rows(get_json("https://gamma-api.polymarket.com/events?" + urllib.parse.urlencode(q), headers=BROWSER_UA), tag, ts)
        time.sleep(0.3)
    hdr = ["ts", "tag", "slug", "start", "outcomes", "prices", "best_bid_first", "best_ask_first", "volume", "closed"]
    append_rows(f"{DATA}/poly/{now:%Y-%m-%d}.csv", hdr, rows)
    return rows


# ---------- Stocks (once a day after the close) ----------
def stock_rows(payload, ts):
    out = []
    for sym, d in (payload or {}).items():
        stamps, closes = d.get("timestamp") or [], d.get("close") or []
        for t, c in zip(stamps, closes):
            if c is not None:
                out.append([dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), sym, round(c, 4)])
    return out


def collect_stocks(ts, now):
    rows = []
    for i in range(0, len(STOCKS), 12):  # Yahoo's spark endpoint takes about 12 symbols at a time
        syms = ",".join(STOCKS[i:i + 12])
        rows += stock_rows(get_json(f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={syms}&range=5d&interval=1d"), ts)
        time.sleep(0.5)
    path = f"{DATA}/stocks/closes.csv"
    seen = set()
    if os.path.exists(path):
        with open(path) as f:
            seen = {(r[0], r[1]) for r in csv.reader(f)}
    rows = [r for r in rows if (r[0], r[1]) not in seen]
    return append_rows(path, ["date", "symbol", "close"], rows)


def main():
    now = dt.datetime.now(dt.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    kalshi, espn, poly = collect_kalshi(ts, now), collect_espn(ts, now), collect_poly(ts, now)
    counts = {"kalshi": len(kalshi), "espn": len(espn), "poly": len(poly)}
    # stock closes once a day, on the run after 21:00 UTC on weekdays
    if now.weekday() < 5 and now.hour == 21 or os.environ.get("FORCE_STOCKS"):
        counts["stocks"] = collect_stocks(ts, now)
    print(ts, counts)
    with open(f"{DATA}/last_run.json", "w") as f:
        json.dump({"ts": ts, "counts": counts, "errors": sorted(set(ERRORS))[:20]}, f)
    return now, kalshi, espn


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    main()
