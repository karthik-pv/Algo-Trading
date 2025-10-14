import datetime
from datetime import date
import calendar
from typing import Optional , Dict , List , Any
from calendar import monthrange
import holidays

india_holidays = holidays.India()


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
        orders.sort(key=lambda item: item['timestamp'] if isinstance(item['timestamp'], datetime.datetime) else datetime.datetime.strptime(item['timestamp'], '%Y-%b-%d %H:%M:%S'), reverse=True)
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

def expiry_filter(exp_date: date, index_name: str,tradingsymbol, expiry_type: str) -> bool:
    """
    Filters expiry based on weekly/monthly rules for NIFTY, SENSEX, CRUDE,
    considering NSE/BSE/MCX holidays.

    :param exp_date: datetime.date object (from instrument['expiry'])
    :param index_name: "NIFTY", "SENSEX", "CRUDE"
    :param expiry_type: "WEEKLY" or "MONTHLY"
    :return: True if the expiry matches the rules and is not a holiday
    """
    index_name = index_name.upper()
    expiry_type = expiry_type.upper()

    if expiry_type == "WEEKLY":
        # Weekly expiry rules
        if index_name == "NIFTY":
            target_weekday = 1  # Tuesday
        elif index_name == "SENSEX":
            target_weekday = 3  # Thursday
        else:
            # Default weekly = Tuesday
            target_weekday = 1

        # Check weekday
        if exp_date.weekday() != target_weekday:
            return False

    elif expiry_type == "MONTHLY":
        # Monthly expiry rules
        if index_name == "CRUDEOIL":
            month_code = exp_date.strftime("%b").upper()  # "OCT"
            if month_code not in tradingsymbol:
                return False
        else:
            # Default monthly expiry = last Thursday
            last_day = monthrange(exp_date.year, exp_date.month)[1]
            target_day = max(
                d for d in range(22, 29)
                if datetime(exp_date.year, exp_date.month, d).weekday() == 3
            )

        # if exp_date.day != target_day:
        #     return False

    # Check if the date is a holiday
    if exp_date in india_holidays:
        return False

    return True

def get_expiry_date(underlying: str) -> datetime.date:
    """
    Determines the next valid trading expiry date based on the underlying asset's rules.
    
    :param underlying: The asset name ('NIFTY', 'SENSEX', 'CRUDEOIL', etc.)
    :return: datetime.date object representing the valid expiry date.
    """
    underlying = underlying.upper()
    today = datetime.date.today()
    now_time = datetime.datetime.now().time()
    market_close_time = datetime.time(15, 30)

    # =========================================================================
    # 1. DETERMINE INITIAL EXPIRY CANDIDATE BASED ON ASSET RULES
    # =========================================================================
    
    if underlying == "NIFTY":
        # Target: Next Tuesday (weekday() == 1)
        target_weekday = 1
        days_until_target = (target_weekday - today.weekday() + 7) % 7
        expiry_candidate = today + datetime.timedelta(days=days_until_target)
        
        # Rollover if it's Tuesday and past market close
        if days_until_target == 0 and now_time > market_close_time:
             expiry_candidate += datetime.timedelta(days=7)
             
    elif underlying == "SENSEX":
        # Target: Next Thursday (weekday() == 3)
        target_weekday = 3
        days_until_target = (target_weekday - today.weekday() + 7) % 7
        expiry_candidate = today + datetime.timedelta(days=days_until_target)

        # Rollover if it's Thursday and past market close
        if days_until_target == 0 and now_time > market_close_time:
             expiry_candidate += datetime.timedelta(days=7)
             
    elif underlying == "CRUDEOIL":
        # Target: 19th of the current month, or next month if the 19th has passed.
        target_day = 19
        
        if today.day > target_day:
            # If the 19th has passed, roll to the 19th of the next month
            if today.month == 12:
                next_month = 1
                next_year = today.year + 1
            else:
                next_month = today.month + 1
                next_year = today.year
            expiry_candidate = datetime.date(next_year, next_month, target_day)
        else:
            # Use the 19th of the current month
            expiry_candidate = datetime.date(today.year, today.month, target_day)
            
    else:
        raise ValueError(f"Underlying '{underlying}' not supported for automatic expiry calculation.")


    # =========================================================================
    # 2. HOLIDAY AND WEEKEND ROLLBACK LOGIC
    # (This ensures the final date is a valid trading day)
    # =========================================================================

    holiday_strings = fetch_from_json("constants.json", "HOLIDAYS")
    if holiday_strings is None:
        holidays = set()
    else:
        # Assuming HOLIDAYS format is "%d-%m-%Y" (e.g., "15-08-2025")
        holidays = {datetime.datetime.strptime(d_str, "%d-%m-%Y").date() for d_str in holiday_strings}

    final_expiry = expiry_candidate
    
    while True:
        is_weekend = final_expiry.weekday() >= 5 # 5=Saturday, 6=Sunday
        is_holiday = final_expiry in holidays

        if not is_weekend and not is_holiday:
            # Found a valid trading day
            return final_expiry
            
        print(f"Adjusting expiry: {final_expiry.strftime('%d-%m-%Y')} is a holiday/weekend. Checking previous day.")
        
        # Roll back one day
        final_expiry -= datetime.timedelta(days=1)
        
        # Safety check for extreme rollback (optional, but good practice)
        if final_expiry < today - datetime.timedelta(days=30):
             raise Exception("Expiry rollback failed: date went too far into the past.")

        


def get_instrument_tokens_from_symbol(name):
    return find_matching_object("instrument_list.json" , "name" , name)["token"]

def get_instrument_details_from_json(name):
    return find_matching_object("instrument_list.json" , "name" , name)
