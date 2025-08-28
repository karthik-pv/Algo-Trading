import time
import random
import threading

from controller.http_controller import fetch_all_positions, sell_units

STOP_LOSS = 5
BOOK_PROFIT = 5


class Trader_Singleton:
    _instance = None
    _open_positions_and_buy_price = {}
    _latest_price_for_tradingsymbol = {}
    _quantities_of_tradingsymbol = {}
    _trading_watcher_thread_running = False
    _stop_event = threading.Event()
    _tick_counter = 0

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
            self._open_positions_and_buy_price[position["tradingsymbol"]] = position[
                "average_price"
            ]
        print(self._open_positions_and_buy_price)

    def get_relevant_instruments_to_track(self):
        positions = fetch_all_positions()
        relevant_instruments = [position["instrument"] for position in positions]
        return relevant_instruments

    def stop_loss_book_profit_core(self):
        print("Started trading watcher thead")
        while not self._stop_event.is_set():
            for tradingsymbol in self._latest_price_for_tradingsymbol.keys():
                difference = (
                    self._latest_price_for_tradingsymbol[tradingsymbol]
                    - self._open_positions_and_buy_price[tradingsymbol]
                )
                if difference < 0 and abs(difference) >= STOP_LOSS:
                    print(
                        "------------------------------------------------------------------------------"
                    )
                    print("difference = " + difference)
                    print("SELLING " + tradingsymbol + " TO STOP LOSS")
                    print(
                        "------------------------------------------------------------------------------"
                    )
                    sell_units(
                        tradingsymbol,
                        self._quantities_of_tradingsymbol[tradingsymbol],
                        "MCX",
                    )
                    continue
                elif difference > 0 and difference >= BOOK_PROFIT:
                    print(
                        "------------------------------------------------------------------------------"
                    )
                    print("difference = " + difference)
                    print("SELLING " + tradingsymbol + " TO BOOK PROFIT")
                    print(
                        "------------------------------------------------------------------------------"
                    )
                    sell_units(
                        tradingsymbol,
                        self._quantities_of_tradingsymbol[tradingsymbol],
                        "MCX",
                    )
                    continue

                self._stop_event.wait(timeout=0.01)

    def set_latest_price(self, ticks):
        for tick in ticks:
            self._latest_price_for_tradingsymbol[tick["tradingsymbol"]] = tick["ohlc"][
                "close"
            ]
        print(self._latest_price_for_tradingsymbol)

    def start_trading_watcher_thread(self):
        if not self._trading_watcher_thread_running:
            self._trading_watcher_thread_running = True
            self._stop_event.clear()
            thread = threading.Thread(
                target=self.stop_loss_book_profit_core, daemon=True
            )
            print("Launching trading watcher thread 🚀")
            thread.start()

    def stop_trading_watcher_thread(self):
        if self._trading_watcher_thread_running:
            print("Stopping trading watcher thread ⏹")
            self._stop_event.set()
            self._trading_watcher_thread_running = False
