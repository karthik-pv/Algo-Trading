def position_attribute_mgmt(positions):
    for position in positions:
        position["lotsize"] = position["multiplier"]
        position["instrument_token"] = str(position["instrument_token"])
        position["net_qty"] = position["quantity"]
    return positions
    

def fund_summary_attribute_mgmt(fund_summary):
    cropped_fund_summary = {}
    cropped_fund_summary["cash_balance"] = fund_summary["equity"]["available"]["live_balance"]
    cropped_fund_summary["utilized"] = fund_summary["equity"]["utilised"]["option_premium"]
    return cropped_fund_summary


def instrument_details_attribute_mgmt(instrument_details):
    return instrument_details

def orders_attribute_mgmt(orders):
    for order in orders:
        order["timestamp"] = order["order_timestamp"]
    return orders