import datetime
import calendar

def last_thursday(year: int, month: int):
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
    yy = year % 100
    return f"{root}{mon_name}{yy:02d}"