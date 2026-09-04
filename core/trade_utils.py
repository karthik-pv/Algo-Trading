import datetime
from datetime import date
import calendar
from typing import Optional , Dict , List , Any
from calendar import monthrange
import holidays
from loguru import logger

india_holidays = holidays.India()


from utils import fetch_from_json , find_matching_object


def last_tuesday(year: int, month: int) -> datetime.date:
    logger.info(f"Calculating last Tuesday for {month}/{year}") 
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
    logger.debug("Calculating accurate average buy price and updating positions")
    if not positions or not orders:
        return positions or []

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
        logger.error(f"Error sorting orders, returning original positions. Error: {e}")
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
    logger.debug("Calculating current Nifty future month and year")
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
    logger.debug(f"Generating Nifty future symbol for root: {root}")
    """
    Generates the Nifty future symbol (e.g., NIFTYOCT25) for the current trading month.
    """
    month, year = get_current_nifty_future(today)
    mon_name = calendar.month_abbr[month].upper()
    yy = year % 100
    return f"{root}{mon_name}{yy:02d}"

def expiry_filter(exp_date: date, index_name: str,tradingsymbol, expiry_type: str) -> bool:
    logger.debug(f"Filtering expiry for {index_name} on {exp_date} as {expiry_type}")
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
    underlying = underlying.upper()
    month_map = {
        'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
        'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
    }
    try:
        year_str = fetch_from_json("constants.json", "EXPIRY_YEAR")
        month_abbr = fetch_from_json("constants.json", "EXPIRY_MONTH")
        year = int(year_str)
        month = month_map[month_abbr.upper()]

        if underlying == "NIFTY":
            day_str = fetch_from_json("constants.json", "NIFTY_EXPIRY_DATE")
            day = int(day_str)
        elif underlying == "SENSEX":
            day_str = fetch_from_json("constants.json", "SENSEX_EXPIRY_DATE")
            day = int(day_str)
        elif underlying == "CRUDEOIL" or underlying == "CRUDEOILM":
            day = 19
        else:
            raise ValueError(f"Underlying '{underlying}' not supported for pre-defined expiry.")
            
        final_expiry = datetime.date(year, month, day)
        logger.debug(f"Expiry is {final_expiry}")
        return final_expiry

    except (TypeError, KeyError, ValueError) as e:
        logger.error(f"Failed to construct expiry date. Check 'constants.json' and underlying. Error: {e}")
        raise

def get_instrument_tokens_from_symbol(name):
    logger.debug(f"Fetching instrument token for: {name}")
    return find_matching_object("mstock_instrument_list_reduced.json" , "name" , name)["token"]

def get_instrument_details_from_json(name):
    logger.debug(f"Fetching instrument details for: {name}")
    return find_matching_object("mstock_instrument_list_reduced.json" , "name" , name)
