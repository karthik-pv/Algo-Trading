from core.kite_connector import KiteSingleton

KiteInstance = KiteSingleton()
kite = KiteInstance.get_kite()


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


def fetch_all_positions():
    try:
        positions = kite.positions()
        net_positions = positions["net"]
        print(net_positions)
        return net_positions
    except Exception as e:
        print(f"Error fetching positions: {e}")
        return None
