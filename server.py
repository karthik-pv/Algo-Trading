import json
import threading
import logging
import asyncio
import os
import pandas as pd

from flask import Flask, jsonify, request, Response, render_template, redirect
from flask_cors import CORS
from datetime import datetime
from datetime import timedelta


from core.trade_logic import Trader_Singleton
from interface.broker_interface import BrokerInterface
from adapter.kite_adapter import KiteAdapter
from adapter.mstock_adapter import MStockAdapter
from adapter.simulator_adapter import SimulatorAdapter
from adapter.playback_adapter import PlaybackAdapter
from core.trading_view_handler import trading_view_handle_func

from utils import is_market_open, fetch_from_json, load_json_with_retry, backup_old_logs, resolve_day_start_cash, check_internet_connectivity, clear_json_cache, UNDERLYING_TO_EXCHANGE

from adapter.mstock_utils import save_orders_to_xlsx, build_orders_export,write_orders_workbook



from loguru import logger

import tempfile
from flask import request, send_file



logging.getLogger("urllib3").setLevel(logging.WARNING) # Avoids any degug messages from urlib3
logging.getLogger('werkzeug').setLevel(logging.ERROR)

logging.getLogger('websockets').setLevel(logging.WARNING)
logging.getLogger('kiteconnect').setLevel(logging.WARNING)

logger.remove()

# File logging
# logger.add(
#     "logs/app.log",
#     rotation="10 MB",
#     retention="7 days",
#     level="DEBUG",
#     format=(
#         "{time:YYYY-MM-DD HH:mm:ss:SSS} | "
#         "{level:<8} | "
#         "{file:<25} | "
#         "{function:<25} | "
#         "{message} | "
        
#     )
# )

#Every new session creates a new log file

# Create folder name in mm_dd format
folder_name = datetime.now().strftime("%m_%d")
log_dir = os.path.join("logs", folder_name)
os.makedirs(log_dir, exist_ok=True)

session_time = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = os.path.join(log_dir, f"app_{session_time}.log")

logger.add(
    log_file,
    rotation="10 MB",
    retention="7 days",
    level="DEBUG",
    enqueue=True,
    format=(
        "{time:YYYY-MM-DD HH:mm:ss:SSS} | "
        "{level:<8} | "
        "{file:<25} | "
        "{function:<28} | "
        "{message} | "
    ),
)


#Console logging
# enqueue=True moves the (very slow on Windows) console print and file
# writes to a background thread so logging never blocks trading logic.
logger.add(
    sink=lambda msg: print(msg, end=""),
    colorize=True,
    enqueue=True,
    format= "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - |"
            "<level>{message}</level> |")




           


# # Add a new handler that prints only the message for errors
# logger.add(lambda msg: print(msg, end=""), level="ERROR", format="{message}")

logger.info("=" * 80)
logger.info("Starting new session")
logger.info("=" * 80)

# Define a custom log level for data logs
logger.level("DATA", no=25, color="<yellow>", icon="📊")

# # Define custom format with colors
# format_str = (
#     "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
#     "<level>{level: <8}</level> | "
#     "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
#     "<level>{message}</level>"
# )

# # Add sink (stdout) with colorization enabled
# logger.add(lambda msg: print(msg, end=""), colorize=True, format=format_str)


logger.info("Logger initialized successfully")


app = Flask(__name__)
CORS(app)

BROKER_MAP = {"KITE": KiteAdapter, "MSTOCK": MStockAdapter, "SIMULATOR": SimulatorAdapter, "PLAYBACK": PlaybackAdapter}

try:
    CURRENT_BROKER = fetch_from_json("constants.json" , "BROKER")
    UNDERLYING = fetch_from_json("constants.json" , "UNDERLYING")
    CURRENT_MODE = str(fetch_from_json("settings.json", "MODE") or "ALERT").upper()
    VALID_MODES = {"LIVE", "PAPER", "SIMULATION", "PLAYBACK"}

    if CURRENT_MODE not in VALID_MODES:
        raise RuntimeError(
            f"Invalid MODE '{CURRENT_MODE}'. Allowed values are: "
            f"{', '.join(sorted(VALID_MODES))}"
        )

    if CURRENT_MODE in ("LIVE", "PAPER"):
        logger.info(f"Mode is {CURRENT_MODE}. Verifying internet connectivity before startup.")
        if not check_internet_connectivity():
            raise RuntimeError(
                "NO INTERNET CONNECTIVITY detected. "
                f"Startup aborted - {CURRENT_MODE} mode requires an active internet connection. "
                "Please check your network and restart the application."
            )
        logger.info("Internet connectivity verified. Proceeding with startup.")

    SIMULATION_CONFIG = {}
    PLAYBACK_CONFIG = {}

    if CURRENT_MODE == "SIMULATION":
        simulation_json_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "simulation.json"
        )
        SIMULATION_CONFIG = load_json_with_retry(simulation_json_path)

    if CURRENT_MODE == "SIMULATION" and SIMULATION_CONFIG.get("REQUIRE_MARKET_CLOSED", True) and is_market_open():
        raise RuntimeError("Simulation mode is configured for non-market hours, but the market is open")

    if CURRENT_MODE == "SIMULATION":
        CURRENT_BROKER = "SIMULATOR"

    if CURRENT_MODE == "PLAYBACK":
        playback_json_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "Playback.json"
        )
        PLAYBACK_CONFIG = load_json_with_retry(playback_json_path)

        if not str(PLAYBACK_CONFIG.get("INSTRUMENT") or "").strip():
            raise RuntimeError("Playback.json must define INSTRUMENT (option tradingsymbol)")

        playback_price_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            str(PLAYBACK_CONFIG.get("PLAYBACK_PRICE_FILE", "PlaybackPrice.csv")),
        )
        if not os.path.exists(playback_price_file):
            raise RuntimeError(f"Playback price file not found: {playback_price_file}")

        CURRENT_BROKER = "PLAYBACK"
except Exception:
    logger.exception("Startup configuration loading failed. Aborting session.")
    raise

logger.debug(f"Current Broker is {CURRENT_BROKER} and underlying is {UNDERLYING}")

# Shared map (utils.py) — the exchange is fully determined by the
# underlying; the old instrument-derived EXCHANGE constant is gone.

if UNDERLYING not in UNDERLYING_TO_EXCHANGE:
    raise RuntimeError(
        f"Unsupported UNDERLYING '{UNDERLYING}'. Allowed values: "
        f"{', '.join(sorted(UNDERLYING_TO_EXCHANGE))}"
    )

EXCHANGE = UNDERLYING_TO_EXCHANGE[UNDERLYING]

broker: BrokerInterface = BROKER_MAP[CURRENT_BROKER]()

trader = Trader_Singleton()

shutdown_event = threading.Event()

SETTINGS_FILE = "settings.json"
CONSTANTS_FILE = "constants.json"

# Default settings (used if file missing)
DEFAULT_SETTINGS = {
    "mode": "PAPER",
    "pctMargin": 50,
    "volReduction": 10,
    "defaultLot": 1,
    "tolerancePts": 5,
    "optionPct": False,
    "optionPts": False,
    "optionEma": True,
    "pctProfit": 1.0,
    "pctLoss": 0.5,
    "ptsProfit": 20,
    "ptsLoss": 10,
    "emaPeriod": 9
}

@app.route("/settings")
def settings_page():
    """Render the settings HTML page."""
    return render_template("settings.html")




@app.route("/load_settings")
def load_settings():
    """Load settings.json and return as JSON."""
    if not os.path.exists(SETTINGS_FILE):
        # Create file with defaults if missing
        with open(SETTINGS_FILE, "w") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=4)
        return jsonify(DEFAULT_SETTINGS)

    try:
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)
        return jsonify(settings)
    except Exception as e:
        print("Error reading settings:", e)
        return jsonify(DEFAULT_SETTINGS), 500

@app.route("/save_settings", methods=["POST"])
def save_settings():
    """Save settings.json and return as JSON."""
    try:
        data = request.get_json()
        # Merge into the existing settings instead of replacing the file,
        # so keys that are not part of the settings form (STOP_LOSS_*,
        # ENTRY_CHECK_*, POSITION_PNL_LOG_FREQ, cash balances, etc.)
        # survive a save from the UI.
        try:
            with open(SETTINGS_FILE, "r") as f:
                settings = json.load(f)
        except Exception:
            settings = {}
        if isinstance(data, dict):
            settings.update(data)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=4)
        # settings.json is cached by fetch_from_json(); clear it so
        # runtime re-reads (e.g. MODE in the order handlers) see the
        # new values instead of the stale import-time cache.
        clear_json_cache(SETTINGS_FILE)
        return jsonify({"status": "success"})
    except Exception as e:
        print("Error saving settings:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/update_sell_mode", methods=["POST"])
def update_sell_mode():
    try:
        data = request.json
        new_mode = data.get("mode")

        with open("settings.json", "r") as f:
            settings = json.load(f)

        # Store under a dedicated SELL_MODE key. Writing to "MODE" would
        # clobber the trading mode (PAPER/LIVE/SIMULATION/PLAYBACK) that
        # startup and the order handlers rely on.
        settings["SELL_MODE"] = new_mode

        with open("settings.json", "w") as f:
            json.dump(settings, f, indent=4)

        # Keep the fetch_from_json cache in sync so the new MODE is
        # picked up by the next order instead of a restart.
        clear_json_cache("settings.json")

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/constants")
def constants():
    return render_template("constants.html")


@app.route("/load", methods=["GET"])
def load_constants():
    if not os.path.exists(CONSTANTS_FILE):
        return jsonify({"error": "constants.json not found"}), 404
    with open(CONSTANTS_FILE, "r") as f:
        data = json.load(f)
    return jsonify(data)

@app.route("/save", methods=["POST"])
def save_constants():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400
    try:
        # Merge the posted form values into the existing constants so
        # any key not currently shown on the UI page survives a save
        # instead of being wiped by a full-file replacement.
        existing = {}
        if os.path.exists(CONSTANTS_FILE):
            try:
                with open(CONSTANTS_FILE, "r") as f:
                    existing = json.load(f) or {}
            except (OSError, json.JSONDecodeError):
                existing = {}
        merged = {**existing, **data}
        with open(CONSTANTS_FILE, "w") as f:
            json.dump(merged, f, indent=4)
        # constants.json is cached by fetch_from_json(); clear it so
        # runtime re-reads (e.g. WOC factors in setup_woc_) see the
        # new values without an app restart.
        clear_json_cache(CONSTANTS_FILE)
        return jsonify({"message": "Saved successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    

@app.route("/api/orders")
def get_orders():
    logger.info("Fetching all orders")
    orders = broker.fetch_all_orders()
    logger.log("DATA", f"Orders: {orders}")
    return jsonify(orders if orders else {"error": "Could not fetch orders"})


@app.route("/import_executed_trades_kite", methods=["POST"])
def import_executed_trades_kite_route():
    """
    Flask endpoint:
    - receives trade_date
    - loads all orders via KiteAdapter.fetch_all_orders()
    - filters by date
    - returns the shared formatted XLSX workbook
    """
    try:
        payload = request.get_json(silent=True)

        if not payload or "trade_date" not in payload:
            return jsonify({"success": False, "error": "Missing trade_date"}), 400

        trade_date = payload["trade_date"]

        # Kite's order/trade book is day-only; past dates cannot be
        # served from the API (Console reports are the only source).
        try:
            kite_requested_date = pd.to_datetime(trade_date, errors="coerce").date()
        except Exception:
            kite_requested_date = None
        if (
            kite_requested_date is not None
            and not pd.isna(kite_requested_date)
            and kite_requested_date < datetime.now().date()
        ):
            return jsonify({
                "success": False,
                "error": (
                    "KITE provides same-day trade data only; past dates "
                    "are not available via the API"
                )
            })

        # 1️⃣ Fetch all orders
        all_orders = broker.fetch_all_orders_for_export()

        if not all_orders:
            return jsonify({"success": False, "error": "No orders found"}), 404

        df = pd.DataFrame(all_orders)

        if "timestamp" not in df.columns:
            return jsonify({"success": False, "error": "timestamp missing"}), 500

        df["timestamp"] = pd.to_datetime(
            df["timestamp"],
            errors="coerce",
            dayfirst=True,
            format="mixed"
        )

        if "order_status" not in df.columns:
            return jsonify({
                "success": False,
                "error": "order_status missing"
            }), 500

        logger.info(
            "Order import diagnostics: "
            f"rows={len(df)}, "
            f"unparsed_timestamps={int(df['timestamp'].isna().sum())}, "
            f"dates={sorted(df['timestamp'].dropna().dt.date.astype(str).unique())}, "
            f"statuses={sorted(df['order_status'].astype(str).str.strip().str.upper().unique())}"
        )

        target_date = pd.to_datetime(trade_date).date()

        # 2️⃣ Filter date
        df = df[df["timestamp"].dt.date == target_date]

        # 3️⃣ Keep executed trades
        df = df[
            df["order_status"].astype(str).str.strip().str.upper().isin(
                {"COMPLETE", "TRADED"}
            )
        ]

        if df.empty:
            return jsonify({
                "success": False,
                "error": f"No executed trades on {trade_date}"
            })

        filtered_orders = df.to_dict(orient="records")

        workbook_path = save_orders_to_xlsx(filtered_orders)
        return send_file(
            workbook_path,
            as_attachment=True,
            download_name=f"Kite_Orders_{trade_date.replace('-', '_')}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    except Exception as e:
        logger.exception("Error importing Kite trades")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/import_executed_trades", methods=["POST"])
def import_executed_trades_route():
    """
    Flask endpoint:
    - receives trade_date from orders.html
    - loads all orders via MStockAdapter.fetch_all_orders()
    - filters orders by date
    - calls adapter.save_orders_to_csv()
    """
    try:
        paper_mode = str(fetch_from_json("settings.json", "MODE") or "").upper() == "PAPER"

        if not paper_mode and CURRENT_BROKER == "KITE":
            return import_executed_trades_kite_route()

        payload = request.get_json(silent=True)
        logger.info(f"Import executed trades payload: {payload}")
        if not payload or "trade_date" not in payload:
            return jsonify({
                "success": False,
                "error": "Missing trade_date"
            }), 400

        trade_date = payload["trade_date"]

        # Past dates are not present in the daily order book. M.Stock
        # exposes a dated trade-history endpoint; other brokers provide
        # same-day data only.
        try:
            mstock_requested_date = pd.to_datetime(trade_date, errors="coerce").date()
        except Exception:
            mstock_requested_date = None
        mstock_is_past_date = bool(
            mstock_requested_date is not None
            and not pd.isna(mstock_requested_date)
            and mstock_requested_date < datetime.now().date()
        )

        if paper_mode:
            # Paper fills never reach the broker; the Excel export reads
            # them from the paper trade log instead.
            all_orders = trader.fetch_paper_orders()
        elif mstock_is_past_date and hasattr(broker, "fetch_trades_for_date"):
            all_orders = broker.fetch_trades_for_date(trade_date)
        elif mstock_is_past_date:
            return jsonify({
                "success": False,
                "error": (
                    f"{CURRENT_BROKER} provides same-day trade data only; "
                    f"past dates are not available via the API"
                )
            })
        else:
            all_orders = broker.fetch_all_orders()
        logger.info(f"Total orders fetched: {len(all_orders)}")


        if not all_orders:
            return jsonify({
                "success": False,
                "error": "No orders found"
            }), 404

        df = pd.DataFrame(all_orders)

        # timestamp must exist
        if "timestamp" not in df.columns:
            return jsonify({"success": False, "error": "timestamp missing"}), 500

        # M.Stock commonly returns day-first timestamps such as
        # 04-09-2026 15:20:00, while the browser sends an ISO date.
        df["timestamp"] = pd.to_datetime(
            df["timestamp"],
            errors="coerce",
            dayfirst=True,
            format="mixed"
        )

        if "order_status" not in df.columns:
            return jsonify({
                "success": False,
                "error": "order_status missing"
            }), 500

        logger.info(
            "M.Stock order import diagnostics: "
            f"rows={len(df)}, "
            f"unparsed_timestamps={int(df['timestamp'].isna().sum())}, "
            f"dates={sorted(df['timestamp'].dropna().dt.date.astype(str).unique())}, "
            f"statuses={sorted(df['order_status'].astype(str).str.strip().str.upper().unique())}"
        )
        df_all_dates = df["timestamp"].dropna().dt.date.astype(str)
        df_all_statuses = df["order_status"].astype(str)

        # 2️⃣ Filter by selected date
        target_date = pd.to_datetime(trade_date).date()
        df = df[df["timestamp"].dt.date == target_date]

        # 3️⃣ Keep only executed/traded orders. M.Stock normalizes this field
        # as order_status and can report either COMPLETE or TRADED.
        df = df[
            df["order_status"].astype(str).str.strip().str.upper().isin(
                {"COMPLETE", "TRADED"}
            )
        ]

        if df.empty:
            available_dates = sorted(df_all_dates.unique())
            available_statuses = sorted(
                df_all_statuses.str.strip().str.upper().unique()
            )
            detail = (
                "PaperTrading.txt has no rows for this date."
                if paper_mode else
                f"M.Stock returned dates={available_dates}, "
                f"statuses={available_statuses}."
            )
            return jsonify({
                "success": False,
                "error": (
                    f"No executed trades on {trade_date}. "
                    f"{detail}"
                )
            })

        filtered_orders = df.to_dict(orient="records")

        logger.info(f"Filtered executed orders count: {len(filtered_orders)}")

        workbook_path = save_orders_to_xlsx(filtered_orders)
        return send_file(
            workbook_path,
            as_attachment=True,
            download_name=f"{'Paper' if paper_mode else 'MStock'}_Orders_{trade_date.replace('-', '_')}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    except Exception as e:
        logger.error(f"Error importing executed trades: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/instruments")
def get_instruments():
    logger.info("Fetching all instruments")
    instruments = broker.fetch_all_instruments()
    logger.log("DATA", f"Instruments: {instruments}")
    return jsonify(
        instruments if instruments else {"error": "Could not fetch instruments"}
    )


@app.route("/positions")
def get_positions():
    positions = broker.fetch_all_positions()
    logger.log("DATA", f"Positions: {positions}")
    return jsonify(positions if positions else {"error": "Could not fetch positions"})


@app.route("/refresh_ticker_subscriptions")
def refresh_ticker_subscriptions():
    logger.info("Refreshing ticker subscriptions")
    trader.refresh_subscriptions()
    return {"msg": "success"}


@app.route("/trades")
def get_trades():
    logger.info("Fetching all trades")
    trades = broker.fetch_all_trades()
    return jsonify(trades if trades else {"error": "Could not fetch trades"})

@app.route("/fund_summary")
def get_fund_summary():
    logger.info("Fetching fund summary")
    current_mode = str(fetch_from_json("settings.json", "MODE") or "").upper()
    if current_mode == "PAPER":
        fund_summary = trader.get_paper_fund_summary()
        logger.log("DATA", f"Paper Fund Summary: {fund_summary}")
        return jsonify({"data": fund_summary})
    fund_summary = broker.fetch_fund_summary()
    logger.log("DATA", f"Fund Summary: {fund_summary}")
    return jsonify({"data" : fund_summary})

@app.route("/day_cash")
def get_day_cash():
    logger.info("Fetching day-start cash")
    current_mode = str(fetch_from_json("settings.json", "MODE") or "").upper()
    if current_mode == "PAPER":
        paper_summary = trader.get_paper_fund_summary()
        return jsonify({
            "success": True,
            "cash_balance": paper_summary.get("initial_cash", 0),
            "source": "PAPER"
        })
    try:
        cash_value, source = resolve_day_start_cash(broker)
    except Exception as e:
        logger.exception("Error resolving day-start cash")
        return jsonify({"success": False, "error": str(e)}), 500

    if cash_value is None:
        return jsonify({"success": False, "error": "Day-start cash unavailable"})

    return jsonify({"success": True, "cash_balance": cash_value, "source": source})

# @app.route("/instrument_quote")
# def get_instrument_quote():
#     logger.info("Fetching instrument quote for NFO 52168")
#     quotes = broker.fetch_instrument_quote("NFO" , "52168")
#     return jsonify(quotes)

@app.route("/get_expiries")
def getExpiries():
    logger.info("Fetching expiries")
    expiries = broker.fetch_expiries("NIFTY")
    return jsonify(expiries)


@app.route("/sell", methods=["POST"])
def sell():
    logger.info("Received sell request")
    data = request.get_json(force=True)  # parse JSON body
    symbol = data.get("symbol")
    qty = data.get("qty")
    exchange = data.get("exchange")

    if not symbol or not qty or not exchange:
        return jsonify({"error": "Missing required fields: symbol, qty, exchange"}), 400

    order_id = broker.sell_units(symbol, qty, exchange)
    if order_id:
        return jsonify({"message": "Sell order placed", "order_id": order_id})
    return jsonify({"error": "Failed to place sell order"}), 400


@app.route("/manual-refresh-positions", methods=["POST"])
def manual_refresh_positions():
    logger.info("Received manual refresh positions request")

    try:
        # Call the adapter method you showed
        broker.manual_refresh_positions()

        return jsonify({"message": "Positions refreshed successfully"}), 200

    except Exception as e:
        logger.exception("Error occurred during manual position refresh")
        return jsonify({"error": str(e)}), 500



@app.route("/instruments/json")
def instruments_json():
    logger.info("Fetching instruments from JSON")
    instruments = broker.fetch_instruments_from_json()
    return jsonify(
        instruments
        if instruments
        else {"error": "Could not fetch relevant instruments"}
    )

@app.route("/trading_view_webhook" , methods = ["POST"])
def trading_view():
    logger.info("Received TradingView webhook")
    data = request.json
    res = trading_view_handle_func(data, trader)
    return jsonify(res)

@app.route("/kite_callback")
def kite_login_callback():
    request_token = request.args["request_token"]
    


def run_flask_app():
    logger.info("FLASK THREAD: Starting Flask app...")

    app.run(
        debug=True,
        use_reloader=False,
        threaded=True
    )

# Default landing page: the consolidated Trade page.
@app.route("/")
def index():
    logger.info("INDEX: Rendering trade page (default)")
    return render_template("trade.html")

@app.route("/trade")
def trade_page():
    logger.info("TRADE PAGE: Request received")
    page = render_template("trade.html")
    logger.info("TRADE PAGE: Template rendered")
    return page

# The Home page was removed; its old URL now lands on the default page.
@app.route("/home")
def home_page():
    return redirect("/")

# Legacy URL: the buy dashboard and sell widget were consolidated into
# trade.html; keep the old address working.
@app.route("/buy_dashboard")
def dashboard():
    return redirect("/trade")

# Orders page (executed-trades grid + utilities). The raw broker-orders
# JSON API moved to /api/orders so this page can own the /orders URL.
@app.route("/orders")
def orders_page():
    logger.info("Rendering Orders page")
    return render_template("orders.html")

# Capitalized /Orders kept as a redirect for URLs used before the page
# route was lowercased.
@app.route("/Orders")
def orders_page_legacy():
    return redirect("/orders")

# Legacy Utilities URLs: the utilities functions moved to the Orders
# page, so redirect there.
@app.route("/Utilities")
@app.route("/utilities")
@app.route("/utilities.html")
def Utilities():
    return redirect("/orders")


@app.route("/convert_contract_note", methods=["POST"])
def convert_contract_note():
    pdf_file = request.files.get("pdf")
    if not pdf_file:
        return {"error": "No PDF uploaded"}, 400

    # Create a closed temporary path so Camelot can open it on Windows.
    tmp_fd, tmp_pdf_path = tempfile.mkstemp(suffix=".pdf")
    os.close(tmp_fd)

    try:
        pdf_file.save(tmp_pdf_path)
        # Contract-note conversion is an mStock utility even in simulation mode,
        # where the active trading broker is SimulatorAdapter.
        converter = broker if hasattr(broker, "convert_mstock_contract_note") else MStockAdapter()
        csv_path = converter.convert_mstock_contract_note(tmp_pdf_path)
    except Exception as e:
        logger.exception("Contract-note conversion failed")
        return {"error": str(e)}, 500
    finally:
        try:
            os.unlink(tmp_pdf_path)
        except FileNotFoundError:
            pass

    return send_file(
        csv_path,
        as_attachment=True,
        download_name=(
            f"MStock_Orders_{pd.to_datetime(
                pd.read_excel(csv_path, usecols=["ORDERDATE"], nrows=1)["ORDERDATE"].iloc[0]
            ).strftime('%m_%d_%Y')}.xlsx"
        ),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.route("/convert_contract_note_rows", methods=["POST"])
def convert_contract_note_rows():
    """
    Convert an uploaded M.Stock contract-note PDF into the same
    formatted rows used by the Orders grid and the Excel workbook,
    returning them as JSON (plus the PineScript and trade date)
    instead of writing a file.
    """
    pdf_file = request.files.get("pdf")
    if not pdf_file:
        return jsonify({"success": False, "error": "No PDF uploaded"}), 400

    # Create a closed temporary path so Camelot can open it on Windows.
    tmp_fd, tmp_pdf_path = tempfile.mkstemp(suffix=".pdf")
    os.close(tmp_fd)

    try:
        pdf_file.save(tmp_pdf_path)
        # Contract-note conversion is an mStock utility even in simulation
        # mode, where the active trading broker is SimulatorAdapter.
        converter = (
            broker
            if hasattr(broker, "convert_mstock_contract_note_data")
            else MStockAdapter()
        )
        rows, pine_script, trade_date = converter.convert_mstock_contract_note_data(
            tmp_pdf_path
        )
    except Exception as e:
        logger.exception("Contract-note conversion failed")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp_pdf_path)
        except FileNotFoundError:
            pass

    return jsonify({
        "success": True,
        "rows": rows,
        "pine_script": pine_script,
        "trade_date": trade_date.isoformat() if trade_date else None,
    })

@app.route("/get_executed_trades", methods=["POST"])
def get_executed_trades():
    """
    Fetch executed orders for the selected trade date and return
    the same formatted rows used by the Excel Orders sheet.

    This endpoint is for the HTML grid.
    The existing /import_executed_trades endpoint remains unchanged
    and continues to generate the Excel workbook.
    """
    try:
        payload = request.get_json(silent=True) or {}
        trade_date = payload.get("trade_date")
        current_mode = str(fetch_from_json("settings.json", "MODE") or CURRENT_MODE or "").upper()

        # In playback mode the orders carry playback-clock timestamps and
        # the price file can span more than one replayed day, so the
        # requested (browser) date is ignored: every session order is
        # returned and the date filter below is skipped.
        if current_mode != "PLAYBACK" and not trade_date:
            return jsonify({
                "success": False,
                "error": "Trade date is required"
            }), 400

        logger.info(
            f"get_executed_trades: fetching orders for trade date {trade_date}"
        )

        # Past dates are not present in the daily order book. M.Stock
        # exposes a dated trade-history endpoint for this; other brokers
        # (e.g. Kite) provide same-day data only.
        requested_date = None
        if current_mode != "PLAYBACK" and trade_date:
            try:
                requested_date = pd.to_datetime(trade_date, errors="coerce").date()
            except Exception:
                requested_date = None
        is_past_date = bool(
            requested_date is not None and not pd.isna(requested_date)
            and requested_date < datetime.now().date()
        )

        if current_mode == "PAPER":
            # Paper fills never reach the broker; the Orders grid reads
            # them from the paper trade log instead.
            orders = trader.fetch_paper_orders()
        elif current_mode == "PLAYBACK":
            orders = broker.fetch_all_orders()
        elif is_past_date and hasattr(broker, "fetch_trades_for_date"):
            orders = broker.fetch_trades_for_date(trade_date)
        elif is_past_date:
            return jsonify({
                "success": False,
                "error": (
                    f"{CURRENT_BROKER} provides same-day trade data only; "
                    f"past dates are not available via the API"
                )
            })
        else:
            orders = broker.fetch_all_orders()

        if not orders:
            return jsonify({
                "success": True,
                "rows": []
            })

        df = pd.DataFrame(orders)

        # Make sure the normalized status column exists.
        if "order_status" in df.columns and "status" not in df.columns:
            df["status"] = df["order_status"]

        # Make sure timestamp exists.
        if "timestamp" not in df.columns:
            return jsonify({
                "success": False,
                "error": "Order data does not contain timestamp"
            }), 500

        # Parse timestamps exactly as the existing import route does.
        df["timestamp"] = pd.to_datetime(
            df["timestamp"],
            errors="coerce",
            dayfirst=True,
            format="mixed"
        )

        if current_mode == "PLAYBACK":
            # Playback orders exist only in this session's memory; return
            # all of them regardless of which replayed day they occurred on.
            logger.info(
                "get_executed_trades: playback mode - returning all session "
                "orders without a date filter"
            )
            df = df[df["timestamp"].notna()].copy()
        else:
            # Convert requested date to date object.
            selected_date = pd.to_datetime(
                trade_date,
                errors="coerce"
            ).date()

            if pd.isna(selected_date):
                return jsonify({
                    "success": False,
                    "error": f"Invalid trade date: {trade_date}"
                }), 400

            # Keep only orders from the selected date.
            df = df[
                df["timestamp"].notna()
                & (df["timestamp"].dt.date == selected_date)
            ].copy()

        # Keep only executed/traded orders.
        if "order_status" in df.columns:
            df = df[
                df["order_status"]
                .astype(str)
                .str.upper()
                .isin(["COMPLETE", "TRADED"])
            ].copy()
        elif "status" in df.columns:
            df = df[
                df["status"]
                .astype(str)
                .str.upper()
                .isin(["COMPLETE", "TRADED"])
            ].copy()

        if df.empty:
            logger.info(
                f"get_executed_trades: no executed trades for {trade_date}"
            )

            return jsonify({
                "success": True,
                "rows": []
            })

        filtered_orders = df.to_dict(orient="records")

        # IMPORTANT:
        # Use the exact same formatter that creates the Excel Orders rows.
        rows, _pine_script = build_orders_export(filtered_orders)

        logger.info(
            f"get_executed_trades: returning {len(rows)} formatted rows"
        )

        return jsonify({
            "success": True,
            "rows": rows,
            "pine_script": _pine_script
        })

    except Exception as e:
        logger.exception("Error getting executed trades")

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/export_executed_trades", methods=["POST"])
def export_executed_trades():
    try:
        data = request.get_json() or {}

        rows = data.get("rows", [])
        pine_script = data.get("pine_script", "")

        if not rows:
            return jsonify({
                "success": False,
                "error": "No executed trades to export"
            }), 400

        output_path = write_orders_workbook(
            rows,
            pine_script
        )

        # Optional client-supplied download name (without extension).
        # Sanitized to a safe basename; default keeps legacy behavior.
        requested_filename = str(data.get("filename") or "").strip()
        safe_filename = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in requested_filename
        )
        download_name = (
            f"{safe_filename}.xlsx" if safe_filename else "Executed_Trades.xlsx"
        )

        return send_file(
            output_path,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        logger.exception("export_executed_trades failed")

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500



async def start_async_connections():
    logger.info("Starting async connections...")
    logger.debug(f"Broker type: {type(broker)}")
    if isinstance(broker, MStockAdapter):   
        logger.info("DEBUG: About to start M.Stock WebSocket connection...")
        #trader.setup_woc_subscriptions()
        #if is_market_open():
        # flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        # flask_thread.start()
        logger.info("DEBUG: Calling broker.start_socket_connection()...")
        await broker.start_socket_connection(shutdown_event, trader_instance=trader)
        trader.refresh_subscriptions()
        # else:
        #     run_flask_app()
    else:
        logger.info("M.Stock not selected, skipping async socket start.")

def start_socket():
    logger.info("Starting socket connection...")
    try:
        logger.info("Starting broker WebSocket connection...")
        broker.start_socket_connection(shutdown_event, trader_instance=trader)
        #trader.setup_woc_subscriptions()
        trader.refresh_subscriptions()
    except Exception as e:
        logger.error(f"Socket error: {e}")


if 1==1: #__name__ == "__main__":
#if __name__ == "__main__":
    try:

        backup_old_logs()    

        #app.run(host='0.0.0.0', port=5000, debug=True)
        logger.info("Initializing broker and trader...")
        trader = Trader_Singleton() 
        # uncomment for development
        logger.info(f"Using broker: {CURRENT_BROKER}")
        # broker.dev_start()
        #logger.info("Starting in development mode...")
        # uncomment for prod
        broker.prod_start()
        #logger.info("Starting in production mode...")
        #broker.fetch_all_instruments()
        broker.download_instrument_list(EXCHANGE,UNDERLYING)
        trader.set_broker(broker)

        # Warm the broker's instrument cache (e.g. Kite CSV) in the
        # background while the rest of startup proceeds, so the first
        # get_instrument_details call no longer pays the lazy-load cost.
        preload_thread = threading.Thread(
            target=broker.preload_instrument_data,
            daemon=True,
            name="instrument-preload"
        )
        preload_thread.start()
        logger.info("Instrument cache preload launched in background.")

        try:
            day_cash_value, day_cash_source = resolve_day_start_cash(broker)
            logger.info(f"Day-start cash resolved: {day_cash_value} (source: {day_cash_source})")
        except Exception as e:
            logger.warning(f"Could not resolve day-start cash at startup: {e}")
        
        trader.start_frontend_socket_server(app)
        
        if CURRENT_BROKER == "MSTOCK":
            logger.info("FLASK: Launching Flask thread...")
            flask_thread = threading.Thread(target=run_flask_app, daemon=True)
            flask_thread.start()
            logger.info("FLASK: Flask thread launched.")

        trader.on_start()
        

        if CURRENT_BROKER == "KITE":
            socket_thread = threading.Thread(target=start_socket, daemon=True)
            socket_thread.start()
            if is_market_open():
                trader.start_trading_watcher_thread()
            app.run(debug=True , use_reloader = False)
        elif CURRENT_BROKER == "MSTOCK":
            if is_market_open():
                trader.start_trading_watcher_thread()
            asyncio.run(start_async_connections())
        elif CURRENT_BROKER == "SIMULATOR":
            trader.start_trading_watcher_thread()
            socket_thread = threading.Thread(target=start_socket, daemon=True)
            socket_thread.start()
            app.run(debug=True, use_reloader=False)
        elif CURRENT_BROKER == "PLAYBACK":
            trader.start_trading_watcher_thread()
            socket_thread = threading.Thread(target=start_socket, daemon=True)
            socket_thread.start()
            app.run(debug=True, use_reloader=False)

        

    except KeyboardInterrupt:
        shutdown_event.set()
        logger.info("Shutting down server...")



    