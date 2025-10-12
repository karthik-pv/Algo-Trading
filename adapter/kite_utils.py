def fund_summary_attribute_mgmt(fund_summary):
    cropped_fund_summary = {}
    cropped_fund_summary["cash_balance"] = fund_summary["equity"]["available"]["live_balance"]
    cropped_fund_summary["utilized"] = fund_summary["equity"]["utilised"]["option_premium"]
    return cropped_fund_summary


def instrument_details_attribute_mgmt(instrument_details):
    return instrument_details