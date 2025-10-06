import datetime
import calendar
from typing import Optional , Dict , List , Any

from utils import fetch_from_json , find_matching_object

def last_tuesday(year: int, month: int) -> datetime.date:
    """
    Finds the last Tuesday of a given month and year.
    """
    last_day = calendar.monthrange(year, month)[1]
    dt = datetime.date(year, month, last_day)
    # Go backwards until we find a Tuesday (weekday() == 1 for Tuesday)
    while dt.weekday() != 1:
        dt -= datetime.timedelta(days=1)
    return dt

def calculate_accurate_average_buy_price_and_update_positions(
    positions: List[Dict[str, Any]], 
    orders: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    positions_map = {}
    for i, p in enumerate(positions):
        symbol = p.get('tradingsymbol')
        quantity = p.get('quantity', 0)
        if symbol and quantity > 0:
            positions_map[symbol] = {
                'remaining_quantity': quantity,
                'total_cost': 0.0,
                'original_index': i
            }

    if not positions_map:
        return positions

    try:
        orders.sort(key=lambda item: datetime.datetime.strptime(item['timestamp'], '%Y-%b-%d %H:%M:%S'), reverse=True)
    except Exception as e:
        print(f"Error sorting orders, returning original positions. Error: {e}")
        return positions

    for order in orders:
        symbol = order.get("tradingsymbol")
        
        if order.get("transaction_type") != "BUY" or symbol not in positions_map:
            continue
            
        order_quantity = int(order.get("quantity", 0))
        avg_price = float(order.get("average_price", 0.0))
        
        if order_quantity <= 0 or avg_price <= 0.0:
            continue
            
        position_data = positions_map[symbol]
        
        consumed_quantity = min(order_quantity, position_data['remaining_quantity'])
        
        if consumed_quantity > 0:
            cost_contribution = consumed_quantity * avg_price
            position_data['total_cost'] += cost_contribution
            position_data['remaining_quantity'] -= consumed_quantity
            
        if not positions_map:
            break

    for symbol, data in positions_map.items():
        original_index = data['original_index']
        total_cost = data['total_cost']
        
        original_quantity = positions[original_index].get('quantity', 0)
        
        if total_cost > 0 and original_quantity > 0:
            quantity_accounted_for = original_quantity - data['remaining_quantity']
            if quantity_accounted_for > 0:
                new_average_price = total_cost / quantity_accounted_for
                positions[original_index]['average_price'] = round(new_average_price, 2)
            
    return positions

def get_current_nifty_future(today: datetime.date = None) -> tuple[int, int]:
    """
    Determines the current trading month for Nifty futures based on the last Tuesday expiry.
    """
    if today is None:
        today = datetime.date.today()

    this_month = today.month
    this_year = today.year
    
    # Use the updated last_tuesday function to find this month's expiry
    expiry_this_month = last_tuesday(this_year, this_month)
    
    if today <= expiry_this_month:
        # If today is on or before this month's expiry, we are in the current month's contract
        return (this_month, this_year)
    else:
        # Otherwise, roll over to the next month's contract
        if this_month == 12:
            return (1, this_year + 1)
        else:
            return (this_month + 1, this_year)

def nifty_future_symbol(root: str = "NIFTY", today: datetime.date = None) -> str:
    """
    Generates the Nifty future symbol (e.g., NIFTYOCT25) for the current trading month.
    """
    month, year = get_current_nifty_future(today)
    mon_name = calendar.month_abbr[month].upper()
    yy = year % 100
    return f"{root}{mon_name}{yy:02d}"

def get_weekly_expiry_date() -> datetime.date:

    today = datetime.date.today()

    # Calculate days until the next Tuesday (weekday() == 1)
    days_until_tuesday = (1 - today.weekday() + 7) % 7
    expiry_candidate = today + datetime.timedelta(days=days_until_tuesday)

    # If it's Tuesday and past market close, roll over to the next week's Tuesday.
    if days_until_tuesday == 0 and datetime.datetime.now().time() > datetime.time(15, 30):
         expiry_candidate += datetime.timedelta(days=7)

    holiday_strings = fetch_from_json("constants.json", "HOLIDAYS")
    if holiday_strings is None:
        holidays = set()
    else:
        holidays = {datetime.datetime.strptime(d_str, "%d-%m-%Y").date() for d_str in holiday_strings}

    while True:
        is_weekend = expiry_candidate.weekday() >= 5
        
        is_holiday = expiry_candidate in holidays

        if not is_weekend and not is_holiday:
            return expiry_candidate
        print(f"Adjusting expiry: {expiry_candidate.strftime('%d-%m-%Y')} is a holiday/weekend. Checking previous day.")
        expiry_candidate -= datetime.timedelta(days=1)


def get_instrument_tokens_from_symbol(name):
    return find_matching_object("instrument_list.json" , "name" , name)["token"]

def get_instrument_details_from_json(name):
    return find_matching_object("instrument_list.json" , "name" , name)
