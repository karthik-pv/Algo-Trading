"""
Offline test for Trader_Singleton._sync_bp_resting_exit (Book-Profit %
edit reconcile). Exercises the decision paths against a mocked broker:

  1. one unfilled resting SELL, price differs  -> MODIFY in place
  2. one unfilled resting SELL, price matches  -> no-op (dedupe)
  3. no resting SELL                            -> fresh place
  4. TWO resting SELLs (legacy dupes)           -> cancel both + place one
  5. resting SELL partially filled              -> cancel + place
  6. resting SELL vanished + position flat      -> NO naked-short place
  7. overlapping events coalesce: second event re-reads the override
     AFTER the lock and reconciles to the latest pct in one action

Run:  python scripts/test_bp_exit_sync.py
"""
import sys
import os
import math
import types
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trade_logic import Trader_Singleton  # noqa: E402


class FakeBroker:
    def __init__(self, pending, positions):
        self._pending = pending
        self._positions = positions
        self.modify_calls = []
        self.place_calls = []
        self.cancel_calls = []
        self.modify_ok = True

    def fetch_all_pending_orders(self):
        return [dict(o) for o in self._pending]

    def modify_exit_limit(self, order_id, trading_symbol, instrument_token, quantity, exchange, price):
        self.modify_calls.append((order_id, trading_symbol, price))
        if self.modify_ok:
            for o in self._pending:
                if str(o.get("order_id")) == str(order_id):
                    o["price"] = price
            return True
        return False

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        before = len(self._pending)
        self._pending = [o for o in self._pending if str(o.get("order_id")) != str(order_id)]
        return len(self._pending) < before

    def place_exit_limit(self, symbol, token, qty, exchange, price):
        self.place_calls.append((symbol, qty, price))
        self._pending.append({
            "tradingsymbol": symbol, "transaction_type": "SELL",
            "order_id": f"NEW{len(self.place_calls)}", "price": price,
            "quantity": qty * 75, "filled_quantity": 0,
        })
        return f"NEW{len(self.place_calls)}"

    def fetch_all_positions(self):
        return {"net": [dict(p) for p in self._positions]}


def make_trader(broker, legs):
    """Build a minimal stand-in exposing only what the method uses."""
    t = object.__new__(Trader_Singleton)
    t._broker = broker
    t._exchange = "NSE"
    t._position_data = legs
    t.refresh_calls = 0

    def refresh():
        t.refresh_calls += 1
    t.refresh_open_pos_buy_price = refresh

    def exit_target(anchor, strategy, override_pct=None):
        # Mirror the real _exit_target_price: ceil to the 0.05 tick.
        raw = anchor * (1 + float(override_pct or 0) / 100.0)
        return round(math.ceil(raw / 0.05 - 1e-9) * 0.05, 2)
    t._exit_target_price = exit_target

    def net_qty(token):
        for p in broker._positions:
            if str(p.get("instrument_token")) == str(token):
                return int(float(p.get("quantity", 0) or 0))
        return 0
    t._broker_net_qty_for_token = net_qty

    def cancel_pending(symbol):
        pending = broker.fetch_all_pending_orders()
        if pending is None:
            raise RuntimeError("book unavailable")
        sym = symbol.upper()
        cancelled = failed = 0
        for o in list(pending):
            if str(o.get("tradingsymbol", "")).upper() != sym:
                continue
            if str(o.get("transaction_type", "")).upper() != "SELL":
                continue
            if broker.cancel_order(o.get("order_id")):
                cancelled += 1
            else:
                failed += 1
        return cancelled, failed
    t._cancel_pending_exit_orders = cancel_pending
    return t


def resting_sell(order_id, price, filled=0, qty=225):
    return {
        "tradingsymbol": "NIFTY-13Oct2026-22700-CE",
        "transaction_type": "SELL",
        "order_id": order_id,
        "price": price,
        "quantity": qty,
        "filled_quantity": filled,
    }


def leg(override=15.0):
    return {
        "position_key": "44620:SCALPING:D",
        "tradingsymbol": "NIFTY-13Oct2026-22700-CE",
        "instrument_token": "44620",
        "sell_mode": "D",
        "order_strategy": "SCALPING",
        "average_price": 3.7,
        "latest_price": 4.25,
        "net_quantity": 3,
        "lots": 3,
        "producttype": "CARRYFORWARD",
        "override_book_profit_pct": override,
    }


KEY = "44620:SCALPING:D"
FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


# --- 1. one unfilled resting SELL, price differs -> MODIFY in place ---
broker = FakeBroker([resting_sell("A1", 4.15)], [{"instrument_token": "44620", "quantity": 195}])
t = make_trader(broker, {KEY: leg(override=15.0)})
Trader_Singleton._sync_bp_resting_exit(t, KEY)  # 3.7 * 1.15 = 4.255 -> tick 4.30
check("1 modify in place", broker.modify_calls == [("A1", "NIFTY-13Oct2026-22700-CE", 4.30)]
      and not broker.place_calls and not broker.cancel_calls,
      f"modify={broker.modify_calls} place={broker.place_calls}")

# --- 2. resting price already matches -> no-op ---
broker = FakeBroker([resting_sell("A2", 4.30)], [{"instrument_token": "44620", "quantity": 195}])
t = make_trader(broker, {KEY: leg(override=15.0)})
Trader_Singleton._sync_bp_resting_exit(t, KEY)
check("2 price-match no-op", not broker.modify_calls and not broker.place_calls
      and not broker.cancel_calls,
      f"modify={broker.modify_calls} place={broker.place_calls} cancel={broker.cancel_calls}")

# --- 3. nothing resting -> fresh place ---
broker = FakeBroker([], [{"instrument_token": "44620", "quantity": 195}])
t = make_trader(broker, {KEY: leg(override=15.0)})
Trader_Singleton._sync_bp_resting_exit(t, KEY)
check("3 fresh place", len(broker.place_calls) == 1 and not broker.modify_calls,
      f"place={broker.place_calls}")

# --- 4. two resting SELLs (legacy dupes) -> cancel both + place one ---
broker = FakeBroker([resting_sell("D1", 4.6), resting_sell("D2", 4.7)],
                    [{"instrument_token": "44620", "quantity": 195}])
t = make_trader(broker, {KEY: leg(override=15.0)})
Trader_Singleton._sync_bp_resting_exit(t, KEY)
check("4 self-heal dupes", sorted(broker.cancel_calls) == ["D1", "D2"]
      and len(broker.place_calls) == 1 and not broker.modify_calls,
      f"cancel={broker.cancel_calls} place={broker.place_calls}")

# --- 5. partial fill -> cancel + place (modify path skipped) ---
broker = FakeBroker([resting_sell("P1", 4.3, filled=65)],
                    [{"instrument_token": "44620", "quantity": 130}])
t = make_trader(broker, {KEY: leg(override=15.0)})
Trader_Singleton._sync_bp_resting_exit(t, KEY)
check("5 partial fill falls back", broker.cancel_calls == ["P1"]
      and len(broker.place_calls) == 1 and not broker.modify_calls,
      f"cancel={broker.cancel_calls} place={broker.place_calls} modify={broker.modify_calls}")

# --- 6. exit filled mid-flight + flat position -> NO naked-short place ---
broker = FakeBroker([], [])  # order book empty, position flat
t = make_trader(broker, {KEY: leg(override=15.0)})
Trader_Singleton._sync_bp_resting_exit(t, KEY)
check("6 naked-short guard", not broker.place_calls and t.refresh_calls == 1,
      f"place={broker.place_calls} refresh_calls={t.refresh_calls}")

# --- 7. overlapping events coalesce on the latest override ---
broker = FakeBroker([resting_sell("C1", 4.3)], [{"instrument_token": "44620", "quantity": 195}])
t = make_trader(broker, {KEY: leg(override=15.0)})
slow = threading.Event()
real_modify = broker.modify_exit_limit


def slow_modify(*a, **k):
    slow.wait(2.0)  # hold the "broker" mid-modify like a slow HTTP call
    return real_modify(*a, **k)
broker.modify_exit_limit = slow_modify

t1 = threading.Thread(target=Trader_Singleton._sync_bp_resting_exit, args=(t, KEY))
t1.start()
import time  # noqa: E402
time.sleep(0.2)          # t1 holds the lock, mid-modify
t._position_data[KEY]["override_book_profit_pct"] = 30.0  # slider moved mid-flight
t2 = threading.Thread(target=Trader_Singleton._sync_bp_resting_exit, args=(t, KEY))
t2.start()
time.sleep(0.2)
slow.set()
t1.join(3)
t2.join(3)
# t1's event no-ops (resting 4.3 already equals the 15% tick target 4.30);
# t2 re-reads 30% and modifies ONCE to the final target 4.85.
check("7 coalesce to latest", broker.modify_calls == [("C1", "NIFTY-13Oct2026-22700-CE", 4.85)]
      and not broker.place_calls,
      f"modify={broker.modify_calls}")

# --- 8. U-mode BP edit anchors on the BUY price, not the LTP ---
u_leg = leg(override=12.0)
u_leg["sell_mode"] = "U"          # average 3.7, LTP 4.25
broker = FakeBroker([resting_sell("U1", 4.3)], [{"instrument_token": "44620", "quantity": 195}])
t = make_trader(broker, {KEY: u_leg})
Trader_Singleton._sync_bp_resting_exit(t, KEY)
# 3.7 * 1.12 = 4.144 -> ceil tick 4.15 (NOT 4.25 * 1.12 = 4.80)
check("8 U edit anchors on buy", broker.modify_calls == [("U1", "NIFTY-13Oct2026-22700-CE", 4.15)],
      f"modify={broker.modify_calls}")

# --- 9. fast buy update seeds the leg average with the EXECUTED fill ---
class _StubSocket:
    def emit(self, *a, **k):
        pass


t = object.__new__(Trader_Singleton)
t._position_data = {}
t._normalise_strategy = lambda s: str(s or "").strip().upper()
t._position_key = lambda tok, s, m: f"{tok}:{s}:{m}"
t._order_strategy_mapping = {}
t._order_sell_mode_mapping = {}
t._default_override_book_profit_pct = lambda s: 6.0
t.get_latest_price = lambda tok: 3.0          # stale in-memory LTP
t.calculate_point_difference = lambda a, b: b - a
t.calculate_pctg_difference = lambda a, b: (b - a) / a * 100.0
t.frontend_data_socket = _StubSocket()
t._save_position_ledger = lambda: None
Trader_Singleton.fast_update_position_after_buy(
    t,
    trading_symbol="NIFTY-13Oct2026-22700-CE",
    instrument_token="44620",
    quantity=4,
    lotsize=65,
    executed_buy_price=3.15,     # the real market fill
    exchange="NSE",
    strategy="SCALPING",
    sell_mode="D",
)
seeded = t._position_data.get("44620:SCALPING:D") or {}
check("9 leg avg seeded from fill", seeded.get("average_price") == 3.15
      and seeded.get("latest_price") == 3.0,
      f"leg={seeded.get('average_price')}, ltp={seeded.get('latest_price')}")

# --- 10. resolve_pending_exit corrects a stale leg average from the fill ---
t = object.__new__(Trader_Singleton)
t._pending_exit_intents = {}   # instance override (class attr is shared)
t._provisional_pending_orders = {}
t._position_data = {KEY: dict(leg(override=15.0), average_price=3.0)}
t._normalise_strategy = lambda s: str(s or "").strip().upper()
t._position_key = lambda tok, s, m: f"{tok}:{s}:{m}"
t._legs_for_token = lambda tok: [KEY]
t._exit_target_price = lambda a, s, override_pct=None: round(a * (1 + float(override_pct or 0) / 100.0), 2)
t.refresh_open_pos_buy_price = lambda: None
t.frontend_data_socket = _StubSocket()
t._broker = types.SimpleNamespace(
    fetch_order_executed_price=lambda oid: 3.15,
    place_exit_limit=lambda *a: "EXIT1",
)


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


t._pending_exit_lock = _Lock()
t._pending_exit_intents["MSTOCK:BUY1"] = {
    "broker": "MSTOCK", "order_id": "BUY1", "mode": "D",
    "anchor_ltp": 3.0, "strategy": "SCALPING",
    "trading_symbol": "NIFTY-13Oct2026-22700-CE",
    "instrument_token": "44620", "lots": 4, "exchange": "NSE",
    "override_book_profit_pct": 15.0, "registered_ts": 0.0,
}
Trader_Singleton.resolve_pending_exit(t, "MSTOCK", "BUY1", "COMPLETE", fill_price=3.15)
corrected = t._position_data[KEY]["average_price"]
exit_placed = t._broker.place_exit_limit
check("10 leg avg corrected from fill", corrected == 3.15,
      f"leg_avg={corrected}")

print()
if FAILURES:
    print(f"FAILED: {FAILURES}")
    sys.exit(1)
print("ALL PASS")
