from controller.http_controller import fetch_all_positions


class Trader_Singleton:
    _instance = None
    _open_positions_and_buy_price = None

    def set_open_positions_and_buy_price():
        positions = fetch_all_positions()
