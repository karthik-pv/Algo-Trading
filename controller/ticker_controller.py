from core.kite_connector import KiteSingleton
from controller.http_controller import fetch_instruments

KiteInstance = KiteSingleton()


# only starting websocket connection and subscribing to events
# NO NEED TO CHANGE


def start_socket_connection():
    socket = KiteInstance.create_kite_socket()

    def on_ticks(ws, ticks):
        print(ticks)

    def on_connect(ws, response):
        print("Connected to Kite WebSocket")
        instruments = fetch_instruments()
        print(instruments)
        ws.set_mode(ws.MODE_FULL, instruments)

    def on_close(ws, code, reason):
        print("WebSocket closed:", code, reason)

    socket.on_ticks = on_ticks
    socket.on_connect = on_connect
    socket.on_close = on_close

    socket.connect(threaded=True)
