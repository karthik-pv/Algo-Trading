from core.kite_connector import KiteSingleton
from core.trade_logic import Trader_Singleton

from controller.http_controller import fetch_instruments_from_json

KiteInstance = KiteSingleton()
TraderInstance = Trader_Singleton()


# only starting websocket connection and subscribing to events
# NO NEED TO CHANGE

shutdown_event = None


def set_shutdown_event(event):
    global shutdown_event
    shutdown_event = event


def start_socket_connection():
    socket = KiteInstance.get_kite_socket_connection()

    def on_ticks(ws, ticks):
        TraderInstance.set_latest_price(ticks)

    def on_connect(ws, response):
        print("Connected to Kite WebSocket")
        instruments = fetch_instruments_from_json()
        ws.set_mode(ws.MODE_FULL, instruments)

    def on_close(ws, code, reason):
        print("WebSocket closed:", code, reason)

    def on_order_update(ws, data):
        if data["status"] == "COMPLETE":
            unsubscribe_all()
            TraderInstance.refresh_open_positions_and_buy_price()
            subscribe_instruments = TraderInstance.get_relevant_instruments_to_track()
            subscribe_to_relevant_instruments(subscribe_instruments)
        print("Order update received:", data)

    socket.on_ticks = on_ticks
    socket.on_connect = on_connect
    socket.on_close = on_close
    socket.on_order_update = on_order_update

    socket.connect(threaded=True)
    if shutdown_event:
        shutdown_event.wait()
    socket.close()


def subscribe_to_relevant_instruments(instruments):
    try:
        socket = KiteInstance.get_kite_socket_connection()
        if instruments:
            socket.subscribe(instruments)
            print(f"Subscribed to instruments: {instruments}")
        else:
            print("No valid instruments to subscribe to.")
    except Exception as e:
        print("Error subscribing to instruments:", e)


def unsubscribe_all():
    try:
        ws = KiteInstance.get_kite_socket_connection()
        if hasattr(ws, "subscribed_tokens") and ws.subscribed_tokens:
            ws.unsubscribe(ws.subscribed_tokens)
            print("Unsubscribed from all instruments")
        else:
            print("No instruments currently subscribed")
    except Exception as e:
        print("Error unsubscribing:", e)
