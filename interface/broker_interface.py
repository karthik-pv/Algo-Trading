from abc import ABC, abstractmethod


class BrokerInterface(ABC):

    @abstractmethod
    def fetch_all_orders(self):
        pass

    @abstractmethod
    def fetch_all_instruments(self):
        pass

    @abstractmethod
    def fetch_all_positions(self):
        pass

    @abstractmethod
    def fetch_all_trades(self):
        pass

    @abstractmethod
    def sell_units(self, trading_symbol, quantity, exchange):
        pass

    @abstractmethod
    def fetch_instruments_from_json(self):
        pass

    @abstractmethod
    def start_socket_connection(self, shutdown_event, trader_instance):
        pass

    @abstractmethod
    def dev_start():
        pass

    @abstractmethod
    def prod_start():
        pass
