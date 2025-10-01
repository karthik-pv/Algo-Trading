import datetime
import calendar
from typing import Optional , Dict

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

def format_option_symbol_mstock(
    underlying: str,
    expiry: datetime.date,
    strike: int,
    call_or_put: str
) -> str:
    yy = expiry.year % 100
    mm_str = f"{expiry.month:02d}"
    dd_str = f"{expiry.day:02d}"
    return f"{underlying}{yy}{mm_str}{dd_str}{strike}{call_or_put.upper()}"

def get_weekly_expiry_date(today: Optional[datetime.date] = None) -> datetime.date:
    """
    Returns upcoming weekly expiry date (Thursday).
    If today is before or on Thursday, return this week's Thursday; else next week's Thursday.
    """
    if today is None:
        today = datetime.date.today()
    # This week's Thursday (weekday() Monday=0 .. Sunday=6; Thursday==3)
    thursday = today + datetime.timedelta(days=(1 - today.weekday() + 7) % 7)
    # if we've passed Thursday this week, move to next week's Thursday
    if today.weekday() > 3:
        thursday += datetime.timedelta(days=7)
    return thursday

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

def get_5_weekly_option_contracts(
    broker_instance ,
    nifty_price: float,
    call_or_put: str,
    strike_interval: int = 100,
    underlying: str = "NIFTY",
    today: Optional[datetime.date] = None , 
    
) -> Dict[str, any]:
    if strike_interval <= 0:
        raise ValueError("strike_interval must be positive integer")

    # Floor to the nearest lower strike -> ATM (ensures ATM <= current price)
    atm_strike = int((nifty_price // strike_interval) * strike_interval)

    # Pass the 'today' argument down to the expiry calculation function
    expiry = get_weekly_expiry_date(today)

    if call_or_put.upper() == "CE":
        strikes = {
            "ITM2": atm_strike - 2 * strike_interval,
            "ITM1": atm_strike - 1 * strike_interval,
            "ATM":  atm_strike,
            "OTM1": atm_strike + 1 * strike_interval,
            "OTM2": atm_strike + 2 * strike_interval,
        }
    elif call_or_put.upper() == "PE":
        strikes = {
            # for puts, ITM = strikes above spot
            "ITM2": atm_strike + 2 * strike_interval,
            "ITM1": atm_strike + 1 * strike_interval,
            "ATM":  atm_strike,
            "OTM1": atm_strike - 1 * strike_interval,
            "OTM2": atm_strike - 2 * strike_interval,
        }
    else:
        raise ValueError("call_or_put must be 'CE' or 'PE'")

    # Normalize: if any strike < 0, set to None
    for k, v in strikes.items():
        if v is None or v < 0:
            strikes[k] = None

    symbols = {
        k: (broker_instance.format_option_symbol(underlying, expiry, v, call_or_put) if v is not None else None)
        for k, v in strikes.items()
    }

    return {"expiry": expiry, "strikes": strikes, "symbols": symbols}

