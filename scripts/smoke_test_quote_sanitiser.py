"""Verify closed-market quote sanitisation against the logged M.Stock garbage payload."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapter.mstock_adapter import _sanitise_closed_market_quote
from utils import is_market_open

print(f"is_market_open() = {is_market_open()}")  # Sunday morning -> False

# EXACT payload from today's log (garbage LTP, true close).
response = {
    "status": True, "message": "SUCCESS", "errorcode": "",
    "data": {"fetched": [{
        "exchange": "NFO", "tradingSymbol": "NIFTY", "symbolToken": "68407",
        "ltp": 25500, "open": 24700, "high": 25649, "low": 24700, "close": 23378.5,
    }], "unfetched": []},
}

out = _sanitise_closed_market_quote(response)
q = out["data"]["fetched"][0]

fails = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)

check("ltp -> close", q["ltp"] == 23378.5)
check("open -> close", q["open"] == 23378.5)
check("high -> close", q["high"] == 23378.5)
check("low -> close", q["low"] == 23378.5)
check("close preserved", q["close"] == 23378.5)

# Sane closed-market payload (ltp == close) must be a no-op.
sane = {
    "status": True, "data": {"fetched": [{
        "exchange": "NFO", "symbolToken": "57680",
        "ltp": 1242.15, "open": 1290.15, "high": 1290.15, "low": 1242.15, "close": 1242.15,
    }], "unfetched": []},
}
out2 = _sanitise_closed_market_quote(sane)
q2 = out2["data"]["fetched"][0]
check("sane quote untouched (open kept)", q2["open"] == 1290.15)

# No close present -> must not touch anything (avoid 0/None damage).
noclose = {
    "status": True, "data": {"fetched": [{
        "exchange": "NFO", "symbolToken": "1", "ltp": 25500, "open": 24700,
    }], "unfetched": []},
}
out3 = _sanitise_closed_market_quote(noclose)
q3 = out3["data"]["fetched"][0]
check("missing close -> untouched", q3["ltp"] == 25500)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("ALL SANITISER CHECKS PASSED")
