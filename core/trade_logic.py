import threading
import logging
import queue
from interface.broker_interface import BrokerInterface

STOP_LOSS = 0.5
BOOK_PROFIT = 0.5


class Trader_Singleton:
    _instance = None
    _broker = BrokerInterface or None
    _open_positions_and_buy_price = {}
    _latest_price_for_tradingsymbol = {}
    _quantities_of_tradingsymbol = {}
    _instrument_token_to_trading_symbol_mapping = {}
    _trading_watcher_thread_running = False
    _stop_event = threading.Event()
    _tick_counter = 0

    alert_queue = queue.Queue()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Trader_Singleton, cls).__new__(cls)
        return cls._instance

    def set_broker(self, broker):
        self._broker = broker

    def refresh_open_positions_and_buy_price(self):
        self._open_positions_and_buy_price.clear()
        positions = self._broker.fetch_all_positions()
        logging.info(f"Positions {positions}")
        for position in positions:
            self._open_positions_and_buy_price[position["tradingsymbol"]] = position[
                "average_price"
            ]
            self._instrument_token_to_trading_symbol_mapping[
                position["instrument_token"]
            ] = position["tradingsymbol"]

        logging.debug(self._open_positions_and_buy_price)

    def get_relevant_instruments_to_track(self):
        positions = self._broker.fetch_all_positions()
        return [position["instrument"] for position in positions]

    def update_trading_symbol_and_quantity(self):
        positions = self._broker.fetch_all_positions()
        for position in positions:
            self._quantities_of_tradingsymbol[position["tradingsymbol"]] = position[
                "quantity"
            ]

    def refresh_subscriptions(self):
        positions = self.get_relevant_instruments_to_track()
        self.refresh_open_positions_and_buy_price()
        print(self._open_positions_and_buy_price)
        self.update_trading_symbol_and_quantity()
        self._broker.refresh_subscriptions(positions)

    def on_start(self):
        self.refresh_open_positions_and_buy_price()
        self.update_trading_symbol_and_quantity()

    def stop_loss_book_profit_core(self, broker: BrokerInterface):
        logging.info("Started trading watcher thread")
        while not self._stop_event.is_set():
            print("Watcher thread is running")
            for tradingsymbol in list(self._latest_price_for_tradingsymbol.keys()):
                qty = self._quantities_of_tradingsymbol.get(tradingsymbol)
                buy_price = self._open_positions_and_buy_price.get(tradingsymbol)
                ltp = self._latest_price_for_tradingsymbol.get(tradingsymbol)

                if not (qty and buy_price and ltp):
                    continue

                difference = ltp - buy_price
                print(difference)
                if difference < 0 and abs(difference) >= STOP_LOSS:
                    logging.warning(
                        f"STOP LOSS triggered for {tradingsymbol}, diff={difference}"
                    )
                    self.alert_queue.put(
                        {
                            "msg": f"STOP LOSS TRIGGERED FOR {tradingsymbol} DIFF = {difference}",
                            "color": "red",
                        }
                    )
                    broker.sell_units(tradingsymbol, qty, "MCX")
                    continue

                elif difference > 0 and difference >= BOOK_PROFIT:
                    logging.info(
                        f"BOOK PROFIT triggered for {tradingsymbol}, diff={difference}"
                    )
                    self.alert_queue.put(
                        {
                            "msg": f"BOOK PROFIT TRIGGERED FOR {tradingsymbol} DIFF = {difference}",
                            "color": "green",
                        }
                    )
                    broker.sell_units(tradingsymbol, qty, "MCX")
                    continue

            self._stop_event.wait(timeout=5)

    def set_latest_price(self, instrument_token, tradingsymbol, price):
        if not tradingsymbol:
            tradingsymbol = self._instrument_token_to_trading_symbol_mapping.get(
                instrument_token
            )
        self._latest_price_for_tradingsymbol[tradingsymbol] = price

    def start_trading_watcher_thread(self):
        if not self._trading_watcher_thread_running:
            self._trading_watcher_thread_running = True
            self._stop_event.clear()
            thread = threading.Thread(
                target=self.stop_loss_book_profit_core,
                args=(self._broker,),
                daemon=True,
            )
            logging.info("Launching trading watcher thread 🚀")
            thread.start()

    def stop_trading_watcher_thread(self):
        if self._trading_watcher_thread_running:
            logging.info("Stopping trading watcher thread ⏹")
            self._stop_event.set()
            self._trading_watcher_thread_running = False
