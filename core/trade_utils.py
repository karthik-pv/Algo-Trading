import datetime
import calendar
from typing import Optional , Dict

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
