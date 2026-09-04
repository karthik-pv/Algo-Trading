import struct
import logging
from pprint import pprint
from loguru import logger
import pandas as pd
from datetime import timedelta


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
                "lotsize" : position.get("lotsize")
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
                "order_status": order.get("orderstatus") or order.get("status"),
                "order_id": (order.get("orderid"))
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


def make_timestamp_expr(self,ts):
    return f"timestamp('GMT',{ts.year},{ts.month},{ts.day},{ts.hour},{ts.minute},{ts.second})"


def save_orders_to_csv(order_list, filename="mstock_orders.csv"):
    """
    Converts order_list -> DataFrame, filters traded orders,
    computes PL values, generates PineScript helper columns,
    and saves CSV.
    """
    try:
        logger.info("Saving orders to CSV file...")
        df = pd.DataFrame(order_list)
        #logger.debug(f"DataFrame content:\n{df.to_string()}")
        # logger.debug(f"DataFrame content (few top records):\n{df.head().to_string()}")
        #logger.debug(df.to_json(orient="records", indent=2))
        
        cols = [
            "timestamp", "tradingsymbol", "transaction_type",
            "quantity", "average_price", "status", "order_id"
        ]
        df = df[[c for c in cols if c in df.columns]].copy()

        # Normalize timestamp
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.sort_values("timestamp").reset_index(drop=True)

        #df["SeqNo"] = df.index + 1
        df["SeqNo"] = df["timestamp"].dt.strftime("%y%m%d") + "_" + (df.index + 1).astype(str)
        df["transaction_type"] = df["transaction_type"].astype(str).str.upper()

        df["average_price"] = pd.to_numeric(df["average_price"], errors="coerce")
        df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")

        # Compute PL
        df["PL_RATE"] = None
        df["PL_AMOUNT"] = None

        open_pos = {}

        for i, r in df.iterrows():
            sym = r["tradingsymbol"]
            qty = r["quantity"]
            price = r["average_price"]
            side = r["transaction_type"]

            if sym not in open_pos:
                open_pos[sym] = []

            if side == "BUY":
                open_pos[sym].append([qty, price])

            elif side == "SELL":
                sell_qty = qty
                cost = 0
                used_qty = 0

                while sell_qty > 0 and open_pos[sym]:
                    b_qty, b_price = open_pos[sym][0]
                    take = min(b_qty, sell_qty)

                    cost += take * b_price
                    used_qty += take

                    b_qty -= take
                    sell_qty -= take

                    if b_qty == 0:
                        open_pos[sym].pop(0)
                    else:
                        open_pos[sym][0][0] = b_qty

                if used_qty > 0:
                    avg_buy = cost / used_qty
                    pl_rate = price - avg_buy
                    df.at[i, "PL_RATE"] = round(pl_rate, 1)
                    df.at[i, "PL_AMOUNT"] = round(pl_rate * used_qty)


                df["P&L Time"] = df["timestamp"].diff()
                df["P&L Time"] = df["P&L Time"].where(df["transaction_type"] == "SELL")
                df["P&L Time"] = df["P&L Time"].apply(
                    lambda x: f"{int(x.total_seconds()//60):02d}:{int(x.total_seconds()%60):02d}" if pd.notnull(x) else ""
                )

                # GMT timestamp
                df["timestamp_gmt"] = df["timestamp"] - timedelta(minutes=330)
                df["suffix"] = df["tradingsymbol"].str[-2:].str.upper()

                df["suffix"] = (df["tradingsymbol"].astype(str).str.split("-").str[-2].str.upper())

                def make_t_line(r):
                    ts = r["timestamp_gmt"]
                    if pd.isnull(ts):
                        return f"t{r['SeqNo']}=timestamp('GMT',1970,1,1,0,0,0)"
                    return (f"t{r['SeqNo']}=timestamp('GMT',{ts.year},{ts.month},{ts.day},"
                            f"{ts.hour},{ts.minute},{ts.second})")

                df["Order_Time_Line"] = df.apply(make_t_line, axis=1)


        # Long_Short_Line: only for SELL rows, color CE->green, PE->red else gray
        def long_short_color(r):
            if r["transaction_type"] != "SELL":
                return ""
            suf = str(r.get("suffix", "")).upper()
            if suf == "CE":
                c = "green"
            elif suf == "PE":
                c = "red"
            else:
                c = "gray"
            return f"line.new(t{r['SeqNo']},low,t{r['SeqNo']},high,xloc.bar_time,extend.both,color.{c},line.style_solid,1)"

        df["Long_Short_Line"] = df.apply(long_short_color, axis=1)

        # Order_Line: exact Access logic:
        # CE+BUY -> green, CE+SELL -> red, PE+BUY -> yellow, PE+SELL -> blue, else gray
        def order_line(r):
            tran = str(r.get("transaction_type", "")).upper()
            suf = str(r.get("suffix", "")).upper()
            if suf == "CE" and tran == "BUY":
                color = "green"
            elif suf == "CE" and tran == "SELL":
                color = "red"
            elif suf == "PE" and tran == "BUY":
                color = "yellow"
            elif suf == "PE" and tran == "SELL":
                color = "blue"
            else:
                color = "gray"
            width = 1 if tran == "SELL" else 2
            return (f"line.new(t{r['SeqNo']},low,t{r['SeqNo']},high,"
                    f"xloc.bar_time,extend.both,color.{color},line.style_solid,{width})")

        df["Order_Line"] = df.apply(order_line, axis=1)

        # PL_Line: only SELL rows; color green if PL_RATE>0 else red
        def pl_line(r):
            if r["transaction_type"] != "SELL":
                return ""
            pl = r.get("PL_RATE")
            color = "green" if (pd.notnull(pl) and pl > 0) else "red"
            return (f"line.new(t{r['SeqNo']},low,t{r['SeqNo']},high,"
                    f"xloc.bar_time,extend.both,color.{color},line.style_solid,2)")

        df["PL_Line"] = df.apply(pl_line, axis=1)

        # PL_Callout: only SELL rows; show PL_RATE (rounded) and color same as PL_Line
        def pl_callout(r):
            if r["transaction_type"] != "SELL":
                return ""
            pl = r.get("PL_RATE")
            if pd.isnull(pl):
                return ""
            color = "green" if pl > 0 else "red"
            # show numeric with one decimal (matches PL_RATE)
            return (f"label.new(t{r['SeqNo']}, high * 1.02, text=str.tostring({pl}), "
                    f"xloc=xloc.bar_time, style=label.style_label_center, color=color.{color}, "
                    f"textcolor=color.white, size=size.small)")

        df["PL_Callout"] = df.apply(pl_callout, axis=1)

        def pl_amt(r):
            # Access had this only on BUY rows as plot(time==tN ? PL_RATE : na ...)
            if r["transaction_type"] != "BUY":
                return ""
            pl = r.get("PL_RATE")
            color = "green" if (pd.notnull(pl) and pl > 0) else "red"
            # Using PL_RATE even though it's typically empty on BUY rows; kept for parity with Access
            return f"plot(time==t{r['SeqNo']}?{pl} : na, style=plot.style_circles, color=color.{color},linewidth=3)"

        df["PL_Amt"] = df.apply(pl_amt, axis=1)

        # Save file
        df.to_csv(filename, index=False)
        logger.info(f"✔ File saved: {filename}")

    except Exception:
        logger.exception("Error saving orders to CSV")
        raise