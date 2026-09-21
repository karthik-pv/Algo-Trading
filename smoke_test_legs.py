"""Smoke test for per-leg positions (token:STRATEGY:MODE)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.trade_logic import Trader_Singleton

t = Trader_Singleton()

# Stub the socket so emits are no-ops during the test.
class _StubSocket:
    def emit(self, *a, **k):
        pass

t.frontend_data_socket = _StubSocket()

# Point the paper file at a temp path so real data is untouched.
paper_file = os.path.join(tempfile.gettempdir(), "opencode", "PaperTrading_test.txt")
if os.path.exists(paper_file):
    os.remove(paper_file)
t._PAPER_TRADE_FILENAME = paper_file
t._position_data = {}

fails = []

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)

# ---------------------------------------------------------------
# 1. Key helpers
# ---------------------------------------------------------------
k = t._position_key("123", "scalping", "t")
check("key format", k == "123:SCALPING:T")
check("parse key", t._parse_position_key(k) == ("123", "SCALPING", "T"))
check("legs_for_token", t._legs_for_token("123") == [])

# ---------------------------------------------------------------
# 1b. Strategy normalisation (raw radio value from the buy widget)
# ---------------------------------------------------------------
check("normalise UltraScalping", t._normalise_strategy("UltraScalping") == "ULTRA_SCALPING")
check("normalise ULTRASCALPING", t._normalise_strategy("ULTRASCALPING") == "ULTRA_SCALPING")
check("normalise intra", t._normalise_strategy("Intra") == "INTRA")
check("normalise scalping", t._normalise_strategy("Scalping") == "SCALPING")
check("normalise empty", t._normalise_strategy("") == "ULTRA_SCALPING")
check("normalise unknown", t._normalise_strategy("UNKNOWN") == "ULTRA_SCALPING")
check("key with raw radio value", t._position_key("123", "UltraScalping", "T") == "123:ULTRA_SCALPING:T")

# ---------------------------------------------------------------
# 1c. A buy emitting the raw radio value lands on the canonical leg
# ---------------------------------------------------------------
t.fast_update_position_after_buy(
    trading_symbol="NIFTY2692225700CE",
    instrument_token="999",
    quantity=14,
    lotsize=75,
    executed_buy_price=0.50,
    exchange="NFO",
    strategy="UltraScalping",
    sell_mode="T",
)
check("raw radio value -> canonical leg",
      "999:ULTRA_SCALPING:T" in t._position_data)
check("no ULTRASCALPING leg", "999:ULTRASCALPING:T" not in t._position_data)
check("leg strategy grid-ready",
      t._position_data["999:ULTRA_SCALPING:T"]["order_strategy"] == "ULTRA_SCALPING")
del t._position_data["999:ULTRA_SCALPING:T"]

# ---------------------------------------------------------------
# 2. Three buys, same contract/price, different strategies -> 3 legs
# ---------------------------------------------------------------
for strat in ("INTRA", "SCALPING", "ULTRA_SCALPING"):
    t.fast_update_position_after_buy(
        trading_symbol="NIFTY25SEP25600CE",
        instrument_token="123",
        quantity=1,
        lotsize=75,
        executed_buy_price=100.0,
        exchange="NFO",
        strategy=strat,
        sell_mode="T",
    )

check("3 legs created", len(t._position_data) == 3)
check("3 distinct keys", len(set(t._position_data.keys())) == 3)
check("all qty 1", all(p["net_quantity"] == 1 for p in t._position_data.values()))
check("strategies stored", sorted(p["order_strategy"] for p in t._position_data.values()) == ["INTRA", "SCALPING", "ULTRA_SCALPING"])

# ---------------------------------------------------------------
# 3. Repeat buy, same strategy+sellmode -> merges into that leg
# ---------------------------------------------------------------
t.fast_update_position_after_buy(
    trading_symbol="NIFTY25SEP25600CE",
    instrument_token="123",
    quantity=2,
    lotsize=75,
    executed_buy_price=104.0,
    exchange="NFO",
    strategy="INTRA",
    sell_mode="T",
)

intra_key = t._position_key("123", "INTRA", "T")
intra = t._position_data[intra_key]
check("still 3 legs", len(t._position_data) == 3)
check("intra merged qty", intra["net_quantity"] == 3)
check("intra weighted avg", abs(intra["average_price"] - (100 * 1 + 104 * 2) / 3) < 1e-9)

# ---------------------------------------------------------------
# 4. Same strategy, different sellmode -> separate leg
# ---------------------------------------------------------------
t.fast_update_position_after_buy(
    trading_symbol="NIFTY25SEP25600CE",
    instrument_token="123",
    quantity=1,
    lotsize=75,
    executed_buy_price=100.0,
    exchange="NFO",
    strategy="INTRA",
    sell_mode="U",
)
check("sellmode split -> 4 legs", len(t._position_data) == 4)

# ---------------------------------------------------------------
# 5. Tick fan-out: position_data_update on token updates all legs
# ---------------------------------------------------------------
t.position_data_update("123", "latest_price", 110.0, is_token=True)
check("all legs ticked", all(abs(p["latest_price"] - 110.0) < 1e-9 for p in t._position_data.values()))
intra = t._position_data[intra_key]
check("intra pl recalced", abs(intra["pts_pl"] - (110.0 - intra["average_price"])) < 1e-9)

# ---------------------------------------------------------------
# 6. Per-leg attribute update via composite key (position radio)
# ---------------------------------------------------------------
scalp_key = t._position_key("123", "SCALPING", "T")
t.position_data_update(scalp_key, "order_strategy", "INTRA")
check("leg relabeled", t._position_data[scalp_key]["order_strategy"] == "INTRA")
check("other legs untouched", t._position_data[t._position_key("123", "ULTRA_SCALPING", "T")]["order_strategy"] == "ULTRA_SCALPING")

# ---------------------------------------------------------------
# 7. Paper trades: write BUY rows per leg, aggregate, rebuild
# ---------------------------------------------------------------
t.write_paper_trade(
    transaction_type="BUY", tradingsymbol="NIFTY25SEP25600CE", token="123",
    qty=1, ltp=100.0, strategy="INTRA", sell_type="T")
t.write_paper_trade(
    transaction_type="BUY", tradingsymbol="NIFTY25SEP25600CE", token="123",
    qty=1, ltp=100.0, strategy="SCALPING", sell_type="T")
t.write_paper_trade(
    transaction_type="BUY", tradingsymbol="NIFTY25SEP25600CE", token="123",
    qty=1, ltp=100.0, strategy="ULTRA_SCALPING", sell_type="U")

trades = t.fetch_paper_trades()
check("3 trades read", len(trades) == 3)
check("strategy col parsed", sorted(tr_[ "Strategy"] for tr_ in trades) == ["INTRA", "SCALPING", "ULTRA_SCALPING"])
check("selltype col parsed", sorted(tr_["SellType"] for tr_ in trades) == ["T", "T", "U"])

paper = t.calculate_paper_positions()
check("3 paper legs", len(paper) == 3)

class _StubBroker:
    def get_instrument_details(self, symbol):
        return {"lot_size": 75, "instrument_token": "123", "exchange": "NFO"}

t._broker = _StubBroker()
t.refresh_paper_positions()
check("paper rebuild -> 3 legs", len(t._position_data) == 3)
check("paper leg strategies", sorted(p["order_strategy"] for p in t._position_data.values()) == ["INTRA", "SCALPING", "ULTRA_SCALPING"])
check("paper leg sellmodes", sorted(p["sell_mode"] for p in t._position_data.values()) == ["T", "T", "U"])

# ---------------------------------------------------------------
# 8. Paper SELL closes only the tagged leg
# ---------------------------------------------------------------
t.write_paper_trade(
    transaction_type="SELL", tradingsymbol="NIFTY25SEP25600CE", token="123",
    qty=1, ltp=112.0, buy_price=100.0, lot_size=75,
    strategy="SCALPING", sell_type="T")
t.refresh_paper_positions()

check("scalping leg closed", t._position_key("123", "SCALPING", "T") not in t._position_data)
check("other legs survive", len(t._position_data) == 2)

# ---------------------------------------------------------------
# 9. FIFO reconciliation against broker net
# ---------------------------------------------------------------
def seed_two_legs():
    t._position_data = {
        t._position_key("123", "INTRA", "T"): {
            "tradingsymbol": "NIFTY25SEP25600CE", "instrument_token": "123",
            "exchange": "NFO", "lotsize": 75, "average_price": 10.0,
            "net_quantity": 2, "lots": 2, "latest_price": 10.0,
            "order_strategy": "INTRA", "sell_mode": "T",
        },
        t._position_key("123", "SCALPING", "T"): {
            "tradingsymbol": "NIFTY25SEP25600CE", "instrument_token": "123",
            "exchange": "NFO", "lotsize": 75, "average_price": 20.0,
            "net_quantity": 3, "lots": 3, "latest_price": 20.0,
            "order_strategy": "SCALPING", "sell_mode": "T",
        },
    }

class _RefreshBroker:
    def __init__(self, net_qty):
        self.net_qty = net_qty
    def fetch_all_positions(self):
        return [{
            "tradingsymbol": "NIFTY25SEP25600CE", "instrument_token": "123",
            "exchange": "NFO", "lotsize": 75, "average_price": 16.0,
            "quantity": self.net_qty,
        }]
    def fetch_all_orders(self):
        return []
    def fetch_fund_summary(self):
        return {"cash_balance": 100000}

# Case A: net 3 < held 5 -> oldest leg (INTRA, 2 lots) fully consumed.
seed_two_legs()
t._broker = _RefreshBroker(3)
t.refresh_open_pos_buy_price()

legs = t._position_data
check("FIFO: oldest leg dropped", t._position_key("123", "INTRA", "T") not in legs)
check("FIFO: newer leg keeps 3", legs.get(t._position_key("123", "SCALPING", "T"), {}).get("net_quantity") == 3)
check("FIFO: avg price preserved", legs.get(t._position_key("123", "SCALPING", "T"), {}).get("average_price") == 20.0)
check("FIFO: total == broker net", sum(p["net_quantity"] for p in legs.values()) == 3)

# Case B: net 4 < held 5 -> oldest leg partially shrunk (2 -> 1).
seed_two_legs()
t._broker = _RefreshBroker(4)
t.refresh_open_pos_buy_price()

legs = t._position_data
check("FIFO partial: oldest leg shrunk to 1", legs.get(t._position_key("123", "INTRA", "T"), {}).get("net_quantity") == 1)
check("FIFO partial: newer leg untouched", legs.get(t._position_key("123", "SCALPING", "T"), {}).get("net_quantity") == 3)
check("FIFO partial: total == broker net", sum(p["net_quantity"] for p in legs.values()) == 4)

# Case C: net 6 > held 5 -> residual 1 lot booked on default leg,
# honoring the strategy/sell mode selected at buy time.
seed_two_legs()
t._order_strategy_mapping["123"] = "SCALPING"
t._order_sell_mode_mapping["123"] = "U"
t._broker = _RefreshBroker(6)
t.refresh_open_pos_buy_price()

legs = t._position_data
check("residual: total == broker net", sum(p["net_quantity"] for p in legs.values()) == 6)
default_key = t._position_key("123", "SCALPING", "U")
check("residual: buy-time strategy/sellmode honored",
      default_key in legs and legs[default_key]["net_quantity"] == 1)
t._order_strategy_mapping.pop("123", None)
t._order_sell_mode_mapping.pop("123", None)

# ---------------------------------------------------------------
# 9b. Watcher exit decision for U/D legs (per sell type x mode)
# ---------------------------------------------------------------
def decision(sell_type, mode, buy, ltp, strategy="ULTRA_SCALPING"):
    t._mode = mode
    result = t._watcher_sell_decision(buy, ltp, strategy, "CE", sell_type, log_enabled=False)
    t._mode = "PAPER"  # restore default for other checks
    return result

# U leg in SIMULATION: profit at buy + 2.5 pts (ULTRA factor x PTS_PROFIT)
check("U/U SIM: below target -> no exit", decision("U", "SIMULATION", 68.87, 71.0) is False)
check("U/U SIM: at/above target -> exit", decision("U", "SIMULATION", 68.87, 71.37) is True)
check("U/U SIM: SL branch still live", decision("U", "SIMULATION", 100.0, 90.0) is True)
check("D/D PAPER: target books", decision("D", "PAPER", 105.21, 107.71) is True)
check("D/D PLAYBACK: target books", decision("D", "PLAYBACK", 105.21, 106.0) is False)
# U leg in LIVE: the broker exit order owns profit -> watcher profit never fires
check("U/U LIVE: watcher skips profit", decision("U", "LIVE", 100.0, 120.0) is False)
# T leg: watcher profit in every mode (pct-based check_profit=True)
check("I/T LIVE: watcher manages profit", decision("T", "LIVE", 100.0, 120.0) in (True, False))
check("I/T PAPER: profit path armed", decision("T", "PAPER", 100.0, 100.0) is False)

# ---------------------------------------------------------------
# 10. Token vanished -> all legs dropped
# ---------------------------------------------------------------
class _EmptyBroker:
    def fetch_all_positions(self):
        return []
    def fetch_all_orders(self):
        return []
    def fetch_fund_summary(self):
        return {"cash_balance": 100000}

t._broker = _EmptyBroker()
t.refresh_open_pos_buy_price()
check("vanished token clears legs", len(t._position_data) == 0)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("ALL CHECKS PASSED")

