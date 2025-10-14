from datetime import datetime, date, timedelta
from calendar import monthrange
import holidays
from pprint import pprint
from datetime import datetime
from calendar import monthrange

# Load India holidays
india_holidays = holidays.India()  # You can add years: holidays.India(years=[2025])

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




def get_option_symbols(kite, trading_symbol,near_month_future_ltp, name, exchange, expiry_type,option_type,level=100,  ):
    """
    Get 5 option trading symbols for a given underlying.
    
    :param kite: KiteConnect object
    :param near_month_future_ltp: float, LTP of the near-month future
    :param name: str, underlying name ('NIFTY', 'BANKNIFTY', 'SENSEX', etc.)
    :param exchange: str, exchange code (e.g., 'NFO', 'BFO')
    :param level: int, strike interval (default 100)
    :param expiry_type: 'weekly' or 'monthly'
    :param option_type: 'CE' or 'PE'
    :return: list of 5 option symbols [ITM2, ITM1, ATM, OTM1, OTM2]
    """
    
    instruments = kite.instruments(exchange)

    option_instruments = [
        i for i in instruments
        if i['name'] == name
        and i['instrument_type'] == option_type  # must be OPT for options
        and i['tradingsymbol'].endswith(option_type)
    ]
    
    print(f"Total {option_type} options found for {name} in {exchange}: {len(option_instruments)}")

    if not option_instruments:
        raise ValueError(f"No {option_type} options found for {name} in {exchange} instruments.")
    
    option_instruments = [
        i for i in option_instruments
        if expiry_filter(i['expiry'],name,trading_symbol, expiry_type)
    ]
    
    if not option_instruments:
        raise ValueError(f"No options found for expiry_type={expiry_type}")
    
    # 4️⃣ Determine ATM strike using near-month future LTP
    atm_strike = round(near_month_future_ltp / level) * level
    
    # 5️⃣ Determine 5 strikes: 2 ITM, ATM, 2 OTM
    strikes = [
        atm_strike - 2*level,
        atm_strike - level,
        atm_strike,
        atm_strike + level,
        atm_strike + 2*level
    ]
    
    # 6️⃣ Map strikes to tradingsymbols (handle missing strikes)
    symbols = []
    for strike in strikes:
        sym = next((i['tradingsymbol'] for i in option_instruments if i['strike'] == strike), None)
        if sym is None:
            print(f"⚠️ Strike {strike} not found in instruments")
        symbols.append(sym)
    
    return symbols