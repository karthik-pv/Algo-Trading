"""Smoke test for the resting-exit P&L cap + exit-pending marker.

2026-10-15 09:37: a U exit resting at 16.35 filled +3.15% while the
grid, fed by the 19.5 spike tick, showed +23% for the seconds before
the fill reconcile landed. The cap pins the displayed P&L at the
exit-anchored value while a U/D SELL LIMIT rests on the leg.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trade_logic import Trader_Singleton

t = Trader_Singleton()


class _StubSocket:
    def emit(self, *a, **k):
        self.emitted = getattr(self, "emitted", [])
        self.emitted.append(a[0] if a else None)


t.frontend_data_socket = _StubSocket()
t._position_data = {}

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


LEG = "1103064:ULTRA_SCALPING:U"
t._position_data[LEG] = {
    "tradingsymbol": "SENSEX26O1574200CE",
    "instrument_token": "1103064",
    "average_price": 15.85,
    "latest_price": 15.85,
    "net_quantity": 20,
    "lotsize": 20,
    "lots": 1,
    "order_strategy": "ULTRA_SCALPING",
    "sell_mode": "U",
}

# ---- 1. Raw LTP P&L without an exit marker ----
t.position_data_update("1103064", "latest_price", 19.5, is_token=True)
leg = t._position_data[LEG]
check("no marker: pct follows LTP",
      round(leg["pct_pl"], 2) == round((19.5 - 15.85) / 15.85 * 100, 2))

# ---- 2. Exit rests at 16.35: cap engages on the spike tick ----
t._set_leg_exit_pending(LEG, 16.35)
leg = t._position_data[LEG]
check("marker set", leg.get("exit_pending_price") == 16.35)
check("capped pct == exit pct",
      round(leg["pct_pl"], 2) == round((16.35 - 15.85) / 15.85 * 100, 2))
check("capped pct == +3.15", round(leg["pct_pl"], 2) == 3.15)
check("exit_pending_pct exposed",
      round(leg.get("exit_pending_pct", 0), 2) == 3.15)
check("capped pts == 0.5", round(leg["pts_pl"], 2) == 0.5)
check("capped total == pts x qty x lotsize",
      round(leg["total_pl"], 2) == round(0.5 * 20 * 20, 2))

# ---- 3. Tick below the limit: raw LTP P&L stays visible ----
t.position_data_update("1103064", "latest_price", 14.55, is_token=True)
leg = t._position_data[LEG]
check("below limit: pct follows LTP",
      round(leg["pct_pl"], 2) == round((14.55 - 15.85) / 15.85 * 100, 2))
check("below limit: still negative",
      leg["pct_pl"] < 0)

# ---- 4. Tick back above the limit: cap re-engages ----
t.position_data_update("1103064", "latest_price", 18.05, is_token=True)
leg = t._position_data[LEG]
check("above limit again: capped", round(leg["pct_pl"], 2) == 3.15)

# ---- 5. Clearing the marker lifts the cap ----
t._set_leg_exit_pending(LEG, None)
leg = t._position_data[LEG]
check("marker cleared", "exit_pending_price" not in leg)
check("cap lifted: pct follows LTP",
      round(leg["pct_pl"], 2) == round((18.05 - 15.85) / 15.85 * 100, 2))

# ---- 6. _legs_for_exit_intent resolution ----
intent = {
    "instrument_token": "1103064",
    "strategy": "ULTRA_SCALPING",
    "mode": "U",
    "position_key": LEG,
}
check("intent -> exact leg", t._legs_for_exit_intent(intent) == [LEG])
intent_no_key = {k: v for k, v in intent.items() if k != "position_key"}
check("intent -> normalized leg",
      t._legs_for_exit_intent(intent_no_key) == [LEG])
intent_wrong = dict(intent_no_key, mode="T")
# Single leg on the token: the uniqueness fallback still resolves it
# (same pattern as the fill-based average correction).
check("intent -> unique-token fallback",
      t._legs_for_exit_intent(intent_wrong) == [LEG])

# ---- 7. Restamp after refresh keeps a live exit's marker ----
t._set_leg_exit_pending(LEG, 16.35)
t._pending_exit_intents["MSTOCK:TESTEXIT"] = {
    "kind": "EXIT",
    "order_id": "TESTEXIT",
    "position_key": LEG,
    "instrument_token": "1103064",
    "strategy": "ULTRA_SCALPING",
    "mode": "U",
    "anchor_used": 16.35,
}
# Simulate the reconcile wiping the marker off the leg dict.
t._position_data[LEG].pop("exit_pending_price", None)
t._position_data[LEG].pop("exit_pending_pct", None)
t._restamp_leg_exits_after_refresh()
leg = t._position_data[LEG]
check("restamp restores marker", leg.get("exit_pending_price") == 16.35)
check("restamp restores cap", round(leg["pct_pl"], 2) == 3.15)
t._pending_exit_intents.pop("MSTOCK:TESTEXIT", None)

# ---- 8. Multi-leg token never stamps ambiguously ----
# A second leg on the same token + an intent matching NEITHER exact
# key: the fallback must yield nothing instead of guessing.
t._position_data["1103064:SCALPING:U"] = dict(t._position_data[LEG])
intent_other = dict(intent_no_key, strategy="INTRA", mode="T")
check("ambiguous token not stamped",
      t._legs_for_exit_intent(intent_other) == [])

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("ALL PASS")
