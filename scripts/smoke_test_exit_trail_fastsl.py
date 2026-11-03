"""Smoke test for the exit collapse-trail and fast-SL fast paths.

Two guards for a resting U/D SELL LIMIT when the premium collapses
below it (2026-10-23-style reversals: the limit waits, the SL watcher
cycles ~2s, and the market-out lands 7-18 points below the limit):

  _trail_exit_if_collapsed - re-prices the resting limit DOWN to a
  marketable price (LTP - limit margin) once the LTP falls
  EXIT_TRAIL_TRIGGER_PCT below the limit.

  _fast_stop_loss_exit - on a stop breach detected by the ~1s exit-fill
  watchdog, cancels the resting exit and market-sells using the same
  guarded sequence as the watcher (cancel first, verify live net, then
  sell - never a naked short).

Runs fully offline with a stub broker; no orders are placed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trade_logic import Trader_Singleton

t = Trader_Singleton()

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


class _StubSocket:
    def emit(self, *a, **k):
        pass


class _StubBroker:
    """Records every call; behavior flipped per test via flags."""

    def __init__(self):
        self.modify_calls = []
        self.modify_returns = True
        self.cancel_calls = []
        self.cancel_returns = (True, [])
        self.sell_calls = []
        self.pending_orders = [
            {"order_id": "EXIT-1", "tradingsymbol": "NIFTY26N0323300CE",
             "transaction_type": "SELL"},
        ]

    def modify_exit_limit(self, order_id, symbol, token, qty, exchange, price):
        self.modify_calls.append((order_id, price))
        return self.modify_returns

    def fetch_all_pending_orders(self):
        return self.pending_orders

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return self.cancel_returns

    def sell_units(self, symbol, token, qty, exchange, ltp, position_key=""):
        self.sell_calls.append((symbol, qty, ltp))


t.frontend_data_socket = _StubSocket()
t._mode = "LIVE"
t._broker = _StubBroker()
t._position_data = {}

KEY = "MSTOCK:EXIT-1"
INTENT = {
    "kind": "EXIT",
    "broker": "MSTOCK",
    "order_id": "EXIT-1",
    "trading_symbol": "NIFTY26N0323300CE",
    "instrument_token": "53940",
    "lots": 2,
    "exchange": "NFO",
    "strategy": "ULTRA_SCALPING",
    "mode": "D",
    "position_key": "53940:ULTRA_SCALPING:D",
    "anchor_used": 106.10,
    "registered_ts": 0.0,
}
LEG = INTENT["position_key"]
t._position_data[LEG] = {
    "tradingsymbol": INTENT["trading_symbol"],
    "instrument_token": "53940",
    "average_price": 102.00,
    "latest_price": 106.00,
    "net_quantity": 40,
    "lotsize": 20,
    "lots": 2,
    "order_strategy": "ULTRA_SCALPING",
}

t._five_weekly_option_contracts["53940"] = {"ltp": 106.0}

# Force the features ON regardless of the runtime appconfig.json.
import core.utils as utils


def fake_fetch(file, attr):
    return {
        "EXIT_TRAIL_ENABLED": True,
        "EXIT_TRAIL_TRIGGER_PCT": "1",
        "EXIT_FAST_SL_ENABLED": True,
        "COMPARISON_FUNCTION": ["PCT"],
        "PCT_STOP_LOSS_ULTRA_SCALPING": "2",
    }.get(attr)


utils.fetch_from_json = fake_fetch
import core.trade_logic as tl
tl.fetch_from_json = fake_fetch

# ---- 1. _intent_fresh_ltp: leg price preferred, chain fallback ----
check("fresh ltp from leg", t._intent_fresh_ltp(INTENT) == 106.0)
t._position_data[LEG]["latest_price"] = 0
t._five_weekly_option_contracts["53940"]["ltp"] = 105.5
check("fresh ltp falls back to chain", t._intent_fresh_ltp(INTENT) == 105.5)

# ---- 2. Trail: no trigger while LTP above limit*(1-pct) ----
t._position_data[LEG]["latest_price"] = 105.5  # 0.57% below 106.10
t._trail_exit_if_collapsed(INTENT, KEY)
check("no trail above trigger", t._broker.modify_calls == [])

# ---- 3. Trail: 1% below the limit re-prices down to LTP - margin ----
pre_trail_anchor = INTENT["anchor_used"]
t._position_data[LEG]["latest_price"] = 104.9  # 1.13% below 106.10
t._trail_exit_if_collapsed(INTENT, KEY)
check("trail modified once", len(t._broker.modify_calls) == 1)
order_id, new_price = t._broker.modify_calls[0]
check("trail uses the intent's order id", order_id == "EXIT-1")
check("trail re-prices DOWN below LTP", new_price < 104.9)
check("trail never re-prices UP", new_price < pre_trail_anchor)
check("intent anchor moved", INTENT["anchor_used"] == new_price)
check("leg exit-pending stamp follows", t._position_data[LEG].get("exit_pending_price") == new_price)

# ---- 4. Trail throttle: a second call within 2s is a no-op ----
t._position_data[LEG]["latest_price"] = 103.0
t._trail_exit_if_collapsed(INTENT, KEY)
check("trail throttled inside 2s", len(t._broker.modify_calls) == 1)

# ---- 5. Fast SL: no breach -> nothing happens ----
t._position_data[LEG]["latest_price"] = 100.5  # above SL 102*0.98=99.96
t._fast_stop_loss_exit(INTENT, KEY)
check("no fast SL above stop", t._broker.cancel_calls == [])

# ---- 6. Fast SL: breach -> cancel resting exit, then market sell ----
t._broker_net_qty_for_token = lambda token: 40  # live net covers the leg
t._position_data[LEG]["latest_price"] = 98.0  # below SL 99.96
t._fast_stop_loss_exit(INTENT, KEY)
check("fast SL cancelled the resting exit", t._broker.cancel_calls == ["EXIT-1"])
check("fast SL market-sold the leg", len(t._broker.sell_calls) == 1)
check("fast SL sold the full leg", t._broker.sell_calls[0][:2] == ("NIFTY26N0323300CE", 2))
check("fast SL retired the EXIT intent",
      t._pending_exit_intents.get(KEY) is None)

# ---- 7. Fast SL: partial cancel -> abort, no sell ----
INTENT2 = dict(INTENT, order_id="EXIT-2", position_key="53940:ULTRA_SCALPING:D2")
t._pending_exit_intents["MSTOCK:EXIT-2"] = INTENT2
t._broker.cancel_returns = (True, ["EXIT-2"])  # cancel FAILED
t._broker.pending_orders = [
    {"order_id": "EXIT-2", "tradingsymbol": "NIFTY26N0323300CE",
     "transaction_type": "SELL"},
]
t._broker.sell_calls = []
t._fast_stop_loss_exit(INTENT2, "MSTOCK:EXIT-2")
check("failed cancel aborts the fast sell", t._broker.sell_calls == [])

# ---- 8. Fast SL: live net zero (exit filled mid-flight) -> no sell ----
t._broker.cancel_returns = (True, [])
orig_net = getattr(t, "_broker_net_qty_for_token", None)
t._broker_net_qty_for_token = lambda token: 0
t._fast_stop_loss_exit(INTENT2, "MSTOCK:EXIT-2")
check("net-zero guard skips the sell", t._broker.sell_calls == [])
t._broker_net_qty_for_token = orig_net
t._pending_exit_intents.pop("MSTOCK:EXIT-2", None)

# ---- 9. PAPER mode: both fast paths are no-ops ----
t._mode = "PAPER"
t._broker.modify_calls = []
t._position_data[LEG]["latest_price"] = 90.0
t._trail_exit_if_collapsed(INTENT, KEY)
check("trail disabled outside LIVE", t._broker.modify_calls == [])
t._mode = "LIVE"

# ---- 10. Feature off via config -> no-op even on a collapse ----
utils.fetch_from_json = lambda file, attr: (
    False if attr == "EXIT_TRAIL_ENABLED" else fake_fetch(file, attr)
)
tl.fetch_from_json = utils.fetch_from_json
t._broker.modify_calls = []
t._position_data[LEG]["latest_price"] = 90.0
t._trail_exit_if_collapsed(INTENT, KEY)
check("trail disabled by config", t._broker.modify_calls == [])

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("ALL PASS")
