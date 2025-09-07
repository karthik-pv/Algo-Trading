from utils import get_trading_symbols_from_json
from core.kite_connector import KiteSingleton

KiteInstance = KiteSingleton()
kite = KiteInstance.get_kite()

supported_exchanges = ["MCX", "NIFTY"]
# supported_exchanges = [kite.EXCHANGE_NSE,kite.EXCHANGE_BSE,kite.EXCHANGE_NFO]
# ,kite.EXCHANGE_MCX


def fetch_all_orders():
    try:
        orders = kite.orders()
        return orders
    except Exception as e:
        print(f"Error fetching orders: {e}")
        return None


def fetch_all_instruments():
    try:
        instruments = kite.instruments()
        return instruments
    except Exception as e:
        print(f"Error fetching instruments: {e}")
        return None


def fetch_instruments_from_json():
    try:
        instruments = kite.instruments()
        trading_symbols = get_trading_symbols_from_json()
        relevant_instruments_data = [
            instrument
            for instrument in instruments
            if instrument["tradingsymbol"] in trading_symbols
            and instrument["exchange"] in supported_exchanges
        ]
        print(relevant_instruments_data)
        relevant_instruments = [
            instrument["instrument_token"]
            for instrument in instruments
            if instrument["tradingsymbol"] in trading_symbols
            and instrument["exchange"] in supported_exchanges
        ]

        if len(relevant_instruments) < len(trading_symbols):
            print(
                "Few trading symbols were invalid and did not map to valid trading instruments."
            )
        print(relevant_instruments)
        return relevant_instruments if relevant_instruments else None

    except Exception as e:
        print(f"Error fetching instrument: {e}")
        return None


def fetch_all_positions():
    try:
        positions = kite.positions()
        net_open_positions = [
            position for position in positions["net"] if position["quantity"] > 0
        ]
        return net_open_positions
    except Exception as e:
        print(f"Error fetching positions: {e}")
        return None


def fetch_all_trades():
    try:
        trades = kite.trades()
        return trades
    except Exception as e:
        print(f"Error fetching trades: {e}")
        return None


def sell_units(trading_symbol, quantity, exchange):
    try:
        order_id = kite.place_order(
            tradingsymbol=trading_symbol,
            exchange=exchange,
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=quantity,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_MIS,
            variety=kite.VARIETY_REGULAR,
        )
        print(f"Sell order placed successfully. Order ID: {order_id}")
        return order_id
    except Exception as e:
        print(f"Error placing sell order: {e}")
        return None


# To get the average buy price of Buy Order which are not sold yet and thus dont use the average buy price from Position which considers even sold Buy Order to calculate the average
def fetch_open_buy_trades():
    """
    Returns the list of BUY trades (with price & qty) that are not yet sold.
    Uses FIFO matching between BUY and SELL trades.
    """
    trades = kite.trades()  # all executed trades
    open_buys = []

    # Organize trades FIFO by order_timestamp
    trades_sorted = sorted(trades, key=lambda x: x["order_timestamp"])

    fifo_buys = []  # will hold unmatched buy trades

    for trade in trades_sorted:
        qty = trade["quantity"]
        price = trade["average_price"]
        side = trade["transaction_type"]

        if side == "BUY":
            fifo_buys.append(
                {"qty": qty, "price": price, "symbol": trade["tradingsymbol"]}
            )

        elif side == "SELL":
            # Match sells against FIFO buys
            remaining = qty
            while remaining > 0 and fifo_buys:
                buy_trade = fifo_buys[0]
                if buy_trade["qty"] > remaining:
                    buy_trade["qty"] -= remaining
                    remaining = 0
                else:
                    remaining -= buy_trade["qty"]
                    fifo_buys.pop(0)  # fully matched buy removed

    # Whatever is left in fifo_buys = open buy positions
    open_buys = fifo_buys
    return open_buys
