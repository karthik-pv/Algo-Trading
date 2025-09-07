# server.py

import threading
import logging
from flask import Flask, jsonify, request

from core.trade_logic import Trader_Singleton
from interface.broker_interface import BrokerInterface
from adapter.kite_adapter import KiteAdapter


app = Flask(__name__)

BROKER_MAP = {
    "kite": KiteAdapter,
}

CURRENT_BROKER = "kite"
broker: BrokerInterface = BROKER_MAP[CURRENT_BROKER]()


shutdown_event = threading.Event()


@app.route("/orders")
def get_orders():
    orders = broker.fetch_all_orders()
    return jsonify(orders if orders else {"error": "Could not fetch orders"})


@app.route("/instruments")
def get_instruments():
    instruments = broker.fetch_all_instruments()
    return jsonify(
        instruments if instruments else {"error": "Could not fetch instruments"}
    )


@app.route("/positions")
def get_positions():
    positions = broker.fetch_all_positions()
    print(positions)
    return jsonify(positions if positions else {"error": "Could not fetch positions"})


@app.route("/trades")
def get_trades():
    trades = broker.fetch_all_trades()
    return jsonify(trades if trades else {"error": "Could not fetch trades"})


@app.route("/sell", methods=["POST"])
def sell():
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
    instruments = broker.fetch_instruments_from_json()
    return jsonify(
        instruments
        if instruments
        else {"error": "Could not fetch relevant instruments"}
    )


def start_socket():
    try:
        logging.info("Starting broker WebSocket connection...")
        broker.start_socket_connection(shutdown_event, trader_instance=None)
    except Exception as e:
        logging.error(f"Socket error: {e}")


if __name__ == "__main__":
    try:
        trader = Trader_Singleton()
        trader.set_broker(broker)

        # uncomment for development
        broker.dev_start()
        # uncomment for prod
        # broker.prod_start()

        socket_thread = threading.Thread(target=start_socket, daemon=True)
        socket_thread.start()

        app.run(host="0.0.0.0", port=5000, debug=True)
    except KeyboardInterrupt:
        shutdown_event.set()
        logging.info("Shutting down server...")
