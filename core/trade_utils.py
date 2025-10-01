import datetime
import calendar
from typing import Optional , Dict

from utils import fetch_from_json

def last_thursday(year: int, month: int):
    # get the last day of month
    last_day = calendar.monthrange(year, month)[1]
    dt = datetime.date(year, month, last_day)
    # go backwards until we find a Thursday (weekday() == 3 for Thursday, since Monday=0)
    while dt.weekday() != 3:
        dt -= datetime.timedelta(days=1)
    return dt


def get_current_nifty_future(today: datetime.date = None):

    if today is None:
        today = datetime.date.today()

    this_month = today.month
    this_year = today.year
    
    exp_this = last_thursday(this_year, this_month)
    if today <= exp_this:
        return (this_month, this_year)
    else:
        if this_month == 12:
            return (1, this_year + 1)
        else:
            return (this_month + 1, this_year)

def nifty_future_symbol(root: str = "NIFTY", today: datetime.date = None) -> str:
    month, year = get_current_nifty_future(today)
    mon_name = calendar.month_abbr[month].upper()
    print(f"MON _ NAME {mon_name}")
    yy = year % 100
    return f"{root}{mon_name}{yy:02d}"

def get_weekly_expiry_date(today: Optional[datetime.date] = None) -> datetime.date:
    if today is None:
        today = datetime.date.today()
    # This week's Thursday (weekday() Monday=0 .. Sunday=6; Thursday==3)
    thursday = today + datetime.timedelta(days=(3 - today.weekday() + 7) % 7)
    # if we've passed Thursday this week, move to next week's Thursday
    if today.weekday() > 3:
        thursday += datetime.timedelta(days=7)
    return thursday

def get_weekly_expiry_date() -> datetime.date:
    today = datetime.date.today()

    days_until_thursday = (3 - today.weekday() + 7) % 7
    expiry_candidate = today + datetime.timedelta(days=days_until_thursday)

    if days_until_thursday == 0 and datetime.datetime.now().time() > datetime.time(15, 30):
         expiry_candidate += datetime.timedelta(days=7)
    elif today.weekday() > 3:
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


def format_option_symbol_mstock(
    underlying: str,
    expiry: datetime.date,
    strike: int,
    call_or_put: str
) -> str:
    yy = expiry.year % 100
    # Get the first letter of the month name, e.g., 'October' -> 'O'
    month_char = calendar.month_name[expiry.month][0].upper()
    dd_str = f"{expiry.day:02d}"
    return f"{underlying}{yy}{month_char}{dd_str}{strike}{call_or_put.upper()}"



