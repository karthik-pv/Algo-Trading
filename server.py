import json
import threading
import logging
import asyncio
import os
import pandas as pd

from flask import Flask, jsonify, request, Response, render_template
from flask_cors import CORS
from datetime import datetime
from datetime import timedelta


from core.trade_logic import Trader_Singleton
from interface.broker_interface import BrokerInterface
from adapter.kite_adapter import KiteAdapter
from adapter.mstock_adapter import MStockAdapter
from core.trading_view_handler import trading_view_handle_func

from utils import is_market_open , fetch_from_json,backup_old_logs

from adapter.mstock_utils import save_orders_to_csv 

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
    format=(
        "{time:YYYY-MM-DD HH:mm:ss:SSS} | "
        "{level:<8} | "
        "{file:<25} | "
        "{function:<28} | "
        "{message} | "
    ),
)


#Console logging
logger.add(
    sink=lambda msg: print(msg, end=""),
    colorize=True,
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

BROKER_MAP = {"KITE": KiteAdapter, "MSTOCK": MStockAdapter}

CURRENT_BROKER = fetch_from_json("constants.json" , "BROKER")
UNDERLYING = fetch_from_json("constants.json" , "UNDERLYING")

logger.debug(f"Current Broker is {CURRENT_BROKER} and underlying is {UNDERLYING}")

if UNDERLYING == "NIFTY":
    EXCHANGE = "NFO"
elif UNDERLYING == "SENSEX":
    EXCHANGE = "BFO"
elif UNDERLYING == "CRUDEOIL" :
    EXCHANGE = "MCX"
elif UNDERLYING == "CRUDEOILM" :
    EXCHANGE = "MCX"

broker: BrokerInterface = BROKER_MAP[CURRENT_BROKER]()

trader = Trader_Singleton()

shutdown_event = threading.Event()

SETTINGS_FILE = "settings.json"
CONSTANTS_FILE = "constants.json"

# Default settings (used if file missing)
DEFAULT_SETTINGS = {
    "mode": "ALERT",
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
    """Save settings back to settings.json."""
    try:
        data = request.get_json()
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=4)
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

        settings["MODE"] = new_mode  # ✅ ONLY UPDATES MODE

        with open("settings.json", "w") as f:
            json.dump(settings, f, indent=4)

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
        with open(CONSTANTS_FILE, "w") as f:
            json.dump(data, f, indent=4)
        return jsonify({"message": "Saved successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    

@app.route("/orders")
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
    - calls save_orders_to_csv()
    """
    try:
        payload = request.get_json(silent=True)

        if not payload or "trade_date" not in payload:
            return jsonify({"success": False, "error": "Missing trade_date"}), 400

        trade_date = payload["trade_date"]

        # 1️⃣ Fetch all orders
        all_orders = broker.fetch_all_orders_for_export()

        if not all_orders:
            return jsonify({"success": False, "error": "No orders found"}), 404

        df = pd.DataFrame(all_orders)

        if "timestamp" not in df.columns:
            return jsonify({"success": False, "error": "timestamp missing"}), 500

        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")

        target_date = pd.to_datetime(trade_date).date()

        # 2️⃣ Filter date
        df = df[df["timestamp"].dt.date == target_date]

        # 3️⃣ Keep executed trades
        df = df[df["order_status"].astype(str).str.lower() == "complete"]

        if df.empty:
            return jsonify({
                "success": False,
                "error": f"No executed trades on {trade_date}"
            })

        filtered_orders = df.to_dict(orient="records")

        filename = f"kite_executed_{trade_date}.csv"

        save_orders_to_csv(filtered_orders, filename)

        return jsonify({
            "success": True,
            "message": f"{len(filtered_orders)} trades saved to {filename}"
        })

    except Exception as e:
        logger.exception("Error importing Kite trades")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/import_executed_trades", methods=["POST"])
def import_executed_trades_route():
    """
    Flask endpoint:
    - receives trade_date from Utilities.html
    - loads all orders via MStockAdapter.fetch_all_orders()
    - filters orders by date
    - calls adapter.save_orders_to_csv()
    """
    try:
        payload = request.get_json(silent=True)
        logger.info(f"Import executed trades payload: {payload}")
        if not payload or "trade_date" not in payload:
            return jsonify({
                "success": False,
                "error": "Missing trade_date"
            }), 400

        trade_date = payload["trade_date"]

        # 1️⃣ Fetch all orders
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

        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")

        # 2️⃣ Filter by selected date
        target_date = pd.to_datetime(trade_date).date()
        df = df[df["timestamp"].dt.date == target_date]

        # 3️⃣ Keep only executed/traded orders
        df = df[df["status"].astype(str).str.lower() == "COMPLETE"]
        #df = df[df["order_status"].astype(str).str.lower() == "traded"]

        if df.empty:
            return jsonify({
                "success": False,
                "error": f"No executed trades on {trade_date}"
            })

        filtered_orders = df.to_dict(orient="records")

        logger.info(f"Filtered executed orders count: {filtered_orders}")

        # 3️⃣ Save to CSV using adapter's routine
        filename = f"mstock_executed_{trade_date}.csv"
        save_orders_to_csv(filtered_orders, filename)

        return jsonify({
            "success": True,
            "message": f"{len(filtered_orders)} trades saved to {filename}"
        })

    except Exception as e:
        logger.error(f"Error importing executed trades: {e}")
        # import traceback
        # traceback.print_exc()
        # return jsonify({"success": False, "error": str(e)}), 500


@app.route("/import_executed_trades_kite", methods=["POST"])
def import_executed_trades_kite():
    """
    Flask endpoint for Kite Connect:
    - receives trade_date
    - loads all orders via kite.orders()
    - filters executed trades by date
    - saves to CSV
    """
    try:
        payload = request.get_json(silent=True)

        if not payload or "trade_date" not in payload:
            return jsonify({
                "success": False,
                "error": "Missing trade_date"
            }), 400

        trade_date = payload["trade_date"]

        # 1️⃣ Fetch all orders from Kite
        all_orders = kite.orders()

        if not all_orders:
            return jsonify({
                "success": False,
                "error": "No orders found"
            }), 404

        df = pd.DataFrame(all_orders)

        if "order_timestamp" not in df.columns:
            return jsonify({
                "success": False,
                "error": "order_timestamp missing"
            }), 500

        # Convert timestamp
        df["order_timestamp"] = pd.to_datetime(
            df["order_timestamp"], errors="coerce"
        )

        # 2️⃣ Filter by date
        target_date = pd.to_datetime(trade_date).date()

        df = df[df["order_timestamp"].dt.date == target_date]

        # 3️⃣ Keep only executed trades
        df = df[df["status"].str.upper() == "COMPLETE"]

        if df.empty:
            return jsonify({
                "success": False,
                "error": f"No executed trades on {trade_date}"
            })

        filtered_orders = df.to_dict(orient="records")

        # 4️⃣ Save CSV
        filename = f"kite_executed_{trade_date}.csv"
        save_orders_to_csv(filtered_orders, filename)

        return jsonify({
            "success": True,
            "message": f"{len(filtered_orders)} trades saved to {filename}"
        })

    except Exception as e:
        logger.error(f"Error importing executed trades: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


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
    fund_summary = broker.fetch_fund_summary()
    logger.log("DATA", f"Fund Summary: {fund_summary}")
    return jsonify({"data" : fund_summary})

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

@app.route("/alert")
def alert_page():
    logger.info("Rendering alert page")
    return render_template("alert.html")

@app.route("/home")
def home_page():
    logger.info("Rendering home page")
    return render_template("home.html")

@app.route("/widget_one")
def widget_one():
    logger.info("Rendering widget one page")
    return render_template("widget_one.html")

@app.route("/buy_dashboard")
def dashboard():
    logger.info("BUY DASHBOARD: Request received")
    page = render_template("buy_dashboard.html")
    logger.info("BUY DASHBOARD: Template rendered")
    return page

@app.route("/settings")
def settings():
    logger.info("Rendering settings page")
    return render_template("settings.html")

@app.route("/Utilities")
def Utilities():
    logger.info("Rendering Utilities page")
    return render_template("Utilities.html")


@app.route("/convert_contract_note", methods=["POST"])
def convert_contract_note():
    pdf_file = request.files.get("pdf")
    if not pdf_file:
        return {"error": "No PDF uploaded"}, 400

    # Save PDF temporarily
    tmp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    pdf_file.save(tmp_pdf.name)

    try:
        csv_path = broker.convert_mstock_contract_note(tmp_pdf.name)
    except Exception as e:
        os.unlink(tmp_pdf.name)
        return {"error": str(e)}, 500

    os.unlink(tmp_pdf.name)

    return send_file(
        csv_path,
        as_attachment=True,
        download_name="paired_trades.csv",
        mimetype="text/csv"
    )


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

        

    except KeyboardInterrupt:
        shutdown_event.set()
        logger.info("Shutting down server...")



    