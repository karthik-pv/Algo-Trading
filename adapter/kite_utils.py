from loguru import logger
import pandas as pd

from core.utils import fetch_from_json

def position_attribute_mgmt(positions):
    for position in positions:
        position["instrument_token"] = str(position["instrument_token"])
    logger.debug(f"POSITIONS{positions}")
    return positions
    

def fund_summary_attribute_mgmt(fund_summary):
    cropped_fund_summary = {}
    
    live_balance = fund_summary["equity"]["available"]["live_balance"]
    collateral = fund_summary["equity"]["available"].get("collateral", 0)

    #cropped_fund_summary["cash_balance"] = fund_summary["equity"]["available"]["live_balance"]
    cropped_fund_summary["cash_balance"] = live_balance + collateral

    cropped_fund_summary["utilized"] = fund_summary["equity"]["utilised"]["option_premium"]
    return cropped_fund_summary


def instr_det_attrib_mgmt(instrument_details):
    if instrument_details.get("exchange") == "MCX":
        # The daily-downloaded instrument CSV should carry the authoritative
        # lot_size straight from the exchange, but the Kite MCX dump returns
        # lot_size=1 for every instrument. A lot size of 1 is never valid for
        # the crude contracts, so only trust the CSV when it is greater than
        # 1; otherwise fall back to the constants so orders cannot be
        # silently mis-sized.
        csv_lot_size = instrument_details.get("lot_size")
        try:
            csv_lot_size = int(float(csv_lot_size))
        except (TypeError, ValueError):
            csv_lot_size = 0

        if csv_lot_size and csv_lot_size > 1:
            instrument_details["lot_size"] = csv_lot_size
        elif str(instrument_details.get("tradingsymbol", "")).startswith("CRUDEOILM"):
            instrument_details["lot_size"] = int(fetch_from_json("appconfig.json" , "LOT_SIZE_CRUDEOILM"))
        elif str(instrument_details.get("tradingsymbol", "")).startswith("CRUDEOIL"):
            instrument_details["lot_size"] = int(fetch_from_json("appconfig.json" , "LOT_SIZE_CRUDEOIL"))
    return instrument_details

def orders_attribute_mgmt(orders):
    for order in orders:
        order["timestamp"] = order["order_timestamp"]
    return orders


_KITE_TRADEBOOK_REQUIRED_COLUMNS = (
    "symbol", "trade_type", "quantity", "price", "order_execution_time",
)


def parse_kite_tradebook_csv(csv_path):
    """
    Parse a Zerodha Console tradebook CSV (tradebook-<client>-<segment>.csv)
    into normalized order dicts shaped like
    KiteAdapter.fetch_all_orders_for_export() output, ready for
    build_orders_export().

    Kite's API serves same-day orders only, so past dates are recovered
    from this report downloaded from Console. Every tradebook row is an
    executed fill, so each record is reported as COMPLETE.

    Returns (records, trade_dates) where trade_dates is the sorted list
    of distinct ISO trade dates present in the file (may span several
    days when the report covers a range).
    """
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df.columns = [str(column).strip().lower() for column in df.columns]

    missing = [
        column for column in _KITE_TRADEBOOK_REQUIRED_COLUMNS
        if column not in df.columns
    ]
    if missing:
        raise ValueError(
            "Not a Kite Console tradebook CSV - missing column(s): "
            + ", ".join(missing)
        )

    df = df.rename(columns={"symbol": "tradingsymbol"})
    df["timestamp"] = df["order_execution_time"].astype(str).str.strip()
    df["transaction_type"] = df["trade_type"].astype(str).str.strip().str.upper()
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["average_price"] = pd.to_numeric(df["price"], errors="coerce")
    df["order_status"] = "COMPLETE"

    df = df.dropna(subset=["timestamp", "quantity", "average_price"])
    df = df[
        df["tradingsymbol"].astype(str).str.strip().ne("")
        & df["timestamp"].str.lower().ne("nan")
        & (df["quantity"] != 0)
    ]

    if df.empty:
        raise ValueError("No executed trades found in the tradebook CSV")

    if "trade_date" in df.columns:
        trade_dates = sorted(
            str(value).strip()[:10]
            for value in df["trade_date"].dropna().unique()
            if str(value).strip()
        )
    else:
        trade_dates = []

    records = [
        {
            "timestamp": row["timestamp"],
            "tradingsymbol": str(row["tradingsymbol"]).strip(),
            "transaction_type": row["transaction_type"],
            "quantity": row["quantity"],
            "average_price": row["average_price"],
            "order_status": "COMPLETE",
            "order_id": row.get("order_id"),
        }
        for row in df.to_dict(orient="records")
    ]
    logger.info(
        f"Parsed Kite tradebook CSV: {len(records)} fills, "
        f"dates={trade_dates}"
    )
    return records, trade_dates