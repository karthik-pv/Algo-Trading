from flask import Flask

from core.kite_connector import initialise_kite_for_dev, initialise_kite_for_prod
from controller.trade_controller import fetch_all_orders, fetch_all_instruments

app = Flask(__name__)


@app.route("/")
def hello_world():
    return "<p>Hello, World!</p>"


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


if __name__ == "__main__":
    # uncomment for development
    initialise_kite_for_dev()
    # uncomment for production
    # initialise_kite_for_prod()
    app.run(debug=True, use_reloader=False)
