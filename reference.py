# NEW FUNCTIONS TO BE INTEGRATED ==========================================================================================================


#  PCT (%) PROFIT / LOSS  =======================================================================================================

def calculate_pct_profit_loss(buy_price: float, current_price: float) -> float:
    """
    Calculate percentage profit or loss.
    
    :param buy_price: The price at which the asset was bought
    :param current_price: The current market price
    :return: Profit/Loss percentage (positive for profit, negative for loss)
    """
    if buy_price <= 0:
        raise ValueError("Buy price must be greater than zero")
    
    pct_change = ((current_price - buy_price) / buy_price) * 100
    return pct_change


# Example usage
# if __name__ == "__main__":
#     buy = 19500
#     current = 19825
#     pl_pct = calculate_pct_profit_loss(buy, current)
#     print(f"Profit/Loss %: {pl_pct:.2f}%")

#  PCT (%) PROFIT / LOSS  =======================================================================================================


#  PTS PROFIT / LOSS  =======================================================================================================

def calculate_point_difference(buy_price: float, current_price: float) -> float:
    """
    Calculate point difference between current market price and buy price.
    
    :param buy_price: The price at which the asset was bought
    :param current_price: The current market price
    :return: Difference in points (positive = profit, negative = loss)
    """
    return current_price - buy_price


# Example usage
# if __name__ == "__main__":
#     buy = 19500
#     current = 19825
#     diff_points = calculate_point_difference(buy, current)
#     print(f"Difference in points: {diff_points}")

#  PTS PROFIT / LOSS  =======================================================================================================


# SELLING BASED ON % PROFIT / LOSS  =======================================================================================================
def should_trigger_sell_pct(buy_price: float, current_price: float, 
                        target_profit_pct: int, target_loss_pct: int) -> bool:
    """
    Decide whether to trigger a SELL order based on profit/loss percentage.
    
    :param buy_price: Price at which the position was bought
    :param current_price: Current market price
    :param target_profit_pct: Target profit percentage (integer, from frontend)
    :param target_loss_pct: Target loss percentage (integer, from frontend)
    :return: True if sell condition is met, else False
    """
    if buy_price <= 0:
        raise ValueError("Buy price must be greater than zero")

    diff_pct = ((current_price - buy_price) * 100) / buy_price

    # Case 1: Profit scenario
    if current_price > buy_price and diff_pct > target_profit_pct:
        return True

    # Case 2: Loss scenario
    if current_price < buy_price and abs(diff_pct) > target_loss_pct:
        return True

    return False
# SELLING BASED ON % PROFIT / LOSS  =======================================================================================================

# SELLING BASED ON PROFIT / LOSS POINTS  =======================================================================================================
def should_trigger_sell_points(buy_price: float, current_price: float, 
                               target_profit_points: int, target_loss_points: int) -> bool:
    """
    Decide whether to trigger a SELL order based on profit/loss in points.
    
    :param buy_price: Price at which the position was bought
    :param current_price: Current market price
    :param target_profit_points: Target profit in points (integer, from frontend)
    :param target_loss_points: Target loss in points (integer, from frontend)
    :return: True if sell condition is met, else False
    """
    diff_points = current_price - buy_price

    # Case 1: Profit scenario
    if diff_points > 0 and diff_points > target_profit_points:
        return True

    # Case 2: Loss scenario (negative diff_points)
    if diff_points < 0 and abs(diff_points) > target_loss_points:
        return True

    return False
# SELLING BASED ON PROFIT / LOSS POINTS  =======================================================================================================

# DETERMINE NEAR MONTH NIFTY FUTURE CONTRACT  =======================================================================================================

import datetime
import calendar

def last_thursday(year: int, month: int) -> datetime.date:
    """
    Return the date of the last Thursday of the given month/year.
    """
    # get the last day of month
    last_day = calendar.monthrange(year, month)[1]
    dt = datetime.date(year, month, last_day)
    # go backwards until we find a Thursday (weekday() == 3 for Thursday, since Monday=0)
    while dt.weekday() != 3:
        dt -= datetime.timedelta(days=1)
    return dt

def get_current_nifty_future(today: datetime.date = None) -> (int, int):
    """
    Determine the “current / nearest” Nifty Future contract month & year.
    
    :param today: date object; uses today if None
    :return: (month, year) of the active Nifty Future contract
    """
    if today is None:
        today = datetime.date.today()

    # candidate: this month’s expiry
    this_month = today.month
    this_year = today.year
    
    exp_this = last_thursday(this_year, this_month)
    # If expiry is in future (or same day), then this month is current
    if today <= exp_this:
        return (this_month, this_year)
    else:
        # else roll to next month
        # handle December → January of next year
        if this_month == 12:
            return (1, this_year + 1)
        else:
            return (this_month + 1, this_year)

def nifty_future_symbol(root: str = "NIFTY", today: datetime.date = None) -> str:
    """
    Build a symbol string for the current Nifty Future, e.g. “NIFTYSEP2025” or “NIFTY09_25”
    depending on your naming convention.
    :param root: root name / prefix of contract
    :param today: optional date
    :return: contract string
    """
    month, year = get_current_nifty_future(today)
    # Format month name or month number:
    # Example: append month in MMM, e.g. “SEP”, or mm, or _mm_yy
    mon_name = calendar.month_abbr[month].upper()  # e.g. 'SEP'
    # Year in 2 digits
    yy = year % 100
    # Example formats – you may vary
    return f"{root}{mon_name}{yy:02d}"

# Example usage
if __name__ == "__main__":
    print("Today:", datetime.date.today())
    mm, yy = get_current_nifty_future()
    print("Current Nifty Future month/year:", mm, yy)
    print("Contract symbol:", nifty_future_symbol("NIFTY"))
    # Example: if today is 2025-09-26, last Thursday of Sept 2025 would be (say) 25th,
    # so since today > expiry, it should roll to October 2025 => “NIFTYOCT25”

# DETERMINE NEAR MONTH NIFTY FUTURE CONTRACT  =======================================================================================================


# DETERMINE LOT_SIZE  =======================================================================================================

LOT_SIZE = 75   # Current NSE NIFTY options lot size (change if updated)

def get_available_funds(api) -> dict:
    """
    Fetch available cash & margin from MStock API.
    Replace with the actual API call.
    :return: dict with keys {'cash', 'margin'}
    """
    # Example placeholder response:
    # resp = api.get_funds()
    # return {"cash": resp["available_cash"], "margin": resp["available_margin"]}

    return {"cash": 200000, "margin": 100000}  # dummy for testing


def get_option_price(api, option_symbol: str) -> float:
    """
    Fetch the latest market price of the option (per share).
    Replace with actual API call to fetch LTP/ask price.
    """
    # resp = api.get_ltp(option_symbol)
    # return float(resp["last_price"])
    return 120.0  # dummy for testing


def calculate_max_lots(api, option_symbol: str, use_max_possible_margin: bool = False) -> int:
    """
    Calculate max NIFTY option lots you can buy.

    If use_max_possible_margin=False -> always return 1.
    If use_max_possible_margin=True  -> return maximum lots based on cash + margin.

    :param api: MStock API client
    :param option_symbol: The option contract symbol (e.g., "NIFTY25OCT22500CE")
    :param use_max_possible_margin: Flag for using cash + margin
    :return: lots as integer
    """
    option_price = get_option_price(api, option_symbol)
    cost_per_lot = option_price * LOT_SIZE

    if not use_max_possible_margin:
        return 1  # Fixed 1 lot case

    funds = get_available_funds(api)
    available_funds = funds["cash"] + funds["margin"]
    max_lots = int(available_funds // cost_per_lot)

    return max_lots if max_lots > 0 else 1

# Example usage
if __name__ == "__main__":
    api = None  # Replace with your MStock API client/session

    option_symbol = "NIFTY25OCT22500CE"  # example contract
    lots_cash_only = calculate_max_lots(api, option_symbol, use_max_possible_margin=False)
    lots_with_margin = calculate_max_lots(api, option_symbol, use_max_possible_margin=True)

    print(f"Lots (cash only flag=False): {lots_cash_only}")
    print(f"Lots (use margin flag=True): {lots_with_margin}")

# DETERMINE LOT_SIZE  =======================================================================================================

# GET THE LIST OF ATM,OTM1,OTM2,ITM1,ITM2 CE AND PE CONTRACTS  =======================================================================================================

import datetime
import calendar
from typing import Dict, Tuple

def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """
    Return the date of the n-th occurrence of the given weekday in the given month/year.
    weekday: 0 = Monday, …, 6 = Sunday
    n: 1,2,3,4,5 ...
    """
    # start from day=1
    d = datetime.date(year, month, 1)
    # shift to first weekday occurrence
    days_to_add = (weekday - d.weekday() + 7) % 7
    d_first = d + datetime.timedelta(days=days_to_add)
    # then add (n-1)*7 days
    return d_first + datetime.timedelta(days=(n-1)*7)

def get_weekly_expiry_date(today: datetime.date = None) -> datetime.date:
    """
    Returns the upcoming weekly expiry date (Thursday) for Nifty options.
    If today is before or on Thursday, return this week’s Thursday; else next week’s Thursday.
    """
    if today is None:
        today = datetime.date.today()
    # find this week’s Thursday
    thursday = today + datetime.timedelta(days=(3 - today.weekday() + 7) % 7)
    # If today is after Thursday (i.e. weekday > 3), we need next week's Thursday
    if today.weekday() > 3:
        thursday = thursday + datetime.timedelta(days=7)
    return thursday

def format_option_symbol_mstock(
    underlying: str,
    expiry: datetime.date,
    strike: int,
    call_or_put: str
) -> str:
    """
    Format symbol in MStock-like notation (example placeholder).  
    You need to adapt this to the exact MStock instrument naming convention.

    Example placeholder style: "NIFTYYYMMDD<strike><CE/PE>"
    e.g. "NIFTY24112519000CE" for Nifty expiry on 25 Nov 2024, strike 19000, call.
    """
    yy = expiry.year % 100
    mm = expiry.month
    dd = expiry.day
    # zero pad month/day
    mm_str = f"{mm:02d}"
    dd_str = f"{dd:02d}"
    # combine
    return f"{underlying}{yy}{mm_str}{dd_str}{strike}{call_or_put.upper()}"

def get_5_weekly_option_contracts(
    nifty_price: float,
    call_or_put: str,
    strike_interval: int = 100,
    underlying: str = "NIFTY"
) -> Dict[str, str]:
    """
    Return 5 weekly option contract symbols: ATM, OTM1, OTM2, ITM1, ITM2.
    :param nifty_price: current Nifty near-month future price (or index approximate)
    :param call_or_put: "CE" or "PE"
    :param strike_interval: interval between option strikes (e.g. 50, 100, etc.)
    :param underlying: underlying index name used in symbol (default "NIFTY")
    :return: dict mapping keys ("ITM2","ITM1","ATM","OTM1","OTM2") → contract symbol
    """
    # Round to nearest strike
    atm_strike = int(round(nifty_price / strike_interval) * strike_interval)

    # Get this week’s expiry
    expiry = get_weekly_expiry_date()

    def sym(s: int) -> str:
        return format_option_symbol_mstock(underlying, expiry, s, call_or_put)

    if call_or_put.upper() == "CE":
        return {
            "ITM2": sym(atm_strike - 2*strike_interval),
            "ITM1": sym(atm_strike - strike_interval),
            "ATM":  sym(atm_strike),
            "OTM1": sym(atm_strike + strike_interval),
            "OTM2": sym(atm_strike + 2*strike_interval),
        }
    elif call_or_put.upper() == "PE":
        # For puts, “in the money” means strike above ATM
        return {
            "ITM2": sym(atm_strike + 2*strike_interval),
            "ITM1": sym(atm_strike + strike_interval),
            "ATM":  sym(atm_strike),
            "OTM1": sym(atm_strike - strike_interval),
            "OTM2": sym(atm_strike - 2*strike_interval),
        }
    else:
        raise ValueError("call_or_put must be 'CE' or 'PE'")

# Example test
if __name__ == "__main__":
    price = 20047
    # e.g. call side
    contracts_ce = get_5_weekly_option_contracts(price, "CE", strike_interval=50)
    contracts_pe = get_5_weekly_option_contracts(price, "PE", strike_interval=50)
    print("Calls:", contracts_ce)
    print("Puts: ", contracts_pe)

# GET THE LIST OF ATM,OTM1,OTM2,ITM1,ITM2 CE AND PE CONTRACTS  =======================================================================================================


# GET 1M EMAS OF NEAR MONTH NIFTY FUTURE   =======================================================================================================

def should_trigger_sell_ema(current_price: float, target_ema: int, tolerance_ema: int) -> bool:
    """
    Stub: uses the below functions to check if the market price  is below selected ema - tolerance_ema. If so returns true.
    """
    
    raise NotImplementedError("Please implement fetch via your MStock API SDK / HTTP")


def fetch_1m_bars(symbol: str, from_ts: int, to_ts: int) -> pd.DataFrame:
    """
    Stub: fetch 1-minute OHLC (or close) bars from MStock Trading API.
    Returns a DataFrame indexed by timestamp (or datetime) with a ‘close’ column.
    """
    # Example pseudo-code – replace with actual API calls
    # resp = mstock_api.get_historical(symbol=symbol, interval="1m", from_ts=from_ts, to_ts=to_ts)
    # data = resp['data']
    # df = pd.DataFrame(data)
    # df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
    # df.set_index('datetime', inplace=True)
    # return df[['close']]
    raise NotImplementedError("Please implement fetch via your MStock API SDK / HTTP")

def compute_emas(close_series: pd.Series, periods: list[int]) -> dict[int, pd.Series]:
    """
    Compute EMA series for given periods.

    :param close_series: pd.Series of close prices (indexed by datetime)
    :param periods: list of integer lookback periods, e.g. [9, 21, 50, 100, 200]
    :return: dict mapping period → EMA series (same index as close_series)
    """
    ema_dict = {}
    for period in periods:
        # use pandas ewm
        ema = close_series.ewm(span=period, adjust=False, min_periods=period).mean()
        ema_dict[period] = ema
    return ema_dict

def get_nifty_ema_1m(symbol: str, lookback_minutes: int = 300) -> dict[int, float]:
    """
    Fetch recent 1m bars for the symbol and return the latest EMA values for
    9, 21, 50, 100, 200 periods.

    :param symbol: e.g. “NIFTY_FUTURE_NEAR” or whatever your symbol identifier is
    :param lookback_minutes: how many minutes of data to fetch (should cover the largest period)
    :return: dict mapping period → latest EMA (float)
    """
    now_ts = int(time.time())
    from_ts = now_ts - lookback_minutes * 60

    df = fetch_1m_bars(symbol, from_ts, now_ts)
    # Expect df to have df['close']
    if 'close' not in df.columns:
        raise RuntimeError("Fetched data missing 'close' column")

    ema_dict = compute_emas(df['close'], periods=[9, 21, 50, 100, 200])
    # Return only the latest value for each period
    latest_ema = {p: ema_dict[p].iloc[-1] for p in ema_dict}
    return latest_ema

# Example of how you’d use it:
if __name__ == "__main__":
    symbol = "NIFTY_NEAR_FUT"  # placeholder, replace with actual
    try:
        ema_values = get_nifty_ema_1m(symbol, lookback_minutes=300)
        print("Latest EMAs:", ema_values)
    except Exception as e:
        print("Error:", e)

class IncrementalEMA:
    def __init__(self, periods=[9, 21, 50, 100, 200]):
        """
        Initialize EMA calculator for multiple periods.
        :param periods: List of EMA periods
        """
        self.periods = periods
        self.multipliers = {p: 2 / (p + 1) for p in periods}
        self.ema_values = {p: None for p in periods}  # store last EMA values

    def update(self, price: float) -> dict[int, float]:
        """
        Update EMA values with the latest price.
        :param price: Latest 1m close price
        :return: dict {period: updated EMA value}
        """
        for p in self.periods:
            alpha = self.multipliers[p]

            if self.ema_values[p] is None:
                # initialize EMA with first price
                self.ema_values[p] = price
            else:
                # recursive update
                self.ema_values[p] = (alpha * price) + (1 - alpha) * self.ema_values[p]

        return self.ema_values.copy()

    def get_ema(self, period: int) -> float | None:
        """
        Get the latest EMA value for a given period.
        """
        return self.ema_values.get(period, None)



# GET 1M EMAS OF NEAR MONTH NIFTY FUTURE   =======================================================================================================