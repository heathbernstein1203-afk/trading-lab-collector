"""Pot refills (fake money only).

At the start of each bot run, any paper pot whose cash is below 40% of its start
amount is topped back up to its start amount. Each refill is logged in that pot's
state file under "refills" (time UTC, cash before, amount added) so ROI and P&L
can still be judged against the total money put in.
"""
import json
import os

THRESHOLD = 0.40

# pot name -> (state file env var, default path, how to read the start amount from config.json)
POTS = {
    "paper": ("BOT_STATE", "state/paper.json", lambda c: c.get("bankroll_start", 1000)),
    "data": ("BOT_DATA_STATE", "state/data.json", lambda c: c.get("data_pot", {}).get("bankroll", 1000)),
    "other": ("BOT_OTHER_STATE", "state/other.json", lambda c: c.get("other_markets", {}).get("bankroll", 1000)),
    "wild": ("BOT_WILD_STATE", "state/wild.json", lambda c: c.get("wild", {}).get("bankroll", 1000)),
    "parlay": ("BOT_PARLAY_STATE", "state/parlay.json", lambda c: c.get("parlays", {}).get("bankroll", 500)),
}


def refill_one(path, start, now, threshold=THRESHOLD):
    """Top up one state file if its cash is under threshold*start. Returns the refill entry or None."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        st = json.load(f)
    cash = float(st.get("cash", start))
    if cash >= threshold * start:
        return None
    added = round(start - cash, 2)
    entry = {"t": now.isoformat(), "cash_before": round(cash, 2), "added": added}
    st["cash"] = round(start, 2)
    st.setdefault("refills", []).append(entry)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, path)
    return entry


def run(now, cfg):
    events = []
    for name, (env, default, start_of) in POTS.items():
        path = os.environ.get(env, default)
        try:
            e = refill_one(path, float(start_of(cfg)), now)
        except Exception as ex:  # a refill problem never stops the bot
            print("refill error", name, type(ex).__name__, ex)
            continue
        if e:
            events.append(f"refill {name}: cash {e['cash_before']} + {e['added']}")
    return events
