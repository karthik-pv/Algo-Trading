import datetime
import calendar
import math
from typing import Dict, Optional
from utils import fetch_from_json

def get_weekly_expiry_date(today: Optional[datetime.date] = None) -> datetime.date:
    """
    Returns the upcoming weekly expiry date, adjusted for holidays and weekends.
    Calculates the upcoming Tuesday and then moves backwards to the previous working day
    if the Tuesday is a holiday or a weekend.
    """
    if today is None:
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

def format_option_symbol_mstock(
    underlying: str,
    expiry: datetime.date,
    strike: int,
    call_or_put: str
) -> str:
    """
    Generates an option symbol in the format:
    <UNDERLYING><YY><MonthChar><DD><STRIKE><CE/PE>
    Example: 'NIFTY25O0224900PE' for expiry 02-Oct-2025 strike 24900 Put.
    """
    yy = expiry.year % 100
    # Get the first letter of the month name, e.g., 'October' -> 'O'
    month_char = calendar.month_name[expiry.month][0].upper()
    dd_str = f"{expiry.day:02d}"
    return f"{underlying}{yy}{month_char}{dd_str}{strike}{call_or_put.upper()}"

def get_5_weekly_option_contracts(
    nifty_price: float,
    call_or_put: str,
    strike_interval: int = 100,
    underlying: str = "NIFTY",
    today: Optional[datetime.date] = None
) -> Dict[str, any]:
    """
    Return 5 weekly option contracts (numeric strikes + MStock-style symbols).
    Keys returned: ITM2, ITM1, ATM, OTM1, OTM2

    :param nifty_price: current Nifty price (e.g. 24919)
    :param call_or_put: 'CE' or 'PE'
    :param strike_interval: strike step (50, 100, ...)
    :param underlying: underlying symbol prefix
    :param today: optional date to calculate expiry from (for testing)
    :return: dict with 'expiry', 'strikes', and 'symbols' sub-dicts
    """
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
        k: (format_option_symbol_mstock(underlying, expiry, v, call_or_put) if v is not None else None)
        for k, v in strikes.items()
    }

    return {"expiry": expiry, "strikes": strikes, "symbols": symbols}


# ---------- Example usage ----------
if __name__ == "__main__":
    price = 24778.3
    today_date = datetime.date(2025,10,1) # A Wednesday
    
    # Correctly pass 'today' as a keyword argument
    res_ce = get_5_weekly_option_contracts(price, "CE", strike_interval=100, today=today_date)
    res_pe = get_5_weekly_option_contracts(price, "PE", strike_interval=100, today=today_date)

    print(f"Assuming Spot Price: {price} on {today_date.strftime('%Y-%m-%d')}")
    print(f"Calculated Weekly Expiry: {res_ce['expiry'].strftime('%Y-%m-%d')}\n")
    
    print("--- Call Options ---")
    print("Strikes:", res_ce["strikes"])
    print("Symbols:", res_ce["symbols"])
    
    print("\n--- Put Options ---")
    print("Strikes:", res_pe["strikes"])
    print("Symbols:", res_pe["symbols"])

