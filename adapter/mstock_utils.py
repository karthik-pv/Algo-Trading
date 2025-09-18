import struct
import logging


def position_attribute_mgmt(positions):
    updated_positions = []
    for position in positions:
        updated_position = {
            "tradingsymbol": position.get("symbolname"),
            "average_price": position.get("buyavgprice"),
            "quantity": position.get("netqty"),
            "exchange": position.get("exchange"),
            "instrument_token": position.get("symboltoken"),
            "instrument": position.get("symboltoken"),
        }
        updated_positions.append(updated_position)
    return updated_positions


def parse_quote_message(binary_data):
    parsed_quotes = []
    try:
        if len(binary_data) < 4:
            logging.error("Received incomplete binary header (less than 4 bytes).")
            return parsed_quotes

        num_packets, total_bytes = struct.unpack("<HH", binary_data[:4])

        logging.info(
            f"Received {num_packets} packets with a total of {total_bytes} bytes each."
        )

        offset = 4

        for _ in range(num_packets):
            if len(binary_data) < offset + total_bytes:
                logging.error("Incomplete packet data received.")
                break

            packet_data = binary_data[offset : offset + total_bytes]

            try:
                (
                    subscription_mode,
                    exchange_type,
                    token,
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
                    upper_circuit,
                    lower_circuit,
                    fifty_two_week_high,
                    fifty_two_week_low,
                ) = struct.unpack(
                    "<BB25sQddQQddQQQQQQQQQQ", packet_data[:147] + packet_data[347:]
                )

                # Extract Market Depth as a separate chunk of 200 bytes
                market_depth_raw = packet_data[147:347]

                # Create a dictionary for the parsed packet
                parsed_packet = {
                    "subscription_mode": subscription_mode,
                    "exchange_type": exchange_type,
                    "token": token.decode("utf-8").strip(
                        "\x00"
                    ),  # Decode and strip null bytes
                    "sequence_number": sequence_number,
                    "exchange_timestamp": exchange_timestamp,
                    "ltp": ltp / 100.0,  # Prices might be scaled by 100
                    "last_traded_qty": last_traded_qty,
                    "avg_traded_price": avg_traded_price,
                    "volume_traded": volume_traded,
                    "total_buy_qty": total_buy_qty,
                    "total_sell_qty": total_sell_qty,
                    "open": open_price / 100.0,
                    "high": high_price / 100.0,
                    "low": low_price / 100.0,
                    "close": close_price / 100.0,
                    "last_traded_timestamp": last_traded_timestamp,
                    "open_interest": open_interest,
                    "open_interest_percent": open_interest_percent,
                    "market_depth_raw": market_depth_raw,  # You would need a separate function to parse this
                    "upper_circuit_limit": upper_circuit / 100.0,
                    "lower_circuit_limit": lower_circuit / 100.0,
                    "52_week_high": fifty_two_week_high / 100.0,
                    "52_week_low": fifty_two_week_low / 100.0,
                }
                parsed_quotes.append(parsed_packet)

            except struct.error as e:
                logging.error(f"Failed to unpack quote packet: {e}")
                # Log the raw data for debugging
                logging.debug(f"Raw packet data: {packet_data.hex()}")

            # Move the offset to the next packet's starting point
            offset += total_bytes

    except struct.error as e:
        logging.error(f"Failed to unpack binary header: {e}")

    return parsed_quotes
