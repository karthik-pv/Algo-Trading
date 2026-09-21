import struct
import logging
from pprint import pprint
from loguru import logger
import pandas as pd
import os
import tempfile
import re

from utils import fetch_from_json, get_underlying_for_symbol, get_exchange_for_underlying


def position_attribute_mgmt(positions):
    logger.info(f"Raw positions data: {positions}")
    updated_positions = []
    if positions:
        for position in positions:
            updated_position = {
                "tradingsymbol": position.get("symbolname"),
                "average_price": float(position.get("buyavgprice")),
                "quantity": int(position.get("netqty"))/int(position.get("lotsize")),
                "exchange": position.get("exchange"),
                "instrument_token": position.get("symboltoken"),
                "instrument": (position.get("symboltoken")),
                "exchange" : position.get("exchange"),
                "lotsize" : position.get("lotsize"),
                # Actual broker product (CARRYFORWARD/MARGIN/INTRADAY) so
                # exit sells net against the position instead of opening
                # a fresh short (which the RMS rejects for span).
                "producttype": position.get("producttype") or "CARRYFORWARD"
            }
            updated_positions.append(updated_position)
    return updated_positions

def order_attribute_mgmt(orders):
    #logger.info(f"Raw orders data: {orders}")
    updated_orders = []
    if orders:
        for order in orders:
            updated_order = {
                "tradingsymbol" : order.get("tradingsymbol") , 
                "timestamp" : order.get("exchorderupdatetime"),
                "transaction_type" : order.get("transactiontype"),
                "quantity" : order.get("quantity"),
                "average_price" : order.get("averageprice"),
                # Resting LIMIT price (the U/D exit target); stays the
                # order's limit while pending, unlike average_price which
                # only fills in as the order executes.
                "price" : order.get("price"),
                "order_type" : order.get("ordertype"),
                "order_status": order.get("orderstatus") or order.get("status"),
                "order_id": (order.get("orderid")),
                # For the pending-grid joins (position leg + lot size).
                "token": order.get("symboltoken") or order.get("token"),
                "exchange": order.get("exchange")
            }
            updated_orders.append(updated_order)
    return updated_orders


def fund_summary_attribute_mgmt(fund_summary):
    #logger.info(f"Raw fund summary data: {fund_summary}")
    cropped_fund_summary = {}
    cropped_fund_summary["cash_balance"] = fund_summary["data"][0]["AVAILABLE_BALANCE"]
    cropped_fund_summary["utilized"] = fund_summary["data"][0]["AMOUNT_UTILIZED"]
    return cropped_fund_summary

def instr_det_attrib_mgmt(instrument_details):
    if not instrument_details:
        return None
    logger.info(f"Raw instrument details data: {instrument_details}")
    instrument_details["tradingsymbol"] = instrument_details["name"]
    instrument_details["instrument_token"] = instrument_details["token"]
    instrument_details["lot_size"] = instrument_details["lotsize"]
    return instrument_details


def normalize_contract_symbol(symbol):
    """Extract the broker instrument name from a contract-note/order symbol."""
    text = str(symbol or "").upper()

    compact_match = re.search(r"((?:NIFTY|SENSEX)\d+(?:CE|PE))", text)
    if compact_match:
        return compact_match.group(1)

    formatted_match = re.search(
        r"\b(NIFTY|SENSEX)\s+(\d{2})([A-Z]{3})(\d{2})\s+(\d+(?:\.00)?)\s+(CE|PE)\b",
        text,
    )
    if formatted_match:
        underlying, day, month_name, year, strike, call_or_put = formatted_match.groups()
        month = pd.to_datetime(month_name, format="%b").strftime("%m")
        return f"{underlying}{year}{month}{day}{strike.removesuffix('.00')}{call_or_put}"

    return re.sub(r"[^A-Z0-9]", "", text)


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


def make_timestamp_expr(self,ts):
    return f"timestamp('GMT',{ts.year},{ts.month},{ts.day},{ts.hour},{ts.minute},{ts.second})"


def format_lakhs(amount):
    """Port of the VBA FormatLakhs helper.

    Renders an amount in lakhs with trailing zeros trimmed, so
    900000 -> "9L", 1250000 -> "12.5L" and 80000 -> ".8L" exactly
    like the VBA ConsolidatedOrders workbook.
    """
    text = f"{round(amount / 100000, 1):.1f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text.startswith("0."):
        text = text[1:]
    elif text.startswith("-0."):
        text = "-" + text[2:]
    return f"{text}L"


TXN_CHARGE_KEYS_BY_EXCHANGE = {
    "NFO": ("TXN_CHARGES_NSE_PCT", 0.03553),
    "BFO": ("TXN_CHARGES_BSE_PCT", 0.0325),
}


def _charges_rate(key, default):
    """
    Read a charges rate from appconfig.json, falling back to the
    published broker schedule when the key is missing or invalid.

    Brokers publish different rates (e.g. MSTOCK vs KITE brokerage and
    sell-side STT), so a broker-suffixed override key "<KEY>_<BROKER>"
    (e.g. BROKERAGE_PER_ORDER_KITE) always wins over the base key.
    """
    broker = str(fetch_from_json("appconfig.json", "BROKER") or "").strip().upper()
    if broker:
        try:
            override = fetch_from_json("appconfig.json", f"{key}_{broker}")
            if override is not None and str(override).strip() != "":
                return float(override)
        except (TypeError, ValueError):
            pass
    try:
        return float(fetch_from_json("appconfig.json", key) or default)
    except (TypeError, ValueError):
        return default


def compute_option_trade_charges(transaction_type, turnover, symbol):
    """
    Compute (brokerage, other_charges) for one F&O option order.

    Turnover is the premium value (units x rate). Other charges bundle
    STT (sell side), exchange transaction charges, SEBI turnover fees,
    GST on (brokerage + SEBI + transaction) and buy-side stamp duty.
    Exercise charges (0.15% of intrinsic value on bought-and-exercised
    options) never appear as grid rows and are intentionally excluded.
    """
    transaction_type = str(transaction_type or "").upper()
    turnover = max(0.0, float(turnover or 0.0))

    brokerage = _charges_rate("BROKERAGE_PER_ORDER", 20.0)
    stt_sell_pct = _charges_rate("OPTIONS_STT_SELL_PCT", 0.15)
    sebi_per_crore = _charges_rate("SEBI_CHARGES_PER_CRORE", 10.0)
    gst_pct = _charges_rate("GST_PCT", 18.0)
    stamp_buy_pct = _charges_rate("STAMP_DUTY_BUY_PCT", 0.003)

    exchange = get_exchange_for_underlying(get_underlying_for_symbol(symbol))
    txn_key, txn_default = TXN_CHARGE_KEYS_BY_EXCHANGE.get(
        exchange, ("TXN_CHARGES_NSE_PCT", 0.03553)
    )
    txn_pct = _charges_rate(txn_key, txn_default)

    stt = turnover * stt_sell_pct / 100.0 if transaction_type == "SELL" else 0.0
    transaction_charges = turnover * txn_pct / 100.0
    sebi_charges = turnover * sebi_per_crore / 1e7
    gst = (brokerage + sebi_charges + transaction_charges) * gst_pct / 100.0
    stamp_duty = turnover * stamp_buy_pct / 100.0 if transaction_type == "BUY" else 0.0

    other_charges = stt + transaction_charges + sebi_charges + gst + stamp_duty
    return round(brokerage, 2), round(other_charges, 2)


def apply_trade_charges(row):
    """
    Attach Brokerage / Other Charges to a formatted orders row using the
    row's own TRAN / Qty. / RATE / CONT values.
    """
    try:
        quantity = float(str(row.get("Qty.", "")).split("/")[0] or 0)
    except (TypeError, ValueError):
        quantity = 0.0
    try:
        price = float(row.get("RATE") or 0)
    except (TypeError, ValueError):
        price = 0.0

    brokerage, other_charges = compute_option_trade_charges(
        row.get("TRAN"), quantity * price, row.get("CONT")
    )
    row["Brokerage"] = brokerage
    row["Other Charges"] = other_charges
    return row


EXPORT_COLUMNS = [
    "ORDERDATE", "ORDERTIME", "TRAN", "CONT", "Product",
    "Qty.", "RATE", "STATUS", "Duration", "PurValue",
    "PnL_Rate", "PnL", "PnL%", "PnL_RT",
    "Brokerage", "Other Charges",
]


def write_orders_workbook(output_rows, pine_script, filename=None):
    """Write the standard formatted trade workbook used by every importer."""
    output_path = filename or tempfile.mktemp(suffix=".xlsx")
    orders_df = pd.DataFrame(output_rows, columns=EXPORT_COLUMNS)

    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        orders_df.to_excel(writer, index=False, sheet_name="Orders")
        workbook = writer.book
        worksheet = writer.sheets["Orders"]
        header_format = workbook.add_format({
            "bold": True, "bg_color": "#D9EAD3", "border": 1,
            "align": "center", "valign": "vcenter",
        })
        for column_index, column_name in enumerate(EXPORT_COLUMNS):
            worksheet.write(0, column_index, column_name, header_format)

        pnl_formats = {
            "dark_green": workbook.add_format({"bg_color": "#00B050", "font_color": "#FFFFFF"}),
            "light_green": workbook.add_format({"bg_color": "#92D050"}),
            "pale_green": workbook.add_format({"bg_color": "#C6EFCE"}),
            "dark_red": workbook.add_format({"bg_color": "#FF0000", "font_color": "#FFFFFF"}),
            "light_red": workbook.add_format({"bg_color": "#FF6666"}),
            "pale_red": workbook.add_format({"bg_color": "#FFC7CE"}),
        }
        pnl_column = EXPORT_COLUMNS.index("PnL%")
        for row_index, row in orders_df.iterrows():
            value = row["PnL%"]
            if value == "" or pd.isna(value):
                continue
            if value > 10:
                cell_format = pnl_formats["dark_green"]
            elif value > 5:
                cell_format = pnl_formats["light_green"]
            elif value > 0:
                cell_format = pnl_formats["pale_green"]
            elif value < -10:
                cell_format = pnl_formats["dark_red"]
            elif value < -5:
                cell_format = pnl_formats["light_red"]
            elif value < 0:
                cell_format = pnl_formats["pale_red"]
            else:
                continue
            worksheet.write(row_index + 1, pnl_column, value, cell_format)

        pine_start = len(output_rows) + 2
        pine_end = pine_start + 12
        pine_label_format = workbook.add_format({
            "bold": True, "bg_color": "#D9EAD3", "border": 1,
            "align": "center", "valign": "vcenter",
        })
        pine_script_format = workbook.add_format({
            "text_wrap": True, "valign": "top", "border": 1,
            "font_name": "Consolas", "font_size": 9,
        })
        worksheet.merge_range(pine_start, 0, pine_end, 0, "PINE", pine_label_format)
        worksheet.merge_range(pine_start, 1, pine_end, 15, pine_script, pine_script_format)
        worksheet.set_column(0, 0, 14)
        worksheet.set_column(1, 15, 16)
        worksheet.set_column(13, 13, 18)
        rate_format = workbook.add_format({"num_format": "0.00"})
        rate_column = EXPORT_COLUMNS.index("RATE")
        worksheet.set_column(rate_column, rate_column, 12, rate_format)
        charges_column = EXPORT_COLUMNS.index("Brokerage")
        worksheet.set_column(charges_column, charges_column + 1, 12, rate_format)
        # VBA parity: PnL and PnL_RT use Indian-style grouping.
        pnl_number_format = workbook.add_format({"num_format": "#,##,##0"})
        for column_name in ("PnL", "PnL_RT"):
            column_index = EXPORT_COLUMNS.index(column_name)
            worksheet.set_column(column_index, column_index, None, pnl_number_format)
        worksheet.set_row(pine_start, 30)

    return output_path


def build_orders_export(order_list):
    """Normalize broker orders, calculate FIFO P&L, and build PineScript data."""
    df = pd.DataFrame(order_list)
    if "status" not in df.columns and "order_status" in df.columns:
        df["status"] = df["order_status"]
    if "timestamp" not in df.columns and "order_timestamp" in df.columns:
        df["timestamp"] = df["order_timestamp"]
    if "transaction_type" not in df.columns and "transactiontype" in df.columns:
        df["transaction_type"] = df["transactiontype"]
    if "average_price" not in df.columns and "averageprice" in df.columns:
        df["average_price"] = df["averageprice"]

    required = ["timestamp", "tradingsymbol", "transaction_type", "quantity", "average_price"]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"Missing order columns: {', '.join(missing)}")

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", dayfirst=True, format="mixed")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["average_price"] = pd.to_numeric(df["average_price"], errors="coerce")
    df["transaction_type"] = df["transaction_type"].astype(str).str.upper()
    df = df.dropna(subset=["timestamp", "quantity", "average_price"]).sort_values("timestamp")

    records = []
    for item in df.to_dict(orient="records"):
        if records and all(records[-1][key] == item[key] for key in ("timestamp", "transaction_type", "tradingsymbol")):
            previous = records[-1]
            total_quantity = previous["quantity"] + item["quantity"]
            previous["average_price"] = round(
                (
                    previous["average_price"] * previous["quantity"]
                    + item["average_price"] * item["quantity"]
                ) / total_quantity,
                2,
            )
            previous["quantity"] = total_quantity
        else:
            records.append(item.copy())

    rows = []
    open_buys = {}
    cumulative_pnl = 0.0
    timeline = []
    zones = []
    pnl_zones = []
    labels = []
    pnl_labels = []
    for sequence, item in enumerate(records, start=1):
        timestamp = item["timestamp"].to_pydatetime()
        symbol = normalize_contract_symbol(item["tradingsymbol"])
        side = item["transaction_type"]
        quantity = float(item["quantity"])
        price = float(item["average_price"])

        # Orders recorded by the playback adapter carry quantity in LOTS
        # plus an explicit lot_size field. Live broker orders carry
        # quantity already in units and no lot_size, so the multiplier
        # stays 1 for them and their PnL scale is unchanged.
        try:
            lot_multiplier = int(float(item.get("lot_size") or 1))
        except (TypeError, ValueError):
            lot_multiplier = 1
        if lot_multiplier <= 0:
            lot_multiplier = 1

        time_var = f"t{timestamp:%y%m%d}_{sequence}"
        # VBA parity: bare ints - zero-padded values (09) would become
        # leading-zero numeric literals, which Pine rejects.
        timeline.append(
            f"{time_var}=timestamp('Asia/Kolkata',{timestamp.year},{timestamp.month},{timestamp.day},{timestamp.hour},{timestamp.minute},{timestamp.second})"
        )
        duration = ""
        purchase_value = ""
        pnl_rate = ""
        pnl = ""
        pnl_pct = ""
        pnl_rt = ""

        if side == "BUY":
            open_buys.setdefault(symbol, []).append({"quantity": quantity, "price": price, "time": timestamp, "var": time_var})
        elif side == "SELL":
            remaining = quantity
            matched = []
            while remaining > 0 and open_buys.get(symbol):
                buy = open_buys[symbol][0]
                taken = min(remaining, buy["quantity"])
                matched.append((buy, taken))
                buy["quantity"] -= taken
                remaining -= taken
                if buy["quantity"] <= 0.0001:
                    open_buys[symbol].pop(0)
            matched_quantity = sum(taken for _, taken in matched)
            if matched_quantity:
                average_buy = sum(buy["price"] * taken for buy, taken in matched) / matched_quantity
                units = matched_quantity * lot_multiplier
                # VBA parity: pnlRate is rounded to 2 decimals BEFORE the
                # pnl multiplication; pct uses the ROUNDED pnl and PnL_RT
                # accumulates the rounded pnl.
                pnl_rate_value = round(price - average_buy, 2)
                purchase_amount = average_buy * units
                pnl = round(pnl_rate_value * units)
                cumulative_pnl += pnl
                elapsed = max(0, int((timestamp - matched[0][0]["time"]).total_seconds()))
                duration = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
                purchase_value = format_lakhs(purchase_amount)
                pnl_rate = pnl_rate_value
                pnl_pct = round((pnl / purchase_amount) * 100, 2) if purchase_amount else 0
                pnl_rt = cumulative_pnl

                mid_y = "(high+low)/2"
                bottom_color = "color.new(color.green,0)" if pnl >= 0 else "color.new(color.red,0)"
                for match_number, (buy, taken) in enumerate(matched, start=1):
                    # VBA computes duration/pnl/pct against THE matched
                    # buy; with one sell filled across several buys each
                    # zone label carries its own match's values.
                    pair_units = taken * lot_multiplier
                    pair_purchase = buy["price"] * pair_units
                    pair_pnl = round((price - buy["price"]) * pair_units)
                    pair_pct = round((pair_pnl / pair_purchase) * 100, 2) if pair_purchase else 0
                    pair_seconds = max(0, int((timestamp - buy["time"]).total_seconds()))
                    pair_duration = f"{pair_seconds // 60:02d}:{pair_seconds % 60:02d}"
                    # VBA names the boxes/labels after the sell's timeline
                    # var (bz_t260918_5); the _N suffix only appears when
                    # one sell was filled across several buys.
                    pine_id = (
                        f"t{timestamp:%y%m%d}_{sequence}"
                        if match_number == 1
                        else f"t{timestamp:%y%m%d}_{sequence}_{match_number}"
                    )
                    color = "color.new(color.green,0)" if symbol.endswith("CE") else "color.new(color.red,0)"
                    mid_time = f"({buy['var']}+{time_var})/2"
                    zones.append(f"if barstate.islast\n    var bz_{pine_id}=box.new({buy['var']},high,{time_var},{mid_y},xloc=xloc.bar_time,bgcolor={color},border_width=0)")
                    pnl_zones.append(f"if barstate.islast\n    var pbz_{pine_id}=box.new({buy['var']},{mid_y},{time_var},low,xloc=xloc.bar_time,bgcolor={bottom_color},border_width=0)")
                    labels.append(f"if barstate.islast\n    var l_{pine_id}=label.new({mid_time},(high+{mid_y})/2,text='{pair_units:g}  {pair_duration}  {round(price - buy['price'], 1):.1f}',xloc=xloc.bar_time,style=label.style_label_center,color=color.new(color.black,15),textcolor=color.white,size=size.normal,textalign=text.align_center,text_font_family=font.family_monospace)")
                    pnl_labels.append(f"if barstate.islast\n    var pl_{pine_id}=label.new({mid_time},(low+{mid_y})/2,text='{format_lakhs(pair_purchase)}  {pair_pnl:,.0f}  {pair_pct:.2f}%',xloc=xloc.bar_time,style=label.style_label_center,color=color.new(color.black,15),textcolor=color.white,size=size.normal,textalign=text.align_center,text_font_family=font.family_monospace)")

        rows.append(apply_trade_charges({
            "ORDERDATE": timestamp.strftime("%m/%d/%Y"), "ORDERTIME": timestamp.strftime("%H:%M:%S"),
            "TRAN": side, "CONT": symbol, "Product": "MIS", "Qty.": int(quantity * lot_multiplier),
            "RATE": price, "STATUS": str(item.get("status", "COMPLETE")), "Duration": duration,
            "PurValue": purchase_value, "PnL_Rate": pnl_rate, "PnL": pnl, "PnL%": pnl_pct, "PnL_RT": pnl_rt,
        }))

    # Section order and headers mirror the VBA final-output cell:
    # TIMELINE, ORDER ZONES, ORDER LABELS, PNL ZONES, PNL LABELS.
    pine_script = "\n".join([
        "// === TIMELINE ===", *timeline,
        "// === ORDER ZONES ===", *zones,
        "// === ORDER LABELS ===", *labels,
        "// === PNL ZONES ===", *pnl_zones,
        "// === PNL LABELS ===", *pnl_labels,
    ])
    return rows, pine_script


def save_orders_to_xlsx(order_list, filename=None):
    rows, pine_script = build_orders_export(order_list)
    return write_orders_workbook(rows, pine_script, filename)

