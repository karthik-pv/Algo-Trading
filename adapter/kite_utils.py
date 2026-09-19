from loguru import logger
from utils import fetch_from_json

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