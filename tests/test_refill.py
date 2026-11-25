import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refill

NOW = dt.datetime(2026, 11, 25, 4, 0, tzinfo=dt.timezone.utc)


def _write(path, cash):
    with open(path, "w") as f:
        json.dump({"cash": cash, "bets": []}, f)


def test_refills_below_40pct(tmp_path):
    p = str(tmp_path / "parlay.json")
    _write(p, 150.0)
    e = refill.refill_one(p, 500, NOW)
    assert e == {"t": NOW.isoformat(), "cash_before": 150.0, "added": 350.0}
    st = json.load(open(p))
    assert st["cash"] == 500 and st["refills"][0]["added"] == 350.0


def test_no_refill_at_or_above_40pct(tmp_path):
    p = str(tmp_path / "data.json")
    _write(p, 400.0)
    assert refill.refill_one(p, 1000, NOW) is None
    assert json.load(open(p))["cash"] == 400.0


def test_missing_file_is_skipped(tmp_path):
    assert refill.refill_one(str(tmp_path / "nope.json"), 1000, NOW) is None


def test_run_uses_config_and_env(tmp_path, monkeypatch):
    for name, (env, _, _) in refill.POTS.items():
        p = str(tmp_path / f"{name}.json")
        _write(p, 900.0 if name != "wild" else 100.0)
        monkeypatch.setenv(env, p)
    cfg = {"bankroll_start": 1000, "wild": {"bankroll": 1000}, "parlays": {"bankroll": 500}}
    ev = refill.run(NOW, cfg)
    assert ev == ["refill wild: cash 100.0 + 900.0"]
