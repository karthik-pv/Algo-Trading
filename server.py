import json
import threading
import logging
import asyncio
from flask import Flask, jsonify, request, Response, render_template

from core.trade_logic import Trader_Singleton
from interface.broker_interface import BrokerInterface
from adapter.kite_adapter import KiteAdapter
from adapter.mstock_adapter import MStockAdapter


app = Flask(__name__)

BROKER_MAP = {"kite": KiteAdapter, "mstock": MStockAdapter}

CURRENT_BROKER = "mstock"

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


@app.route("/refresh_ticker_subscriptions")
def refresh_ticker_subscriptions():
    trader.refresh_subscriptions()
    return {"msg": "success"}


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


def run_flask_app():
    app.run(debug=True, use_reloader=False)


def start_socket():
    try:
        logging.info("Starting broker WebSocket connection...")
        broker.start_socket_connection(shutdown_event, trader_instance=trader)
    except Exception as e:
        logging.error(f"Socket error: {e}")


@app.route("/alert")
def alert_page():
    return render_template("alert.html")


@app.route("/stream")
def stream():
    def event_stream():
        logging.info("Client connected to SSE stream.")
        while True:
            message = trader.alert_queue.get()
            json_data = json.dumps(message)
            yield f"data: {json_data}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


async def start_async_connections():
    """Main async function to run all async brokers."""
    if isinstance(broker, MStockAdapter):
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        # run_flask_app()
        await broker.start_socket_connection(shutdown_event, trader_instance=trader)
    else:
        logging.info("M.Stock not selected, skipping async socket start.")


if __name__ == "__main__":
    try:
        trader = Trader_Singleton()

        # uncomment for development
        broker.dev_start()
        # uncomment for prod
        # broker.prod_start()

        trader.set_broker(broker)
        trader.on_start()

        if CURRENT_BROKER == "kite":
            socket_thread = threading.Thread(target=start_socket, daemon=True)
            socket_thread.start()
            trader.start_trading_watcher_thread()
            app.run(debug=True)
        elif CURRENT_BROKER == "mstock":

            trader.start_trading_watcher_thread()
            asyncio.run(start_async_connections())

    except KeyboardInterrupt:
        shutdown_event.set()
        logging.info("Shutting down server...")
