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
    if instrument_details["exchange"] == "MCX":
        if instrument_details["tradingsymbol"].startswith("CRUDEOILM"):
            instrument_details["lot_size"] = fetch_from_json("constants.json" , "LOT_SIZE_CRUDEOILM")
        elif instrument_details["tradingsymbol"].startswith("CRUDEOIL"):
            instrument_details["lot_size"] = fetch_from_json("constants.json" , "LOT_SIZE_CRUDEOIL")
    return instrument_details

def orders_attribute_mgmt(orders):
    for order in orders:
        order["timestamp"] = order["order_timestamp"]
    return orders