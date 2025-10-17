import struct
import logging
from pprint import pprint
from loguru import logger


def position_attribute_mgmt(positions):
    logger.info(f"Raw positions data: {positions}")
    updated_positions = []
    if positions:
        for position in positions:
            updated_position = {
                "tradingsymbol": position.get("symbolname"),
                "average_price": float(position.get("buyavgprice")),
                "quantity": int(position.get("netqty")),
                "exchange": position.get("exchange"),
                "instrument_token": position.get("symboltoken"),
                "instrument": (position.get("symboltoken")),
                "exchange" : position.get("exchange"),
                "lotsize" : position.get("lotsize")
            }
            updated_positions.append(updated_position)
    return updated_positions

def order_attribute_mgmt(orders):
    logger.info(f"Raw orders data: {orders}")
    updated_orders = []
    if orders:
        for order in orders:
            updated_order = {
                "tradingsymbol" : order.get("tradingsymbol") , 
                "timestamp" : order.get("exchorderupdatetime"),
                "transaction_type" : order.get("transactiontype"),
                "quantity" : order.get("quantity"),
                "average_price" : order.get("averageprice")
            }
            updated_orders.append(updated_order)
    return updated_orders


def fund_summary_attribute_mgmt(fund_summary):
    logger.info(f"Raw fund summary data: {fund_summary}")
    cropped_fund_summary = {}
    cropped_fund_summary["cash_balance"] = fund_summary["data"][0]["AVAILABLE_BALANCE"]
    cropped_fund_summary["utilized"] = fund_summary["data"][0]["AMOUNT_UTILIZED"]
    return cropped_fund_summary

def instrument_details_attribute_mgmt(instrument_details):
    logger.info(f"Raw instrument details data: {instrument_details}")
    instrument_details["tradingsymbol"] = instrument_details["name"]
    instrument_details["instrument_token"] = instrument_details["token"]
    instrument_details["lot_size"] = instrument_details["lotsize"]
    return instrument_details


def parse_market_depth(market_depth_data):
    #logger.info(f"Raw market depth data: {market_depth_data}")
    """Parses the 200-byte market depth data into bids and asks."""
    depth_items = []
    item_format = "<hqqh"
    item_size = struct.calcsize(item_format)

    for i in range(10):
        offset = i * item_size
        item_data = market_depth_data[offset : offset + item_size]

        flag, qty, price, num_orders = struct.unpack(item_format, item_data)

        depth_items.append(
            {
                "type": "bid" if i < 5 else "ask",
                "quantity": qty,
                "price": price / 100.0,
                "orders": num_orders,
            }
        )

    return {"bids": depth_items[:5], "asks": depth_items[5:]}


def parse_quote_message(binary_data):
    #logger.info(f"Raw binary quote data: {binary_data}")
    if len(binary_data) != 379:
        logger.error(
            f"Incomplete packet. Expected 379 bytes, but got {len(binary_data)}."
        )
        return None

    try:
        full_packet_format = (
            "<" "B" "B" "25s" + "q" * 6 + "d" * 2 + "q" * 7 + "200s" + "q" * 4
        )

        unpacked_data = struct.unpack(full_packet_format, binary_data)

        (
            subscription_mode,
            exchange_type,
            token_bytes,
            sequence_number,
            exchange_timestamp,
            ltp,
            last_traded_qty,
            avg_traded_price,
            volume_traded,
            total_buy_qty,
            total_sell_qty,
            open_price,
            high_price,
            low_price,
            close_price,
            last_traded_timestamp,
            open_interest,
            open_interest_percent,
            market_depth_raw,
            upper_circuit,
            lower_circuit,
            fifty_two_week_high,
            fifty_two_week_low,
        ) = unpacked_data

        price_scale = 100.0

        parsed_packet = {
            "subscription_mode": subscription_mode,
            "exchange_type": exchange_type,
            "token": token_bytes.decode("utf-8").strip("\x00"),
            "sequence_number": sequence_number,
            "exchange_timestamp": exchange_timestamp,
            "ltp": ltp / price_scale,
            "last_traded_qty": last_traded_qty,
            "avg_traded_price": avg_traded_price / price_scale,
            "volume_traded": volume_traded,
            "total_buy_qty": total_buy_qty,
            "total_sell_qty": total_sell_qty,
            "open": open_price / price_scale,
            "high": high_price / price_scale,
            "low": low_price / price_scale,
            "close": close_price / price_scale,
            "last_traded_timestamp": last_traded_timestamp,
            "open_interest": open_interest,
            "open_interest_percent": open_interest_percent,
            "market_depth": parse_market_depth(market_depth_raw),
            "upper_circuit_limit": upper_circuit / price_scale,
            "lower_circuit_limit": lower_circuit / price_scale,
            "52_week_high": fifty_two_week_high / price_scale,
            "52_week_low": fifty_two_week_low / price_scale,
        }
        return parsed_packet

    except struct.error as e:
        logger.error(f"Failed to unpack quote packet: {e}")
        return None


