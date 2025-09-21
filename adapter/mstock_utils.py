import struct
import logging
from pprint import pprint


def position_attribute_mgmt(positions):
    # updated_positions = []
    # if positions:
    #     for position in positions:
    #         updated_position = {
    #             "tradingsymbol": position.get("symbolname"),
    #             "average_price": position.get("buyavgprice"),
    #             "quantity": position.get("netqty"),
    #             "exchange": position.get("exchange"),
    #             "instrument_token": position.get("symboltoken"),
    #             "instrument": position.get("symboltoken"),
    #         }
    #         updated_positions.append(updated_position)
    # return updated_positions

    return [
        {
            "tradingsymbol": "idea",
            "average_price": 975,
            "quantity": "3",
            "exchange": "NSE",
            "instrument_token": 1333,
            "instrument": 1333,
        }
    ]


def parse_market_depth(market_depth_data):
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
    if len(binary_data) != 379:
        logging.error(
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
        logging.error(f"Failed to unpack quote packet: {e}")
        return None


# raw_byte_stream = b"\x03\x011333\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00v\x8a\xfeU\x00\x00\x00\x00uz\x01\x00\x00\x00\x00\x00K\x00\x00\x00\x00\x00\x00\x00\xe5{\x01\x00\x00\x00\x00\x00S\xe98\x00\x00\x00\x00\x00\x00\x00\x00\x008\xf2 A\x00\x00\x00\x00&^*A\xd2|\x01\x00\x00\x00\x00\x00\x86}\x01\x00\x00\x00\x00\x00Rz\x01\x00\x00\x00\x00\x00\x9a}\x01\x00\x00\x00\x00\x00v\x8a\xfeU\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x90\x01\x00\x00\x00\x00\x00\x00zz\x01\x00\x00\x00\x00\x00\x02\x00\x00\x00<\x03\x00\x00\x00\x00\x00\x00uz\x01\x00\x00\x00\x00\x00\x02\x00\x00\x00I\x01\x00\x00\x00\x00\x00\x00pz\x01\x00\x00\x00\x00\x00\x05\x00\x00\x00.\x03\x00\x00\x00\x00\x00\x00kz\x01\x00\x00\x00\x00\x00\x06\x00\x00\x00\xb0\x01\x00\x00\x00\x00\x00\x00fz\x01\x00\x00\x00\x00\x00\t\x00\x00\x00=\x01\x00\x00\x00\x00\x00\x00\x84z\x01\x00\x00\x00\x00\x00\x06\x00\x00\x00D\x00\x00\x00\x00\x00\x00\x00\x89z\x01\x00\x00\x00\x00\x00\x02\x00\x00\x00\x1a\x00\x00\x00\x00\x00\x00\x00\x8ez\x01\x00\x00\x00\x00\x00\x02\x00\x00\x00\t\x00\x00\x00\x00\x00\x00\x00\x93z\x01\x00\x00\x00\x00\x00\x01\x00\x00\x00\x08\x01\x00\x00\x00\x00\x00\x00\x98z\x01\x00\x00\x00\x00\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
# pprint(parse_quote_message(raw_byte_stream))
# print(parse_quote_message(raw_byte_stream)["close"])
