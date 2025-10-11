import datetime
import json
from typing import List, Dict, Any

# 1. DATA: Provided orders and positions data
# =================================================

orders_data = [
    {
        "average_price": "23.75", "quantity": "150", "timestamp": "2025-Oct-08 15:12:58",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "13.8", "quantity": "75", "timestamp": "2025-Oct-08 15:12:58",
        "tradingsymbol": "NIFTY-14Oct2025-25400-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "26.15", "quantity": "75", "timestamp": "2025-Oct-08 15:11:27",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "BUY"
    },
    {
        "average_price": "27.35", "quantity": "75", "timestamp": "2025-Oct-08 15:10:03",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "BUY"
    },
    {
        "average_price": "43.7", "quantity": "75", "timestamp": "2025-Oct-08 14:51:26",
        "tradingsymbol": "NIFTY-14Oct2025-24900-PE", "transaction_type": "SELL"
    },
    {
        "average_price": "41.7", "quantity": "75", "timestamp": "2025-Oct-08 14:49:49",
        "tradingsymbol": "NIFTY-14Oct2025-24900-PE", "transaction_type": "BUY"
    },
    {
        "average_price": "40.9", "quantity": "75", "timestamp": "2025-Oct-08 14:48:20",
        "tradingsymbol": "NIFTY-14Oct2025-24900-PE", "transaction_type": "SELL"
    },
    {
        "average_price": "42.3", "quantity": "75", "timestamp": "2025-Oct-08 14:47:57",
        "tradingsymbol": "NIFTY-14Oct2025-24900-PE", "transaction_type": "BUY"
    },
    {
        "average_price": "41.55", "quantity": "75", "timestamp": "2025-Oct-08 14:34:17",
        "tradingsymbol": "NIFTY-14Oct2025-24900-PE", "transaction_type": "SELL"
    },
    {
        "average_price": "40.15", "quantity": "75", "timestamp": "2025-Oct-08 14:33:46",
        "tradingsymbol": "NIFTY-14Oct2025-24900-PE", "transaction_type": "BUY"
    },
    {
        "average_price": "30.1", "quantity": "75", "timestamp": "2025-Oct-08 14:20:32",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "32.5", "quantity": "75", "timestamp": "2025-Oct-08 14:17:40",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "BUY"
    },
    {
        "average_price": "0", "quantity": "75", "timestamp": "2025-Oct-08 14:16:14",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "BUY"
    },
    {
        "average_price": "38.4", "quantity": "75", "timestamp": "2025-Oct-08 14:08:28",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "38.8", "quantity": "75", "timestamp": "2025-Oct-08 14:07:58",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "BUY"
    },
    {
        "average_price": "24.3", "quantity": "75", "timestamp": "2025-Oct-08 14:05:35",
        "tradingsymbol": "NIFTY-14Oct2025-25400-CE", "transaction_type": "BUY"
    },
    {
        "average_price": "46.2", "quantity": "75", "timestamp": "2025-Oct-08 13:49:41",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "225", "timestamp": "2025-Oct-08 13:42:12",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:42:11",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:42:11",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:42:11",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:39:08",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "225", "timestamp": "2025-Oct-08 13:39:08",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:39:08",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:39:08",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:37:14",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "225", "timestamp": "2025-Oct-08 13:37:14",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:37:14",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:37:14",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:34:16",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:34:16",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:34:16",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "225", "timestamp": "2025-Oct-08 13:34:16",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:33:49",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "225", "timestamp": "2025-Oct-08 13:33:49",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:33:49",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "1800", "timestamp": "2025-Oct-08 13:33:49",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "42.95", "quantity": "75", "timestamp": "2025-Oct-08 13:14:12",
        "tradingsymbol": "NIFTY-14Oct2025-25300-CE", "transaction_type": "BUY"
    },
    {
        "average_price": "38.25", "quantity": "75", "timestamp": "2025-Oct-08 13:07:20",
        "tradingsymbol": "NIFTY-14Oct2025-24900-PE", "transaction_type": "SELL"
    },
    {
        "average_price": "38.65", "quantity": "75", "timestamp": "2025-Oct-08 13:06:55",
        "tradingsymbol": "NIFTY-14Oct2025-24900-PE", "transaction_type": "BUY"
    },
    {
        "average_price": "67.1", "quantity": "150", "timestamp": "2025-Oct-08 13:02:31",
        "tradingsymbol": "NIFTY-14Oct2025-25200-CE", "transaction_type": "SELL"
    },
    {
        "average_price": "0", "quantity": "150", "timestamp": "2025-Oct-08 13:00:24",
        "tradingsymbol": "NIFTY-14Oct2025-25200-CE", "transaction_type": "BUY"
    },
    {
        "average_price": "0", "quantity": "150", "timestamp": "2025-Oct-08 13:00:18",
        "tradingsymbol": "NIFTY-14Oct2025-25200-CE", "transaction_type": "BUY"
    },
    {
        "average_price": "69.8", "quantity": "75", "timestamp": "2025-Oct-08 13:00:12",
        "tradingsymbol": "NIFTY-14Oct2025-25200-CE", "transaction_type": "BUY"
    },
    {
        "average_price": "46.9", "quantity": "75", "timestamp": "2025-Oct-08 12:24:38",
        "tradingsymbol": "NIFTY-14Oct2025-25200-CE", "transaction_type": "BUY"
    }
]

positions_data = [
    {
        "average_price": 39.3,
        "exchange": "NSE",
        "instrument": "42703",
        "instrument_token": "42703",
        "lotsize": "75",
        "quantity": 75,
        "tradingsymbol": "NIFTY-14Oct2025-25400-CE"
    }
]

# 2. FUNCTION: The provided calculation logic
# ===============================================

def calculate_accurate_average_buy_price_and_update_positions(
    positions: List[Dict[str, Any]], 
    orders: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Calculates the accurate average buy price for open positions based on the last buy orders.
    """
    positions_map = {}
    for i, p in enumerate(positions):
        symbol = p.get('tradingsymbol')
        # Ensure quantity is an integer
        quantity = int(p.get('quantity', 0))
        if symbol and quantity > 0:
            positions_map[symbol] = {
                'remaining_quantity': quantity,
                'total_cost': 0.0,
                'original_index': i
            }

    # If there are no open positions to calculate, return the original list
    if not positions_map:
        return positions

    # Sort orders from most recent to oldest
    try:
        # The provided timestamp format is '%Y-%b-%d %H:%M:%S'
        orders.sort(key=lambda item: datetime.datetime.strptime(item['timestamp'], '%Y-%b-%d %H:%M:%S'), reverse=True)
    except Exception as e:
        print(f"Error sorting orders, returning original positions. Error: {e}")
        return positions

    # Iterate through sorted orders to find the last BUYs that make up the current position
    for order in orders:
        symbol = order.get("tradingsymbol")
        
        if order.get("transaction_type") != "BUY" or symbol not in positions_map:
            continue
            
        order_quantity = int(order.get("quantity", 0))
        avg_price = float(order.get("average_price", 0.0))
        
        # Ignore orders with no valid quantity or price (e.g., rejected orders)
        if order_quantity <= 0 or avg_price <= 0.0:
            continue
            
        position_data = positions_map[symbol]
        
        # Only process if this position still needs quantity to be accounted for
        if position_data['remaining_quantity'] <= 0:
            continue
        
        # Determine how much of this order contributes to the current open position
        consumed_quantity = min(order_quantity, position_data['remaining_quantity'])
        
        if consumed_quantity > 0:
            cost_contribution = consumed_quantity * avg_price
            position_data['total_cost'] += cost_contribution
            position_data['remaining_quantity'] -= consumed_quantity

    # Update the original positions list with the newly calculated average price
    for symbol, data in positions_map.items():
        original_index = data['original_index']
        total_cost = data['total_cost']
        
        original_quantity = int(positions[original_index].get('quantity', 0))
        
        # Calculate the quantity we found buy orders for
        quantity_accounted_for = original_quantity - data['remaining_quantity']

        if total_cost > 0 and quantity_accounted_for > 0:
            new_average_price = total_cost / quantity_accounted_for
            positions[original_index]['average_price'] = round(new_average_price, 2)
            
    return positions


# 3. EXECUTION: Run the function and print the results
# =======================================================

if __name__ == "__main__":
    print("--- Original Positions ---")
    print(json.dumps(positions_data, indent=2))
    print("\n" + "="*30 + "\n")

    # Run the calculation
    updated_positions = calculate_accurate_average_buy_price_and_update_positions(
        positions_data, 
        orders_data
    )

    print("--- Updated Positions ---")
    print(json.dumps(updated_positions, indent=2))