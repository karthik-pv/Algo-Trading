"""
Full-combination smoke test: 5 broker/mode cells x 4 instruments.

    Cells        Instruments
    ----         -----------
    LIVE-KITE    NIFTY      (NFO, lot 75)
    LIVE-MSTOCK  SENSEX     (BFO, lot 20)
    PAPER        CRUDEOIL   (MCX, lot 100)
    SIMULATION   CRUDEOILM  (MCX, lot 100)
    PLAYBACK

Every cell runs the regression battery that each production incident
came from: signature drift (target_profit_pct crash), quantity
semantics (MCX lots vs NFO/BFO units), fill-gated exits (naked-short
bug), paper sell tagging (phantom-short bug), fund-rejection retry
parsing (Kite + MStock RMS formats), and the Orders-tab export
(same-second clubbing KeyError).

Run:  ./venv/bin/python scripts/smoke_test_matrix.py
Exit code 0 = every cell green.
"""
import os
import sys
import time
import tempfile
import threading
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trade_logic import Trader_Singleton
from core import audit_log as _audit_module
import adapter.kite_adapter as _kite_module
import adapter.mstock_adapter as _mstock_module
from adapter.kite_adapter import KiteAdapter
from adapter.mstock_adapter import MStockAdapter
from adapter.simulator_adapter import SimulatorAdapter
from adapter.playback_adapter import PlaybackAdapter

INSTRUMENTS = {
    "NIFTY":     {"exchange": "NFO", "lot": 75,  "symbol": "NIFTY2692225700CE",     "token": "50001"},
    "SENSEX":    {"exchange": "BFO", "lot": 20,  "symbol": "SENSEX26O0173200CE",    "token": "50002"},
    "CRUDEOIL":  {"exchange": "MCX", "lot": 100, "symbol": "CRUDEOIL26OCT5900CE",   "token": "50003"},
    "CRUDEOILM": {"exchange": "MCX", "lot": 100, "symbol": "CRUDEOILM26OCT6000CE",  "token": "50004"},
}

t = Trader_Singleton()

fails = []
results = {}  # cell -> {instrument: [scenario results]}


def check(scenario, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    line = f"  {tag}  {scenario}" + (f" | {detail}" if detail and not cond else "")
    print(line)
    if not cond:
        fails.append(f"{scenario}" + (f" [{detail}]" if detail else ""))
    return cond


class StubSocket:
    def __init__(self):
        self.emitted = []
    def emit(self, event, *payload):
        self.emitted.append((event,) + payload)


class FakeKite:
    """Records place_order calls; raises scripted errors."""
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_LIMIT = "LIMIT"
    PRODUCT_MIS = "MIS"
    VARIETY_REGULAR = "regular"

    def __init__(self):
        self.orders = []
        self.error = None

    def place_order(self, **kw):
        if self.error:
            raise Exception(self.error)
        self.orders.append(kw)
        return f"ORD{len(self.orders)}"


class FakeMStockConn:
    """Scripted HTTP connection: pops one response per read()."""

    class _Resp:
        def __init__(self, body):
            self._body = body
        def read(self):
            return self._body

    def __init__(self, queue):
        self.queue = queue
        self.requests = []

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body))

    def getresponse(self):
        return self._Resp(self.queue.pop(0))

    def close(self):
        pass


class FakeMSInstance:
    _api_key = "K"
    _access_token = "T"


def reset_state(mode, exchange, broker):
    """Fresh trader state per scenario."""
    t._mode = mode
    t._exchange = exchange
    t._broker = broker
    t._position_data = {}
    t._five_weekly_option_contracts = {
        cfg["token"]: {
            "lotsize": cfg["lot"], "ltp": 100.0, "name": cfg["symbol"],
            "level": "ATM",
        } for cfg in INSTRUMENTS.values()
    }
    t.frontend_data_socket = StubSocket()
    Trader_Singleton._pending_exit_intents.clear()
    Trader_Singleton._paper_pending_exits.clear()
    paper_file = os.path.join(
        tempfile.gettempdir(), "opencode", f"paper_matrix_{exchange}_{mode}.txt"
    )
    if os.path.exists(paper_file):
        os.remove(paper_file)
    t._PAPER_TRADE_FILENAME = paper_file
    return paper_file


# Silence the audit trail (writes data/audit files) for the whole run.
for _m in ("record_buy_click", "record_buy_executed", "record_buy_avg_price",
           "record_sell_trigger", "record_sell_executed",
           "record_retry_attempt", "record_resting_exit_placed"):
    setattr(_audit_module.trade_audit, _m, lambda *a, **k: None)


def build_kite_adapter(trader, lot):
    ka = KiteAdapter.__new__(KiteAdapter)
    ka._trader = trader
    ka.kite = FakeKite()
    ka.kite_instance = None
    ka._underlying = "TEST"
    ka._instrument_cache = {}
    ka._instrument_data_loaded = {}
    ka._csv_loaded = True
    ka._csv_load_lock = threading.Lock()
    ka._instrument_meta = None
    ka.get_instrument_details = lambda ts: {"lot_size": lot, "tick_size": 0.05}
    ka._get_order_freeze_limit = lambda: 1800
    return ka


def build_mstock_adapter(trader, lot, responses):
    ma = MStockAdapter.__new__(MStockAdapter)
    ma._trader = trader
    ma.mstock_instance = FakeMSInstance()
    ma._underlying = "TEST"
    _mstock_module._MSTOCK_HTTP = type(
        "P", (), {"lease": lambda self, *a, **k: FakeMStockConn(responses)}
    )()
    ma._MSTOCK_INSTRUMENT_FILE = "x.json"
    ma.fetch_order_executed_price = lambda orderid: 103.15
    return ma


def ensure_playback_price_file():
    """PlaybackAdapter needs data/PlaybackPrice.csv (time,open,high,low,
    close); create a minimal one when missing so the engine can load."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "PlaybackPrice.csv")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("time,open,high,low,close\n")
            f.write("2026-10-01 09:15:00,100,101,99,100\n")
            f.write("2026-10-01 09:15:01,100,101,99,100.5\n")
    return path


def build_engine(adapter_cls, trader, lot):
    eng = adapter_cls()
    eng._trader = trader
    eng.get_instrument_details = lambda ts: {
        "instrument_token": "1", "lot_size": lot, "last_price": 100.0,
        "tradingsymbol": ts,
    }
    return eng


# =================================================================
# CELL: LIVE-KITE
# =================================================================
def cell_live_kite(instrument):
    cfg = INSTRUMENTS[instrument]
    lot = cfg["lot"]
    out = []

    ka = build_kite_adapter(t, lot)
    t._broker = ka
    reset_state("LIVE", cfg["exchange"], ka)

    # K1: signature + order quantity semantics (T mode: no intent)
    ka.kite.orders.clear()
    ka.buy_units(cfg["symbol"], cfg["token"], 2, cfg["exchange"], 100.0,
                 mode="LIVE", sell_mode="T", target_profit=0,
                 strategy="INTRA", target_profit_pct=3.0)
    expected_qty = 2 if cfg["exchange"] == "MCX" else 2 * lot
    out.append(check(
        "buy: kwarg + qty semantics",
        len(ka.kite.orders) == 1 and int(ka.kite.orders[0]["quantity"]) == expected_qty,
        f"got {[(o['quantity'], o['transaction_type']) for o in ka.kite.orders]}"
    ))
    out.append(check("buy: T mode places no exit intent",
                     not Trader_Singleton._pending_exit_intents))

    # K2: U buy registers intent, places NO exit (no naked exit)
    ka.kite.orders.clear()
    ka.buy_units(cfg["symbol"], cfg["token"], 2, cfg["exchange"], 100.0,
                 mode="LIVE", sell_mode="U", target_profit=0,
                 strategy="INTRA", target_profit_pct=3.0)
    intents = dict(Trader_Singleton._pending_exit_intents)
    out.append(check(
        "U buy: fill-gated intent, no exit yet",
        len(intents) == 1
        and list(intents.values())[0]["mode"] == "U"
        and list(intents.values())[0]["anchor_ltp"] == 100.0
        and len(ka.kite.orders) == 1,
        f"intents={intents}, orders={len(ka.kite.orders)}"
    ))

    # K3: COMPLETE resolves the U exit at the click-LTP anchor
    oid = list(intents.values())[0]["order_id"]
    ka.kite.orders.clear()
    t.resolve_pending_exit("KITE", oid, "COMPLETE", fill_price=None)
    u_price = t._exit_target_price(100.0, "INTRA")
    out.append(check(
        "U exit on fill: anchored at click LTP",
        len(ka.kite.orders) == 1
        and ka.kite.orders[0]["transaction_type"] == "SELL"
        and abs(ka.kite.orders[0]["price"] - u_price) < 0.01
        and not Trader_Singleton._pending_exit_intents,
        f"orders={ka.kite.orders}, want {u_price}"
    ))

    # K3b: D exit anchors the executed fill price
    ka.kite.orders.clear()
    ka.buy_units(cfg["symbol"], cfg["token"], 2, cfg["exchange"], 100.0,
                 mode="LIVE", sell_mode="D", target_profit=0,
                 strategy="INTRA", target_profit_pct=3.0)
    oid = list(Trader_Singleton._pending_exit_intents.values())[0]["order_id"]
    t.resolve_pending_exit("KITE", oid, "COMPLETE", fill_price=104.0)
    d_price = t._exit_target_price(104.0, "INTRA")
    sells = [o for o in ka.kite.orders if o["transaction_type"] == "SELL"]
    out.append(check(
        "D exit on fill: anchored at executed price",
        len(sells) == 1 and abs(sells[0]["price"] - d_price) < 0.01,
        f"orders={ka.kite.orders}, want {d_price}"
    ))

    # K4: REJECTED drops the intent - no naked exit (the BUY order
    # record itself is expected: FakeKite accepts, the rejection is
    # the async order-update event)
    ka.kite.orders.clear()
    ka.buy_units(cfg["symbol"], cfg["token"], 2, cfg["exchange"], 100.0,
                 mode="LIVE", sell_mode="U", target_profit=0,
                 strategy="INTRA", target_profit_pct=3.0)
    oid = list(Trader_Singleton._pending_exit_intents.values())[0]["order_id"]
    t.resolve_pending_exit("KITE", oid, "REJECTED")
    sells = [o for o in ka.kite.orders if o["transaction_type"] == "SELL"]
    out.append(check(
        "rejected buy: exit dropped (no naked short)",
        not Trader_Singleton._pending_exit_intents and not sells,
        f"intents={len(Trader_Singleton._pending_exit_intents)}, sells={len(sells)}"
    ))

    # K5: the send_pending sweep recovers missed fill events
    ka.buy_units(cfg["symbol"], cfg["token"], 2, cfg["exchange"], 100.0,
                 mode="LIVE", sell_mode="U", target_profit=0,
                 strategy="INTRA", target_profit_pct=3.0)
    ka.fetch_order_status = lambda oid: "COMPLETE"
    ka.kite.orders.clear()
    t._sweep_pending_exits()
    out.append(check(
        "sweep: late fill gets its exit",
        len(ka.kite.orders) == 1 and not Trader_Singleton._pending_exit_intents,
        f"orders={len(ka.kite.orders)}"
    ))

    # K6: RMS rejections re-raise (retry machinery sees them)
    ka.kite.error = ("Insufficient funds. Margin required: 1119.00. "
                     "Margin available: 1025.30. Add 93.70 to place this order.")
    raised = False
    try:
        ka.buy_units(cfg["symbol"], cfg["token"], 1, cfg["exchange"], 100.0,
                     mode="LIVE", sell_mode="T", target_profit=0,
                     strategy="INTRA", target_profit_pct=None)
    except Exception as e:
        raised = "Insufficient funds" in str(e)
    ka.kite.error = None
    out.append(check("RMS rejection re-raises to retry layer", raised))

    # K7: iceberg branch runs without crash on huge quantity
    ka.kite.orders.clear()
    huge = 1800 * (3 if cfg["exchange"] != "MCX" else 1) * 2
    try:
        ka.buy_units(cfg["symbol"], cfg["token"], huge, cfg["exchange"], 100.0,
                     mode="LIVE", sell_mode="T", target_profit=0,
                     strategy="INTRA", target_profit_pct=None)
        ok = True
    except Exception:
        ok = False
    out.append(check("freeze-limit/iceberg path no crash", ok))
    return out


# =================================================================
# CELL: LIVE-MSTOCK
# =================================================================
def cell_live_mstock(instrument):
    cfg = INSTRUMENTS[instrument]
    lot = cfg["lot"]
    out = []
    ok_buy = b'{"status": "ok", "data": {"orderid": "M1"}}'
    ok_exit = b'{"status": "ok", "data": {"orderid": "M2"}}'

    # M1: offline end-to-end U buy: intent -> fill poll -> resting exit
    captured = []

    class CapConn(FakeMStockConn):
        def request(self, *a, **k):
            captured.append(a)
            super().request(*a, **k)

    responses2 = [ok_buy, ok_exit]
    ma2 = build_mstock_adapter(t, lot, responses2)
    t._broker = ma2
    _mstock_module._MSTOCK_HTTP = type(
        "P2", (), {"lease": lambda self, *a, **k: CapConn(responses2)}
    )()
    try:
        ma2.buy_units(trading_symbol=cfg["symbol"], instrument_token=cfg["token"],
                      quantity=2, exchange=cfg["exchange"], ltp=100.0,
                      mode="LIVE", sell_mode="U", target_profit=0,
                      strategy="INTRA", target_profit_pct=None)
        sells = [r for r in captured if '"SELL"' in str(r) or ", 'SELL'" in str(r)]
        out.append(check(
            "U buy: resting SELL LIMIT placed after fill poll",
            len(captured) == 2 and len(sells) == 1
            and not Trader_Singleton._pending_exit_intents,
            f"requests={len(captured)}, sells={len(sells)}"
        ))
    except Exception as e:
        out.append(check("U buy: resting SELL LIMIT placed", False, str(e)))

    # M2: sync RMS rejection raises; no intent, no exit
    captured.clear()
    responses3 = [b'{"status": false, "message": "FUND LIMIT INSUFFICIENT, AVAILABLE FUND =1025.30, ADDITIONAL REQUIRED FUND =93.70, CALCULATED SPAN & EXPOSURE FOR ORDER =2238.00"}']
    ma3 = build_mstock_adapter(t, lot, responses3)
    t._broker = ma3
    raised = False
    try:
        ma3.buy_units(trading_symbol=cfg["symbol"], instrument_token=cfg["token"],
                      quantity=2, exchange=cfg["exchange"], ltp=100.0,
                      mode="LIVE", sell_mode="U", target_profit=0,
                      strategy="INTRA", target_profit_pct=None)
    except Exception as e:
        raised = "FUND LIMIT INSUFFICIENT" in str(e).upper()
    out.append(check(
        "rejected buy: raises, no naked exit",
        raised and not Trader_Singleton._pending_exit_intents and not captured[1:],
        f"raised={raised}, extra_requests={len(captured)-1}"
    ))

    # M3: margin estimate sane
    est = ma3.fetch_order_margin(cfg["symbol"], cfg["exchange"], 2 * lot, 100.0)
    out.append(check(
        "margin estimate: turnover < est < 1.05x",
        est is not None and 200 * lot < est < 210 * lot,
        f"est={est}"
    ))
    return out


# =================================================================
# CELL: PAPER
# =================================================================
def cell_paper(instrument):
    cfg = INSTRUMENTS[instrument]
    lot = cfg["lot"]
    out = []

    class FakeBroker:
        def fetch_all_pending_orders(self):
            return []
        def get_instrument_details(self, ts):
            return {"lot_size": lot, "instrument_token": cfg["token"]}
        def fetch_order_executed_price(self, oid):
            return None
        def fetch_fund_summary(self):
            return {"cash_balance": 100000.0}

    fb = FakeBroker()
    paper_file = reset_state("PAPER", cfg["exchange"], fb)
    t._five_weekly_option_contracts[cfg["token"]]["ltp"] = 100.0

    # P1: paper U buy -> virtual pending exit with correct quantity
    t.write_paper_trade(transaction_type="BUY", tradingsymbol=cfg["symbol"],
                        token=cfg["token"], qty=2, ltp=100.0, product="MIS",
                        strategy="ULTRA_SCALPING", sell_type="U")
    t.refresh_paper_positions()
    t._register_paper_exit_after_buy(cfg["symbol"], cfg["token"], 2, 100.0,
                                     "ULTRA_SCALPING", "U")
    exits = t._paper_pending_exit_rows()
    expected_qty = 2 if cfg["exchange"] == "MCX" else 2 * lot
    out.append(check(
        "U buy: virtual pending exit, qty semantics",
        len(exits) == 1 and exits[0]["quantity"] == expected_qty
        and exits[0]["transaction_type"] == "SELL"
        and exits[0]["order_type"] == "LIMIT",
        f"rows={exits}"
    ))

    # P2: no fill below target; fills AT target at the limit price
    t._five_weekly_option_contracts[cfg["token"]]["ltp"] = 100.5
    t._check_paper_pending_exits()
    below_ok = len(t._paper_pending_exits) == 1
    exit_price = exits[0]["price"]
    t._five_weekly_option_contracts[cfg["token"]]["ltp"] = exit_price
    t._check_paper_pending_exits()
    sells = [l for l in open(paper_file).read().splitlines() if "SELL" in l]
    out.append(check(
        "exit fills at limit price when LTP crosses",
        below_ok and len(t._paper_pending_exits) == 0 and len(sells) == 1
        and f"{exit_price:.2f}" in sells[0],
        f"below_ok={below_ok}, sells={sells}"
    ))

    # P2b: the position leg is closed after the fill
    out.append(check(
        "fill closes the position leg",
        t._paper_exit_leg_key(cfg["token"]) is None,
        f"position_data={t._position_data}"
    ))

    # P3: RETAGGED leg (order_strategy mutated by the radio) - manual
    # sell must tag from the KEY so it closes the BUY's leg (no
    # phantom short)
    t.write_paper_trade(transaction_type="BUY", tradingsymbol=cfg["symbol"],
                        token=cfg["token"], qty=2, ltp=100.0, product="MIS",
                        strategy="ULTRA_SCALPING", sell_type="U")
    t.refresh_paper_positions()
    leg_key = t._paper_exit_leg_key(cfg["token"])
    leg = t._position_data[leg_key]
    leg["order_strategy"] = "INTRA"  # the radio-change mutation
    tags = Trader_Singleton._paper_sell_file_tags(leg_key, leg)
    out.append(check(
        "retagged leg: file tags from KEY",
        tags == ("ULTRA_SCALPING", "U"),
        f"tags={tags}"
    ))
    t.write_paper_trade(transaction_type="SELL", tradingsymbol=cfg["symbol"],
                        token=cfg["token"], qty=2, ltp=101.0, product="MIS",
                        buy_price=leg["average_price"],
                        lot_size=leg.get("lotsize", lot),
                        buy_time=None, strategy=tags[0], sell_type=tags[1])
    t.refresh_paper_positions()
    out.append(check(
        "retagged sell closes the BUY leg (no phantom short)",
        t._paper_exit_leg_key(cfg["token"]) is None,
        f"position_data={list(t._position_data)}"
    ))

    # P4: partial manual sell reduces the pending exit; full sells cancel it
    t.write_paper_trade(transaction_type="BUY", tradingsymbol=cfg["symbol"],
                        token=cfg["token"], qty=2, ltp=100.0, product="MIS",
                        strategy="SCALPING", sell_type="U")
    t.refresh_paper_positions()
    t._register_paper_exit_after_buy(cfg["symbol"], cfg["token"], 2, 100.0,
                                     "SCALPING", "U")
    t._reduce_paper_exits_after_manual_sell(cfg["token"], 2, 1)
    exits = t._paper_pending_exit_rows()
    out.append(check(
        "partial sell: pending exit reduced to remaining lots",
        len(exits) == 1 and exits[0]["quantity"] == (1 if cfg["exchange"] == "MCX" else 1 * lot),
        f"rows={exits}"
    ))
    t._reduce_paper_exits_after_manual_sell(cfg["token"], 1, 1)
    out.append(check(
        "full sell: pending exit cancelled",
        not t._paper_pending_exits
    ))
    return out


# =================================================================
# CELL: SIMULATION / PLAYBACK
# =================================================================
def cell_engine(adapter_cls, mode_name, instrument):
    cfg = INSTRUMENTS[instrument]
    lot = cfg["lot"]
    out = []

    if adapter_cls is PlaybackAdapter:
        ensure_playback_price_file()
    eng = build_engine(adapter_cls, t, lot)
    reset_state(mode_name, cfg["exchange"], eng)

    # E1: signature + order lifecycle
    try:
        eng.buy_units(cfg["symbol"], cfg["token"], 2, cfg["exchange"], 100.0,
                      mode=mode_name, sell_mode="T", target_profit=0,
                      strategy="INTRA", target_profit_pct=3.0)
        eng.buy_units(cfg["symbol"], cfg["token"], 1, cfg["exchange"], 100.0,
                      mode=mode_name, sell_mode="U", target_profit=0,
                      strategy="SCALPING", target_profit_pct=None)
        ok = True
    except Exception as e:
        ok = False
        out.append(check("buy with target_profit_pct", False, str(e)))
        return out
    out.append(check("buy with target_profit_pct (T + U) no crash", ok))

    # E2: sell closes; pending list sane
    try:
        eng.sell_units(cfg["symbol"], cfg["token"], 3, cfg["exchange"], 100.0)
        pending = eng.fetch_all_pending_orders()
        out.append(check("sell + pending list format", isinstance(pending, list)))
    except Exception as e:
        out.append(check("sell + pending list format", False, str(e)))

    if mode_name == "PLAYBACK":
        # B-specific: the engine raises the MStock-format fund error the
        # retry machinery understands
        eng._cash = 1.0
        raised = False
        try:
            eng.buy_units(cfg["symbol"], cfg["token"], 5, cfg["exchange"], 100.0,
                          mode=mode_name, sell_mode="T", target_profit=0,
                          strategy="INTRA", target_profit_pct=None)
        except Exception as e:
            raised = Trader_Singleton._is_fund_limit_rejection(str(e))
        out.append(check("insufficient-cash rejection feeds retry layer", raised))
    return out


# =================================================================
# GLOBAL (broker/instrument agnostic) - run once
# =================================================================
def cell_global():
    out = []
    kite_msg = ("Insufficient funds. Margin required: 1119.00. "
                "Margin available: 1025.30. Add 93.70 to place this order.")
    mstock_msg = ("...B,20,M,29.3,FUND LIMIT INSUFFICIENT,AVAILABLE FUND "
                  "=1025.30,ADDITIONAL REQUIRED FUND =93.70,CALCULATED SPAN "
                  "& EXPOSURE FOR ORDER =2238.00")
    out.append(check("rejection: Kite format detected",
                     Trader_Singleton._is_fund_limit_rejection(kite_msg)))
    out.append(check("rejection: MStock format detected",
                     Trader_Singleton._is_fund_limit_rejection(mstock_msg)))
    out.append(check("rejection: unrelated text not matched",
                     not Trader_Singleton._is_fund_limit_rejection("Network error")))
    out.append(check("retry math: Kite fail-fast at 0 lots",
                     t._smart_retry_lots(kite_msg, 1) == 0))
    out.append(check("retry math: Kite right-size 2->1",
                     t._smart_retry_lots("Insufficient funds. Margin required: 2238.00. Margin available: 1500.00. Add 738.00", 2) == 1))
    out.append(check("retry math: MStock fail-fast at 0 lots",
                     t._smart_retry_lots(mstock_msg, 2) == 0))
    out.append(check("retry math: unparseable falls back (-1)",
                     t._smart_retry_lots("weird", 2) == -1))

    from adapter.mstock_utils import build_orders_export
    import pandas as pd
    df = pd.DataFrame([
        {"timestamp": "10/1/2026 17:28:46", "tradingsymbol": "X",
         "transaction_type": "SELL", "quantity": 97, "average_price": 108.35},
        {"timestamp": "10/1/2026 17:28:46", "tradingsymbol": "X",
         "transaction_type": "SELL", "quantity": 97, "average_price": 108.35},
    ])
    try:
        rows, _ = build_orders_export(df.to_dict(orient="records"))
        out.append(check("orders export: same-second clubbing no crash",
                         len(rows) == 1 and abs(rows[0]["Qty."] - 194) < 0.01))
    except Exception as e:
        out.append(check("orders export: same-second clubbing no crash", False, str(e)))

    cfg_flag = t._pre_margin_check_enabled()
    out.append(check("pre-margin flag reads config (bool or string)",
                     isinstance(cfg_flag, bool)))
    return out


# =================================================================
# CELL: INTERFACE PARITY (the bug class that started the session:
# KiteAdapter.buy_units missing the target_profit_pct kwarg the
# trader passes - every adapter must accept every trader call)
# =================================================================
TRADER_BUY_KWARGS = dict(
    trading_symbol="SYM", instrument_token="1", quantity=2,
    exchange="BFO", ltp=100.0, mode="LIVE", sell_mode="U",
    target_profit=0, strategy="INTRA", target_profit_pct=3.0,
)
TRADER_SELL_KWARGS = dict(
    trading_symbol="SYM", instrument_token="1", quantity=2,
    exchange="BFO", ltp=100.0, position_key="1:INTRA:T",
)
ADAPTER_CONTRACT = {
    KiteAdapter: ["fetch_order_status", "fetch_order_executed_price",
                  "fetch_order_margin", "place_exit_limit",
                  "fetch_all_pending_orders", "cancel_order",
                  "cancel_all_pending_orders", "fetch_fund_summary",
                  "get_instrument_details", "get_ltp"],
    MStockAdapter: ["fetch_order_status", "fetch_order_executed_price",
                    "fetch_order_margin", "place_exit_limit",
                    "fetch_all_pending_orders", "cancel_order",
                    "cancel_all_pending_orders", "fetch_fund_summary",
                    "get_instrument_details", "get_ltp"],
    SimulatorAdapter: ["fetch_all_pending_orders", "cancel_order",
                       "cancel_all_pending_orders", "fetch_fund_summary",
                       "get_instrument_details"],
    PlaybackAdapter: ["fetch_all_pending_orders", "cancel_order",
                      "cancel_all_pending_orders", "fetch_fund_summary",
                      "get_instrument_details"],
}


def cell_interface_parity():
    out = []
    for adapter_cls, methods in ADAPTER_CONTRACT.items():
        name = adapter_cls.__name__
        # The trader's exact buy calls must bind on every adapter.
        # sig.bind(None, ...) binds `self` positionally (the class-level
        # signature is unbound).
        for method, kwargs in (
            ("buy_units", TRADER_BUY_KWARGS),
            ("buy_sell_units", TRADER_BUY_KWARGS),
            ("sell_units", TRADER_SELL_KWARGS),
        ):
            sig = inspect.signature(getattr(adapter_cls, method))
            try:
                sig.bind(None, **kwargs)
                ok = True
            except TypeError:
                ok = False
            out.append(check(f"{name}.{method} accepts the trader call", ok))
        for method in methods:
            out.append(check(
                f"{name}.{method} exists",
                hasattr(adapter_cls, method),
            ))
    return out


# =================================================================
# CELL: DECISION MAKER (SL/book-profit logic, PCT + PNT paths,
# overrides, U/D semantics per mode)
# =================================================================
def cell_decision():
    out = []
    t._mode = "LIVE"

    # Thresholds read the live config the same way the code does, so
    # the assertions hold whatever the configured values are.
    strategy = "INTRA"
    bp = float(t._strategy_threshold("PCT_BOOK_PROFIT", strategy) or 0)
    sl = float(t._strategy_threshold("PCT_STOP_LOSS", strategy) or 0)
    bp = bp if bp > 0 else 12.0

    # T leg: profit above the book-profit threshold -> sell
    out.append(check(
        "T: profit beyond threshold books",
        t.to_sell_or_not_to_sell_SL(100.0, 100.0 * (1 + bp / 100) + 0.5,
                                    strategy, "CE") is True,
    ))
    # T leg: within thresholds -> no sell
    out.append(check(
        "T: inside thresholds holds",
        t.to_sell_or_not_to_sell_SL(100.0, 101.0, strategy, "CE") is False,
    ))
    # T leg: loss beyond the SL threshold -> sell
    if sl > 0:
        out.append(check(
            "T: loss beyond SL threshold sells",
            t.to_sell_or_not_to_sell_SL(100.0, 100.0 * (1 - sl / 100) - 0.5,
                                        strategy, "CE") is True,
        ))
    # Override: BK% = 0 disables profit booking for the leg
    out.append(check(
        "T: BK% override 0 disables profit booking",
        t.to_sell_or_not_to_sell_SL(100.0, 100.0 * (1 + bp / 100) + 0.5,
                                    strategy, "CE",
                                    override_book_profit_pct=0) is False,
    ))
    # Override: a tighter BK% books earlier
    out.append(check(
        "T: BK% override books at its own level",
        t.to_sell_or_not_to_sell_SL(100.0, 103.0, strategy, "CE",
                                    override_book_profit_pct=2.5) is True,
    ))
    # U/D LIVE: profit alone must NOT trigger (the resting exit owns
    # profit) - only the SL safety net
    out.append(check(
        "U/D LIVE: profit does not trigger (exit owns profit)",
        t._watcher_sell_decision(100.0, 100.0 * (1 + bp / 100) + 0.5,
                                 strategy, "CE", "U") is False,
    ))
    # U/D PAPER: the points target applies (watcher-managed exit)
    pts = float(t._target_profit_points(strategy) or 0)
    if pts > 0:
        t._mode = "PAPER"
        out.append(check(
            "U/D PAPER: points target triggers",
            t._watcher_sell_decision(100.0, 100.0 + pts + 0.5,
                                     strategy, "CE", "U") is True,
        ))
        t._mode = "LIVE"
    return out


# =================================================================
# CELL: SIZING & PNL MATH
# =================================================================
def cell_sizing_pnl():
    out = []

    # LIVE sizing: (cash x margin_pct) / (price + buffer) / lot
    t._mode = "LIVE"
    t._fund_summary = {"cash_balance": 100000.0}
    import core.utils as _cu
    for instrument, cfg in INSTRUMENTS.items():
        lots = t._calculate_option_lots(50.0, cfg["lot"])
        buffered = 50.0 + _cu.compute_limit_margin(50.0)
        expected = int((100000.0 * 1.0 / buffered) / cfg["lot"])
        out.append(check(
            f"sizing: {instrument} lots math",
            lots == max(expected, 1) or lots == expected,
            f"got {lots}, expected {expected}"
        ))
    # Zero cash -> 1 lot fallback (LIVE)
    t._fund_summary = {"cash_balance": 0}
    out.append(check(
        "sizing: zero cash falls back to 1 lot (LIVE)",
        t._calculate_option_lots(50.0, 20) == 1,
    ))

    # PnL math on a synthetic leg
    t._position_data = {"T1:INTRA:T": {
        "average_price": 100.0, "latest_price": 110.0,
        "net_quantity": 2, "lotsize": 20,
    }}
    t._update_leg_pl("T1:INTRA:T")
    leg = t._position_data["T1:INTRA:T"]
    out.append(check(
        "PnL: pts/pct/total on a leg",
        abs(leg["pts_pl"] - 10.0) < 1e-9
        and abs(leg["pct_pl"] - 10.0) < 1e-9
        and abs(leg["total_pl"] - 400.0) < 1e-9,
        f"leg={leg}"
    ))
    # PnL: losing leg
    t._position_data["T1:INTRA:T"]["latest_price"] = 90.0
    t._update_leg_pl("T1:INTRA:T")
    leg = t._position_data["T1:INTRA:T"]
    out.append(check(
        "PnL: losing leg math",
        abs(leg["pts_pl"] + 10.0) < 1e-9
        and abs(leg["total_pl"] + 400.0) < 1e-9,
        f"leg={leg}"
    ))
    return out


# =================================================================
# CELL: UTILS (exchange map, symbol parsing, tick/limit math)
# =================================================================
def cell_utils():
    out = []
    from core.utils import (
        get_exchange_for_underlying, get_underlying_for_symbol,
        is_option_instrument, round_to_tick, compute_limit_margin,
    )

    # Every instrument in this matrix maps to its exchange
    for underlying, exchange in (("NIFTY", "NFO"), ("SENSEX", "BFO"),
                                 ("CRUDEOIL", "MCX"), ("CRUDEOILM", "MCX")):
        out.append(check(
            f"exchange map: {underlying}",
            get_exchange_for_underlying(underlying) == exchange,
        ))

    # Longest-prefix symbol parsing (CRUDEOILM must win over CRUDEOIL)
    for symbol, underlying in (
        ("SENSEX26O0173200CE", "SENSEX"),
        ("NIFTY2692225700CE", "NIFTY"),
        ("CRUDEOIL26OCT5900CE", "CRUDEOIL"),
        ("CRUDEOILM26OCT6000CE", "CRUDEOILM"),
        ("SENSEX26OCTFUT", "SENSEX"),
    ):
        out.append(check(
            f"symbol parse: {symbol} -> {underlying}",
            get_underlying_for_symbol(symbol) == underlying,
        ))

    # Option detection
    out.append(check("option: CE detected", is_option_instrument("SENSEX26O0173200CE")))
    out.append(check("option: PE detected", is_option_instrument("NIFTY2692225700PE")))
    out.append(check("option: FUT not an option", not is_option_instrument("SENSEX26OCTFUT")))

    # Tick rounding: SELL side floors, BUY side ceils, epsilon-safe
    out.append(check("tick: floor to 0.05",
                     abs(round_to_tick(30.949, 0.05, up=False) - 30.90) < 1e-9))
    out.append(check("tick: ceil to 0.05",
                     abs(round_to_tick(30.951, 0.05, up=True) - 31.00) < 1e-9))
    out.append(check("tick: binary artifact guarded",
                     abs(round_to_tick(30.95, 0.05, up=False) - 30.95) < 1e-9))

    # Limit margin: exact tier math (tier pct of LTP, clamped to
    # [MIN_PTS, MAX_PTS]) - expected values computed from the same
    # config the function reads, so config overrides stay consistent.
    m_low, m_mid, m_high = (
        compute_limit_margin(5.0), compute_limit_margin(50.0),
        compute_limit_margin(1000.0),
    )
    from core.utils import fetch_from_json as _ffj
    pct = {
        "lt10": float(_ffj("appconfig.json", "LIMIT_MARGIN_PCT_LT_10") or 5.0),
        "mid": float(_ffj("appconfig.json", "LIMIT_MARGIN_PCT_10_100") or 3.0),
        "gt500": float(_ffj("appconfig.json", "LIMIT_MARGIN_PCT_GT_500") or 1.0),
    }
    min_pts = float(_ffj("appconfig.json", "LIMIT_MARGIN_MIN_PTS") or 0.05)
    max_pts = float(_ffj("appconfig.json", "LIMIT_MARGIN_MAX_PTS") or 3.0)

    def _expected(ltp, tier_pct):
        raw = ltp * tier_pct / 100.0
        return max(min_pts, min(max_pts, raw))

    out.append(check(
        "limit margin: tier math exact",
        abs(m_low - _expected(5.0, pct["lt10"])) < 1e-9
        and abs(m_mid - _expected(50.0, pct["mid"])) < 1e-9
        and abs(m_high - _expected(1000.0, pct["gt500"])) < 1e-9,
        f"low={m_low}, mid={m_mid}, high={m_high}"
    ))
    return out


# =================================================================
# CELL: PAPER FILE ENGINE (parse + multi-leg aggregation + buy time)
# =================================================================
def cell_paper_engine():
    out = []
    cfg = INSTRUMENTS["SENSEX"]
    paper_file = reset_state("PAPER", cfg["exchange"], t._broker)

    t.write_paper_trade(transaction_type="BUY", tradingsymbol=cfg["symbol"],
                        token=cfg["token"], qty=2, ltp=100.0, product="MIS",
                        strategy="ULTRA_SCALPING", sell_type="U")
    t.write_paper_trade(transaction_type="BUY", tradingsymbol=cfg["symbol"],
                        token=cfg["token"], qty=1, ltp=99.0, product="MIS",
                        strategy="SCALPING", sell_type="D")
    t.write_paper_trade(transaction_type="SELL", tradingsymbol=cfg["symbol"],
                        token=cfg["token"], qty=2, ltp=105.0, product="MIS",
                        buy_price=100.0, lot_size=cfg["lot"],
                        buy_time=None, strategy="ULTRA_SCALPING", sell_type="U")

    trades = t.fetch_paper_trades()
    out.append(check("paper file: 3 rows parsed",
                     trades is not None and len(trades) == 3,
                     f"got {len(trades) if trades else trades}"))

    t.refresh_paper_positions()
    legs = {str(k): v for k, v in t._position_data.items()}
    ultra_key = next((k for k in legs if "ULTRA_SCALPING" in k), None)
    scalping_key = next((k for k in legs if k.endswith("SCALPING:D")), None)
    out.append(check(
        "paper engine: flat leg removed, open leg kept",
        ultra_key is None and scalping_key is not None
        and int(legs[scalping_key]["net_quantity"]) == 1,
        f"legs={list(legs)}"
    ))

    buy_time = t.get_open_position_buy_time(
        cfg["token"], tradingsymbol=cfg["symbol"],
        strategy="SCALPING", sell_type="D",
    )
    out.append(check("paper engine: buy time resolved for the open leg",
                     bool(buy_time), f"buy_time={buy_time}"))
    return out


# =================================================================
# CELL: KITE FEED SAMPLER (stall/rate math behind the watchdog)
# =================================================================
def cell_feed_sampler():
    out = []
    cfg = INSTRUMENTS["SENSEX"]
    ka = build_kite_adapter(t, cfg["lot"])
    # Feed-sampler state (normally initialized in __init__)
    ka._feed_lock = threading.Lock()
    ka._feed_last_tick_ts = 0.0
    ka._feed_packets = 0
    ka._feed_max_gap = 0.0
    # Window starts 30s ago: inside the 60s stats window so the
    # sampler's end-of-window reset does not wipe the counters
    # between the call and the reads.
    ka._feed_stats_window_start = time.time() - 30

    ka._record_feed_ticks(10)
    with ka._feed_lock:
        packets, last_ts = ka._feed_packets, ka._feed_last_tick_ts
    out.append(check(
        "feed sampler: packets counted + last-tick stamped",
        packets == 10 and last_ts > 0,
        f"packets={packets}",
    ))

    # A batch after a simulated 12s quiet gap measures the gap
    with ka._feed_lock:
        ka._feed_last_tick_ts = time.time() - 12
        ka._feed_max_gap = 0.0
        ka._feed_stats_window_start = time.time() - 30
    ka._record_feed_ticks(1)
    with ka._feed_lock:
        max_gap = ka._feed_max_gap
    out.append(check(
        "feed sampler: quiet gap measured",
        max_gap >= 11.0,
        f"max_gap={max_gap}"
    ))
    return out


# =================================================================
# RUN THE MATRIX
# =================================================================
def main():
    print("=" * 70)
    print("SMOKE TEST MATRIX | 5 modes x 4 instruments")
    print("=" * 70)

    CELLS = [
        ("LIVE-KITE", cell_live_kite),
        ("LIVE-MSTOCK", cell_live_mstock),
        ("PAPER", cell_paper),
        ("SIMULATION", lambda i: cell_engine(SimulatorAdapter, "SIMULATION", i)),
        ("PLAYBACK", lambda i: cell_engine(PlaybackAdapter, "PLAYBACK", i)),
        ("INTERFACE", lambda i: cell_interface_parity()),
        ("DECISION", lambda i: cell_decision()),
        ("SIZING-PNL", lambda i: cell_sizing_pnl()),
        ("UTILS", lambda i: cell_utils()),
        ("PAPER-FILE", lambda i: cell_paper_engine()),
        ("FEED-SAMPLER", lambda i: cell_feed_sampler()),
    ]

    for cell_name, fn in CELLS:
        results[cell_name] = {}
        print(f"\n--- CELL {cell_name} ---")
        for instrument in INSTRUMENTS:
            print(f"  [{instrument}]")
            try:
                results[cell_name][instrument] = fn(instrument)
            except Exception as e:
                results[cell_name][instrument] = [check(f"cell crashed: {e}", False)]

    results["GLOBAL"] = {"(all)": []}
    print(f"\n--- CELL GLOBAL (cross-cutting) ---")
    results["GLOBAL"]["(all)"] = cell_global()

    # =================================================================
    # SUMMARY TABLE
    # =================================================================
    print("\n" + "=" * 70)
    print("RESULTS TABLE (pass/total per cell)")
    print("=" * 70)
    header = f"{'Cell':<12}" + "".join(f"{i:>12}" for i in INSTRUMENTS)
    print(header)
    print("-" * len(header))
    for cell_name, instruments in results.items():
        row = f"{cell_name:<12}"
        for instrument, checks in instruments.items():
            passed = sum(1 for c in checks if c)
            row += f"{'OK' if passed == len(checks) else 'FAIL':>7} {passed}/{len(checks):<4}"
        print(row)

    total = sum(len(v) for instruments in results.values() for v in instruments.values())
    passed = sum(1 for instruments in results.values() for v in instruments.values() if v for c in v if c)
    print("-" * len(header))
    print(f"TOTAL: {passed}/{total}")

    if fails:
        print("\nFAILED SCENARIOS:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("\nALL COMBINATIONS GREEN")


if __name__ == "__main__":
    main()
