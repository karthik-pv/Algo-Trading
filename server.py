from flask import Flask, request, jsonify
import threading

from core.kite_connector import initialise_kite_for_dev, initialise_kite_for_prod
from controller.http_controller import (
    fetch_all_orders,
    fetch_all_instruments,
    fetch_all_positions,
    fetch_all_trades,
)
from controller.ticker_controller import start_socket_connection, set_shutdown_event
from core.trade_logic import Trader_Singleton

trader = Trader_Singleton()

shutdown_event = threading.Event()
set_shutdown_event(shutdown_event)

app = Flask(__name__)


@app.route("/get_all_orders", methods=["GET"])
def get_all_orders():
    orders = fetch_all_orders()
    if orders is not None:
        return {"orders": orders}, 200
    else:
        return {"error": "Failed to fetch orders"}, 500


@app.route("/get_all_instruments", methods=["GET"])
def get_all_instruments():
    instruments = fetch_all_instruments()
    if instruments is not None:
        return {"instruments": instruments}, 200
    else:
        return {"error": "Failed to fetch instruments"}, 500


@app.route("/get_open_positions", methods=["GET"])
def get_all_open_positions():
    positions = fetch_all_positions()
    if positions is not None:
        return {"positions": positions}, 200
    else:
        return {"error": "Failed to fetch positions"}, 500


@app.route("/get_trades", methods=["GET"])
def get_trades():
    trades = fetch_all_trades()
    if trades is not None:
        return {"trades": trades}, 200
    else:
        return {"error": "Failed to fetch trades"}, 500


if __name__ == "__main__":
    # uncomment for development
    initialise_kite_for_dev()

    # uncomment for production
    # initialise_kite_for_prod()

    trader.start_trading_watcher_thread()
    trader.refresh_open_positions_and_buy_price()
    threading.Thread(target=start_socket_connection, daemon=True).start()
    app.run(debug=True, use_reloader=False)
