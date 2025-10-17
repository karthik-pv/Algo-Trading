import json
import threading
import logging
import asyncio
from flask import Flask, jsonify, request, Response, render_template
from flask_cors import CORS

from core.trade_logic import Trader_Singleton
from interface.broker_interface import BrokerInterface
from adapter.kite_adapter import KiteAdapter
from adapter.mstock_adapter import MStockAdapter
from core.trading_view_handler import trading_view_handle_func

from utils import is_market_open , fetch_from_json

from loguru import logger



logger.remove()

# File logging
logger.add(
    "app.log",
    rotation="10 MB",
    retention="7 days",
    level="DEBUG",
    format=(
        "{time:YYYY-MM-DD HH:mm:ss:SSS} | "
        "{level:<8} | "
        "{file:<25} | "
        "{function:<25} | "
        "{message} | "
        
    )
)

# Console logging
logger.add(
    sink=lambda msg: print(msg, end=""),
    colorize=True,
    format= "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - |"
            "<level>{message}</level> |"
           
)

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

BROKER_MAP = {"kite": KiteAdapter, "mstock": MStockAdapter}

CURRENT_BROKER = "kite"

EXCHANGE = fetch_from_json("constants.json" , "EXCHANGE")

broker: BrokerInterface = BROKER_MAP[CURRENT_BROKER]()

shutdown_event = threading.Event()


@app.route("/orders")
def get_orders():
    logger.info("Fetching all orders")
    orders = broker.fetch_all_orders()
    logger.log("DATA", f"Orders: {orders}")
    return jsonify(orders if orders else {"error": "Could not fetch orders"})


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

@app.route("/instrument_quote")
def get_instrument_quote():
    logger.info("Fetching instrument quote for NFO 52168")
    quotes = broker.fetch_instrument_quote("NFO" , "52168")
    return jsonify(quotes)


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
    res = trading_view_handle_func(data)
    return jsonify(res)


def run_flask_app():
    logger.info("Starting Flask app...")
    app.run(debug=True, use_reloader=False)

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
    logger.info("Rendering buy dashboard page")
    return render_template("buy_dashboard.html")


async def start_async_connections():
    logger.info("Starting async connections...")
    logger.debug(f"Broker type: {type(broker)}")
    if isinstance(broker, MStockAdapter):   
        trader.setup_weekly_option_contract_subscriptions()
        if is_market_open():
            flask_thread = threading.Thread(target=run_flask_app, daemon=True)
            flask_thread.start()
            await broker.start_socket_connection(shutdown_event, trader_instance=trader)
            trader.refresh_subscriptions()
        else:
            run_flask_app()
    else:
        logger.info("M.Stock not selected, skipping async socket start.")

def start_socket():
    logger.info("Starting socket connection...")
    try:
        logger.info("Starting broker WebSocket connection...")
        broker.start_socket_connection(shutdown_event, trader_instance=trader)
        trader.setup_weekly_option_contract_subscriptions()
        trader.refresh_subscriptions()
    except Exception as e:
        logger.error(f"Socket error: {e}")


if __name__ == "__main__":
    try:
        logger.info("Initializing broker and trader...")
        trader = Trader_Singleton() 
        # uncomment for development
        logger.info(f"Using broker: {CURRENT_BROKER}")
        broker.dev_start()
        logger.info("Starting in development mode...")
        # uncomment for prod
        #broker.prod_start()
        #logger.info("Starting in production mode...")
        #broker.fetch_all_instruments()
        broker.download_instrument_list(EXCHANGE)
        trader.set_broker(broker)
        trader.on_start()
        trader.start_frontend_socket_server(app)
        

        if CURRENT_BROKER == "kite":
            socket_thread = threading.Thread(target=start_socket, daemon=True)
            socket_thread.start()
            # if is_market_open():
            trader.start_trading_watcher_thread()
            app.run(debug=True , use_reloader = False)
        elif CURRENT_BROKER == "mstock":
            if is_market_open():
                trader.start_trading_watcher_thread()
            asyncio.run(start_async_connections())

        

    except KeyboardInterrupt:
        shutdown_event.set()
        logger.info("Shutting down server...")
