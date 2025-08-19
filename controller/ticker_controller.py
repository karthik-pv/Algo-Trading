from core.kite_connector import KiteSingleton
from core.trade_logic import Trader_Singleton

from controller.http_controller import fetch_instruments

KiteInstance = KiteSingleton()
TraderInstance = Trader_Singleton()


# only starting websocket connection and subscribing to events
# NO NEED TO CHANGE


def start_socket_connection():
    socket = KiteInstance.create_kite_socket()

    def on_ticks(ws, ticks):

        print(ticks)

    def on_connect(ws, response):
        print("Connected to Kite WebSocket")
        instruments = fetch_instruments()
        ws.set_mode(ws.MODE_FULL, instruments)

    def on_close(ws, code, reason):
        print("WebSocket closed:", code, reason)

    def on_order_update(ws, data):
        print("Order update received:", data)
        Trader_Singleton.refresh_open_positions_and_buy_price()

    socket.on_ticks = on_ticks
    socket.on_connect = on_connect
    socket.on_close = on_close
    socket.on_order_update = on_order_update

    socket.connect(threaded=True)
