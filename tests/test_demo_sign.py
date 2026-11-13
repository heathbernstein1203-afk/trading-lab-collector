import os, sys, json, base64, io, tempfile, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization, hashes
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
d = tempfile.mkdtemp()
os.environ.update(KALSHI_DEMO_KEY_ID="test-key", KALSHI_DEMO_PRIVATE_KEY=pem, BOT_DEMO_STATE=d + "/demo.json")
import bot
seen = []
class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass
def fake_urlopen(req, timeout=30):
    seen.append(req)
    assert "demo" in req.full_url and "/trade-api/v2/" in req.full_url, req.full_url
    h = {k.lower(): v for k, v in req.header_items()}
    msg = (h["kalshi-access-timestamp"] + req.get_method() + "/trade-api/v2" + req.full_url.split("/trade-api/v2")[1].split("?")[0]).encode()
    key.public_key().verify(base64.b64decode(h["kalshi-access-signature"]), msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    if req.get_method() == "GET":
        return Resp(json.dumps({"balance": 260000}).encode())
    body = json.loads(req.data)
    assert body["action"] == "buy" and body["type"] == "limit" and 1 <= (body.get("yes_price") or body.get("no_price")) <= 99
    return Resp(json.dumps({"order": {"order_id": "abc", **body}}).encode())
bot.urllib.request.urlopen = fake_urlopen
cfg = {"demo": {"on": True, "daily_budget": 100, "max_pct_balance": 0.25, "min_balance": 20}}
now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
bets = [{"id": "1", "ticker": "KXNFLGAME-X-ATL", "side": "yes", "status": "open", "price": 0.33, "limit": None, "qty": 61, "start": "2026-11-15T18:00:00+00:00"},
        {"id": "2", "ticker": "KXNFLGAME-X-TEN", "side": "no", "status": "pending", "price": None, "limit": 0.94, "qty": 200, "start": "2026-11-15T18:00:00+00:00"}]
out = bot.demo_mirror(cfg, bets, now)
print(out)
ds = json.load(open(d + "/demo.json"))
assert ds["days"]["2026-11-15"] <= 100.0 + 1e-6, ds
assert all(o["ok"] for o in ds["orders"])
print("demo signing + budget test passed; spent", ds["days"]["2026-11-15"])
