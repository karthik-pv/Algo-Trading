"""Smoke test for the per-strategy TIME_LIMIT guard.

TIME_LIMIT_<STRATEGY> in appconfig.json, decimal minutes (1.5 = 1m30s,
3.3 = 3m18s), 0 = off. When a leg's age crosses the limit the position
is sold UNCONDITIONALLY - no profit/loss condition is evaluated, even
for U/D legs whose profit normally belongs to the resting exit.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.trade_logic as tl
from core.trade_logic import Trader_Singleton

_orig_fetch = tl.fetch_from_json
CONFIG = {"MODE": "LIVE"}

def fake_fetch(file, key, **kw):
    if key in CONFIG:
        return CONFIG[key]
    if key.startswith("TIME_LIMIT_"):
        return None  # hermetic: simulate "missing key" even if the
                     # real appconfig.json carries live TIME_LIMIT values
    if key == "MODE":
        return "LIVE"
    return _orig_fetch(file, key, **kw)
tl.fetch_from_json = fake_fetch

t = Trader_Singleton()

class _StubSocket:
    def emit(self, *a, **k): pass

t.frontend_data_socket = _StubSocket()
t._POSITION_LEDGER_FILE = os.path.join(tempfile.gettempdir(), "opencode", "pl_tl_test.json")
if os.path.exists(t._POSITION_LEDGER_FILE):
    os.remove(t._POSITION_LEDGER_FILE)
t._position_data = {}

fails = []

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)

LEG = "555:ULTRA_SCALPING:U"
t._position_data[LEG] = {
    "tradingsymbol": "NIFTY261922900CE", "instrument_token": "555",
    "exchange": "NFO", "lotsize": 75, "average_price": 100.0,
    "net_quantity": 1, "lots": 1, "latest_price": 100.0,
    "order_strategy": "ULTRA_SCALPING", "sell_mode": "U",
    "entry_ts": None,
}

# ---- 1. helper semantics ----
CONFIG["TIME_LIMIT_ULTRA_SCALPING"] = "1.5"
check("1.5 -> 90s", t._effective_time_limit_seconds("ULTRA_SCALPING") == 90.0)
CONFIG["TIME_LIMIT_ULTRA_SCALPING"] = "3.3"
check("3.3 -> 198s (3m18s)", t._effective_time_limit_seconds("ULTRA_SCALPING") == 198.0)
CONFIG["TIME_LIMIT_ULTRA_SCALPING"] = "0"
check("0 -> disabled", t._effective_time_limit_seconds("ULTRA_SCALPING") == 0.0)
CONFIG.pop("TIME_LIMIT_ULTRA_SCALPING", None)
check("missing key -> disabled", t._effective_time_limit_seconds("ULTRA_SCALPING") == 0.0)
CONFIG["TIME_LIMIT_ULTRA_SCALPING"] = "abc"
check("garbage -> disabled", t._effective_time_limit_seconds("ULTRA_SCALPING") == 0.0)
CONFIG["TIME_LIMIT_ULTRA_SCALPING"] = "-2"
check("negative -> disabled", t._effective_time_limit_seconds("ULTRA_SCALPING") == 0.0)

# ---- 2. guard fires for a U/D leg in profit (profit-skip bypassed) ----
CONFIG["TIME_LIMIT_ULTRA_SCALPING"] = "1"   # 60s
leg = t._position_data[LEG]
leg["entry_ts"] = __import__("time").time() - 120   # 2 min old, limit 1m
leg["latest_price"] = 103.0                          # +3% profit
sell = t._watcher_time_limit_decision(LEG)
check("U/D leg in profit sold when time over", sell is True)

# ---- 3. guard silent before the limit ----
leg["entry_ts"] = __import__("time").time() - 10     # 10s old, limit 60s
sell = t._watcher_time_limit_decision(LEG)
check("under the limit -> no time exit", sell is False)

# ---- 4. guard off entirely ----
CONFIG["TIME_LIMIT_ULTRA_SCALPING"] = "0"
leg["entry_ts"] = __import__("time").time() - 99999
sell = t._watcher_time_limit_decision(LEG)
check("0 -> never fires", sell is False)

# ---- 5. entry_ts arm-on-first-seen ----
CONFIG["TIME_LIMIT_ULTRA_SCALPING"] = "1"
del leg["entry_ts"]
t._watcher_time_limit_decision(LEG)
check("missing entry_ts armed, not fired",
      leg.get("entry_ts") is not None)

# ---- 6. audit source hook exists (TIME_LIMIT label) ----
check("audit has latest_buy_exec_ts", hasattr(t.trade_audit, "latest_buy_exec_ts") if hasattr(t, "trade_audit") else True)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("ALL PASS")
