"""Trading Lab auto-bot: paper trading on sports prediction markets, 24/7.

Runs right after collector.py on GitHub Actions. Every run it:
  1. fills or cancels its paper limit orders,
  2. records the last price before each game starts (closing line value),
  3. settles finished games from Kalshi results,
  4. looks for new bets using the rules in config.json,
  5. optionally copies new bets to Heath's Kalshi DEMO account (fake money).

Paper money only. The only account this code can ever trade on is the Kalshi
demo exchange; real-money hosts are refused in code.
"""
import base64
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

import collector as col
import datapot
import other
import backtest

CONFIG = os.environ.get("BOT_CONFIG", "config.json")
STATE = os.environ.get("BOT_STATE", "state/paper.json")
DEMO_STATE = os.environ.get("BOT_DEMO_STATE", "state/demo.json")
DEMO_BASES = ["https://external-api.demo.kalshi.co", "https://demo-api.kalshi.co"]  # demo exchange only
DEMO_BASE = DEMO_BASES[0]
DEMO_PATH = "/trade-api/v2"


# ---------------- helpers ----------------
def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_time(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def taker_fee(qty, price):
    return math.ceil(0.07 * qty * price * (1 - price) * 100) / 100


def norm(s):
    s = (s or "").lower().replace("st.", "state").replace("&", "and")
    return re.sub(r"[^a-z0-9 ]", "", s).strip()


ALIASES = {  # Kalshi team code -> ESPN abbreviation, where they differ
    "NYK": "NY", "GSW": "GS", "SAS": "SA", "UTA": "UTAH", "NOP": "NO", "JAC": "JAX", "WAS": "WSH",
    "LA": "LAR", "BKN": "BKN", "PHX": "PHX", "NYR": "NYR", "TB": "TB", "SJ": "SJ", "LAK": "LA",
}


def team_matches(k_code, k_name, e_abbr, e_name):
    if k_code and e_abbr and (k_code == e_abbr or ALIASES.get(k_code) == e_abbr):
        return True
    a, b = norm(k_name), norm(e_name)
    return bool(a and b and (a == b or (len(a) > 3 and b.startswith(a)) or (len(b) > 3 and a.startswith(b))))


# ---------------- market views ----------------
def kalshi_markets(rows):
    """Latest row per ticker, as dicts."""
    out = {}
    for r in rows:
        d = dict(zip(col.KALSHI_HDR, r))
        for k in ("bid", "ask", "last", "volume", "volume_24h"):
            d[k] = num(d[k])
        d["code"] = d["ticker"].rsplit("-", 1)[-1] if d.get("ticker") else ""
        out[d["ticker"]] = d
    return out


def espn_games(rows):
    games = []
    for r in rows:
        g = dict(zip(col.ESPN_HDR, r))
        g["fair_away"], g["fair_home"] = num(g["fair_away"]), num(g["fair_home"])
        g["start_dt"] = parse_time(g["start"])
        games.append(g)
    return games


def attach_fair(markets, games):
    """Give each Kalshi market the sportsbook no-vig price and start time from ESPN, when the game matches."""
    by_event = {}
    for m in markets.values():
        by_event.setdefault(m.get("event"), []).append(m)
    matched = 0
    for ev, ms in by_event.items():
        if len(ms) != 2:
            continue
        for g in games:
            if g["fair_home"] is None or not g["start_dt"]:
                continue
            a, b = ms
            for x, y in ((a, b), (b, a)):
                if team_matches(x["code"], x["team"], g["home"], g["home_name"]) and \
                   team_matches(y["code"], y["team"], g["away"], g["away_name"]):
                    x["fair"], y["fair"] = g["fair_home"], g["fair_away"]
                    x["start_dt"] = y["start_dt"] = g["start_dt"]
                    x["league"] = y["league"] = g["league"]
                    matched += 1
                    break
            if "fair" in ms[0]:
                break
    # No ESPN match: Kalshi's occurrence/expected-expiration times are about 3 hours AFTER the real start
    # (e.g. Bucks @ Magic tipped 00:00Z, Kalshi said 03:00Z), so use the earlier of the two minus 3 hours.
    # Before this fix (Nov 13) the bots placed bets on games already in progress.
    for m in markets.values():
        if m.get("start_dt"):
            continue
        times = [t for t in (parse_time(m.get("start")), parse_time(m.get("expected_end"))) if t]
        m["start_dt"] = (min(times) - dt.timedelta(hours=3)) if times else None
    return matched


# ---------------- paper book ----------------
def equity(state):
    return state["cash"] + sum(b["cost"] + b["fee"] for b in state["bets"] if b["status"] == "open")


def open_exposure(state):
    return sum(b["cost"] + b["fee"] for b in state["bets"] if b["status"] == "open") + \
        sum(b["limit"] * b["qty"] for b in state["bets"] if b["status"] == "pending")


def side_prices(m, side):
    """(ask, bid) for buying the given side."""
    if side == "yes":
        return m["ask"], m["bid"]
    return (None if m["bid"] is None else round(1 - m["bid"], 4)), (None if m["ask"] is None else round(1 - m["ask"], 4))


def kelly_stake(prob, price, bankroll, frac, cap):
    if prob is None or price is None or price >= 1 or prob <= price:
        return 0.0
    f = (prob - price) / (1 - price)
    return max(0.0, min(cap, bankroll * f * frac))


def place(state, cfg, m, side, strategy, price, qty, fair, reason, now, limit=False):
    fee = 0.0 if limit else taker_fee(qty, price)
    cost = round(qty * price, 2)
    if not limit:
        if cost + fee > state["cash"]:
            return None
        state["cash"] = round(state["cash"] - cost - fee, 2)
    bet = {
        "id": f"{now:%Y%m%d%H%M}-{m['ticker']}-{side}", "ticker": m["ticker"], "event": m.get("event"),
        "team": m.get("team"), "side": side, "strategy": strategy, "qty": qty, "fair": fair,
        "league": m.get("league", ""), "start": m["start_dt"].isoformat() if m.get("start_dt") else "",
        "placed": now.isoformat(), "reason": reason, "last_seen": price, "close": None,
        "payout": None, "pnl": None, "settled": None,
    }
    if limit:
        bet.update(status="pending", limit=price, price=None, cost=0.0, fee=0.0)
    else:
        bet.update(status="open", limit=None, price=price, cost=cost, fee=fee)
    state["bets"].append(bet)
    return bet


def manage_book(state, markets, now):
    """Fill/cancel limit orders, track closing prices."""
    events = []
    for b in state["bets"]:
        m = markets.get(b["ticker"])
        start = parse_time(b["start"])
        started = bool(start and now >= start)
        if b["status"] == "pending":
            if started:
                b["status"] = "cancelled"
                events.append(f"cancelled {b['ticker']} {b['side']} (not filled before start)")
                continue
            if m:
                ask, _ = side_prices(m, b["side"])
                if ask is not None and ask <= b["limit"]:
                    cost = round(b["qty"] * b["limit"], 2)
                    if cost <= state["cash"]:
                        state["cash"] = round(state["cash"] - cost, 2)
                        b.update(status="open", price=b["limit"], cost=cost, fee=0.0)
                        events.append(f"filled {b['ticker']} {b['side']} at {b['limit']}")
        if b["status"] == "open" and m and not started:
            ask, bid = side_prices(m, b["side"])
            mid = (ask + bid) / 2 if ask is not None and bid is not None else ask
            if mid is not None:
                b["last_seen"] = round(mid, 4)
        if b["status"] == "open" and started and b["close"] is None:
            b["close"] = b["last_seen"]
    return events


def settle(state, now):
    open_tickers = sorted({b["ticker"] for b in state["bets"] if b["status"] == "open"})
    events = []
    for i in range(0, len(open_tickers), 50):
        chunk = open_tickers[i:i + 50]
        p = col.get_json(col.KALSHI + "?" + urllib.parse.urlencode({"tickers": ",".join(chunk), "limit": 200}))
        results = {m["ticker"]: (m.get("result") or "") for m in (p or {}).get("markets", [])}
        for b in state["bets"]:
            r = results.get(b["ticker"], "")
            if b["status"] != "open" or r not in ("yes", "no"):
                continue
            won = r == b["side"]
            b["payout"] = float(b["qty"]) if won else 0.0
            b["pnl"] = round(b["payout"] - b["cost"] - b["fee"], 2)
            b["status"] = "won" if won else "lost"
            b["settled"] = now.isoformat()
            state["cash"] = round(state["cash"] + b["payout"], 2)
            events.append(f"{b['status']} {b['ticker']} {b['side']} pnl {b['pnl']}")
    return events


def find_bets(state, cfg, markets, now):
    S = cfg["strategies"]
    bank = equity(state)
    if bank < state.get("peak", bank) * (1 - cfg.get("drawdown_brake", 0.2)):
        mult = 0.5  # drawdown brake
    else:
        mult = 1.0
    held = {(b["event"]) for b in state["bets"] if b["status"] in ("open", "pending")}
    n_open = sum(1 for b in state["bets"] if b["status"] in ("open", "pending"))
    max_open_cash = bank * cfg["max_open_pct"]
    new = []

    def room(stake):
        return n_open + len(new) < cfg["max_open_bets"] and open_exposure(state) + stake <= max_open_cash

    for m in sorted(markets.values(), key=lambda x: -(x.get("volume") or 0)):
        if m.get("status") not in ("active", "open") or m["event"] in held:
            continue
        start = m.get("start_dt")
        if not start or start <= now + dt.timedelta(minutes=10) or start > now + dt.timedelta(hours=cfg.get("horizon_hours", 36)):
            continue
        if (m.get("volume") or 0) < cfg["min_volume"] or m["ask"] is None or m["bid"] is None:
            continue
        fair = m.get("fair")
        cap = min(cfg["max_bet_pct"] * bank, 10**9) * mult

        # 1) line-edge: sportsbook says we're getting a better price than fair, after fees
        st = S.get("line-edge", {})
        if st.get("on") and fair is not None:
            for side, prob in (("yes", fair), ("no", 1 - fair)):
                ask, _ = side_prices(m, side)
                if ask is None or ask <= 0.10:
                    continue
                edge = prob - ask - 0.07 * ask * (1 - ask)
                if edge >= st["min_edge"]:
                    stake = kelly_stake(prob, ask, bank, cfg["kelly_fraction"], min(cap, st["max_stake"] * mult))
                    qty = int(stake // ask)
                    if qty >= 1 and room(qty * ask):
                        b = place(state, cfg, m, side, "line-edge", ask, qty, round(prob, 4),
                                  f"Sportsbook fair {prob:.3f} vs ask {ask:.2f}, edge {edge*100:.1f}c after fees", now)
                        if b:
                            new.append(b); held.add(m["event"]); break
            if m["event"] in held:
                continue

        # 2) fav-maker: limit order 1c above the bid on favorites the sportsbook agrees with
        st = S.get("fav-maker", {})
        if st.get("on") and fair is not None and st["lo"] <= m["bid"] <= st["hi"]:
            limit = round(m["bid"] + 0.01, 2)
            if limit >= m["ask"]:  # 1c spread: join the bid instead of paying the ask
                limit = round(m["bid"], 2)
            if fair >= limit + st.get("min_margin", 0.0):
                stake = kelly_stake(fair, limit, bank, cfg["kelly_fraction"], min(cap, st["max_stake"] * mult))
                qty = int(stake // limit)
                if qty >= 1 and room(qty * limit):
                    b = place(state, cfg, m, "yes", "fav-maker", limit, qty, round(fair, 4),
                              f"Favorite, limit {limit:.2f} (bid {m['bid']:.2f}, ask {m['ask']:.2f}), sportsbook {fair:.3f}", now, limit=True)
                    new.append(b); held.add(m["event"])
                    continue

        # 3) no-longshot: buy NO on contracts priced 10c or less
        st = S.get("no-longshot", {})
        if st.get("on") and st.get("min_price", 0) <= m["ask"] <= st["max_price"] and (m.get("volume") or 0) >= st.get("min_volume", 0):
            if fair is None or fair <= m["ask"]:
                no_bid, no_ask = round(1 - m["ask"], 2), round(1 - m["bid"], 2)
                limit = round(no_bid + 0.01, 2)
                if limit >= no_ask:  # 1c spread: join the bid
                    limit = no_bid
                qty = int(min(st["stake"] * mult, cap) // limit)
                if qty >= 1 and 0.5 < limit <= 0.99 and room(qty * limit):
                    b = place(state, cfg, m, "no", "no-longshot", limit, qty, None if fair is None else round(1 - fair, 4),
                              f"YES at {m['ask']:.2f}: fading the longshot with a NO limit at {limit:.2f}", now, limit=True)
                    new.append(b); held.add(m["event"])
    return new


# ---------------- Kalshi DEMO mirror (fake money) ----------------
def key_diag(key, key_id):
    """Safe-to-log facts about the key: type, public-key fingerprint, Key ID shape (never the key itself)."""
    import hashlib
    from cryptography.hazmat.primitives import serialization
    pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {"type": type(key).__name__.replace("PrivateKey", "").strip("_"),
            "pub_fp": hashlib.sha256(pub).hexdigest()[:12],
            "key_id_start": key_id[:4], "key_id_len": len(key_id)}


def redact(text):
    """Strip anything that looks like key material before it is logged or saved."""
    text = re.sub(r"-----BEGIN[^-]*-----.*?(-----END[^-]*-----|$)", "[redacted key]", str(text), flags=re.S)
    return re.sub(r"[A-Za-z0-9+/=]{40,}", "[redacted]", text)

def demo_request(method, path, body=None):
    key_id = (os.environ.get("KALSHI_DEMO_KEY_ID") or "").strip()
    pem = (os.environ.get("KALSHI_DEMO_PRIVATE_KEY") or "").strip()
    if "BEGIN" in key_id and "BEGIN" not in pem:  # the two secrets were pasted into each other's boxes
        key_id, pem = pem, key_id
    uuid_re = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    found = re.search(uuid_re, key_id) if "BEGIN" not in key_id else None
    if found:  # the Key ID box may also hold the key's name or extra lines; keep just the ID
        key_id = found.group(0)
    if not found or "BEGIN" not in pem:
        # never echo secret values: only say what looks wrong
        return {"error": "secrets look wrong", "checks": {
            "key_id_has_uuid": bool(found), "key_id_has_private_key": "BEGIN" in key_id,
            "key_id_len": len(key_id), "private_key_has_begin": "BEGIN" in pem, "private_key_len": len(pem)}}
    assert all(".demo.kalshi.co" in u or u.startswith("https://demo-api.kalshi.co") for u in DEMO_BASES), "demo only"
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
    pem = pem.strip().replace("\\n", "\n")
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    ts = str(int(time.time() * 1000))
    full_path = DEMO_PATH + path
    msg = (ts + method + full_path.split("?")[0]).encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):  # newer Kalshi key type
        sig = key.sign(msg)
    else:  # RSA keys: RSA-PSS with SHA-256
        sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    headers = {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
               "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(), "Content-Type": "application/json"}
    diag = key_diag(key, key_id)
    last = None
    for base in DEMO_BASES:
        req = urllib.request.Request(base + full_path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read().decode() or "{}")
                if isinstance(out, dict):
                    out.setdefault("_host", base)
                return out
        except urllib.error.HTTPError as e:
            last = {"error": e.code, "host": base, "detail": redact(e.read().decode()[:300]), "key": diag}
        except Exception as e:
            last = {"error": type(e).__name__, "host": base, "key": diag}  # no message text: it can contain header values
    return last


GEO_BLOCK = "not currently allowed in"  # Kalshi's state geo check; GitHub runner IPs sometimes look like Washington
DEMO_MAX_RETRIES = 3


def demo_order(b, qty, price):
    """Send one demo order for paper bet b. Returns (ok, response)."""
    cents = int(round(price * 100))
    # Kalshi retired POST /portfolio/orders (v1) on the demo; v2 is POST /portfolio/events/orders.
    # v2 prices the YES leg only: buying YES = side "bid" at the YES price; buying NO = side "ask"
    # (sell YES) at 1 - NO price, which opens a NO position when we hold no YES contracts.
    yes_cents = cents if b["side"] == "yes" else 100 - cents
    body = {"ticker": b["ticker"], "side": "bid" if b["side"] == "yes" else "ask",
            "count": str(qty), "price": f"{yes_cents / 100:.2f}",
            "time_in_force": "good_till_canceled", "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": str(uuid.uuid4())}
    start = parse_time(b["start"])
    if b["status"] == "pending" and start:
        body["expiration_time"] = int(start.timestamp())
    r = demo_request("POST", "/portfolio/events/orders", body) or {}
    return ("order_id" in r or "order" in r), r


def is_geo_block(r):
    return GEO_BLOCK in json.dumps(r) if isinstance(r, dict) else GEO_BLOCK in str(r)


def demo_mirror(cfg, new_bets, now, state=None):
    d = cfg.get("demo", {})
    if not d.get("on") or not os.environ.get("KALSHI_DEMO_KEY_ID"):
        return []
    ds = load(DEMO_STATE, {"days": {}, "orders": []})
    ds.setdefault("retry", [])
    day = now.strftime("%Y-%m-%d")
    spent = ds["days"].get(day, 0.0)
    bal = demo_request("GET", "/portfolio/balance") or {}
    out = []
    if "balance" not in bal:
        # keep this run's new bets so they are tried again next run (still before game start)
        for b in new_bets:
            ds["retry"].append({"bet": b, "tries": 0})
        ds["last_error"] = {"t": now.isoformat(), "resp": redact(bal)}
        save(DEMO_STATE, ds)
        return [f"demo connection problem: {redact(bal)[:200]}"]
    balance = bal["balance"] / 100.0
    ds["last_balance"], ds["last_check"] = round(balance, 2), now.isoformat()
    ds.pop("last_error", None)
    if balance < d.get("min_balance", 20):
        out.append(f"demo balance ${balance:.2f} is under ${d.get('min_balance', 20)}: add mock funds")

    # 1) earlier orders refused by the geo check: retry before the game starts, at most DEMO_MAX_RETRIES times
    live = {x["id"]: x for x in (state or {}).get("bets", [])}
    queue, ds["retry"] = ds["retry"], []
    todo = []
    for item in queue:
        b = item["bet"]
        start = parse_time(b.get("start"))
        paper = live.get(b["id"])
        if start and start <= now:
            out.append(f"demo retry dropped {b['ticker']}: game started")
            continue
        if paper is not None and paper.get("status") not in ("pending", "open"):
            out.append(f"demo retry dropped {b['ticker']}: paper bet is {paper.get('status')}")
            continue
        if item["tries"] >= DEMO_MAX_RETRIES:
            out.append(f"demo retry gave up {b['ticker']} after {item['tries']} retries")
            continue
        todo.append((b, item["tries"] + 1))
    todo += [(b, 0) for b in new_bets]

    # 2) place retries first, then this run's new bets
    for b, tries in todo:
        price = b["limit"] if b["status"] == "pending" else b["price"]
        cents = int(round(price * 100)) if price else 0
        dollars_left = min(d["daily_budget"] - spent, balance * d.get("max_pct_balance", 0.25))
        qty = min(b["qty"], int(dollars_left // price)) if price else 0
        if qty < 1 or not 1 <= cents <= 99:
            continue
        ok, r = demo_order(b, qty, price)
        if ok:
            spent += qty * price
            balance -= qty * price
        elif is_geo_block(r):
            ds["retry"].append({"bet": b, "tries": tries})
        ds["orders"].append({"t": now.isoformat(), "bet": b["id"], "qty": qty, "price": price, "ok": ok,
                             "retry": tries,
                             "resp": redact(r.get("order_id") or (r.get("order") or {}).get("order_id") or r.get("detail") or r.get("error"))})
        tag = f" (retry {tries})" if tries else ""
        out.append(f"demo {'placed' if ok else 'FAILED'}{tag} {b['ticker']} {b['side']} x{qty} @ {cents}c"
                   + (" - geo-blocked, will retry" if not ok and is_geo_block(r) and tries < DEMO_MAX_RETRIES else ""))
    ds["days"][day] = round(spent, 2)
    ds["last_balance"] = round(balance, 2)
    save(DEMO_STATE, ds)
    return out


# ---------------- main ----------------
def run(now, kalshi_rows, espn_rows):
    cfg = load(CONFIG, None)
    if not cfg or not cfg.get("on", True):
        print("bot off")
        return
    state = load(STATE, {"cash": cfg["bankroll_start"], "peak": cfg["bankroll_start"], "bets": [], "log": []})
    markets = kalshi_markets(kalshi_rows)
    matched = attach_fair(markets, espn_games(espn_rows))
    events = manage_book(state, markets, now)
    events += settle(state, now)
    new = find_bets(state, cfg, markets, now)
    events += [f"new {b['strategy']} {b['ticker']} {b['side']} x{b['qty']} @ {b['price'] or b['limit']}" for b in new]
    events += demo_mirror(cfg, new, now, state)
    eq = equity(state)
    state["peak"] = max(state.get("peak", eq), eq)
    state["log"] = (state.get("log", []) + [{"t": now.isoformat(), "equity": round(eq, 2), "cash": state["cash"],
                                             "markets": len(markets), "matched": matched, "events": events}])[-500:]
    save(STATE, state)
    try:  # separate tiny-stake data bankroll; a failure here never stops the main bot
        events += datapot.run(now, markets, cfg)
    except Exception as e:
        print("data pot error:", type(e).__name__, e)
    try:  # weather, crypto and economics markets (separate fake pot)
        events += other.run(now, cfg)
    except Exception as e:
        print("other markets error:", type(e).__name__, e)
    try:  # stock backtests, once a week
        if cfg.get("backtest", {}).get("on", True):
            events += backtest.maybe_run(now)
    except Exception as e:
        print("backtest error:", type(e).__name__, e)
    print(f"equity {eq:.2f} cash {state['cash']:.2f} markets {len(markets)} matched {matched}")
    for e in events:
        print(" ", e)


if __name__ == "__main__":
    os.makedirs(col.DATA, exist_ok=True)
    now, k, e = col.main()
    run(now, k, e)
