from controller.http_controller import fetch_all_positions

STOP_LOSS = 5
BOOK_PROFIT = 5


class Trader_Singleton:
    _instance = None
    _open_positions_and_buy_price = {}
    _latest_price_for_instrument_token = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Trader_Singleton, cls).__new__(cls)
        return cls._instance

    def get_instance(self):
        if self._instance is None:
            self._instance = self()
        return self._instance

    def refresh_open_positions_and_buy_price(self):
        self._open_positions_and_buy_price.clear()
        positions = fetch_all_positions()
        for position in positions:
            self._open_positions_and_buy_price[position["instrument_token"]] = position[
                "average_price"
            ]
        print(self._open_positions_and_buy_price)

    def stop_loss_book_profit_core(self):
        for instrument in self._open_positions_and_buy_price.keys:
            difference = (
                self._latest_price_for_instrument_token[instrument]
                - self._open_positions_and_buy_price[instrument]
            )
            if difference < 0 and difference >= STOP_LOSS:
                # sell logic
                continue
            elif difference > 0 and difference >= BOOK_PROFIT:
                # sell logic
                continue
            else:
                continue
