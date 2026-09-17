# Paper Trading (MODE=PAPER) Fix Plan — approved P0+P1+P2 incl. Kite MCX fix

Repo: `C:\Users\phvra\Desktop\Algo-Trading - 1.8`
Scope approved by user: all fixes, INCLUDING the Kite MCX live-path change.
Constraint: LIVE MSTOCK-NIFTY (and all LIVE paths except Kite MCX quantity semantics) must be untouched.

Affected files: `core/trade_logic.py`, `server.py`, `adapter/kite_adapter.py`
NOT touched: `core/mstock_connector.py`, `adapter/mstock_adapter.py`, all LIVE branches in trade_logic.py.

---

## A. core/trade_logic.py

### A1. Class attributes — insert after line 95 (`    _voice_announcement_enabled = True`)

```python
    _paper_trade_lock = threading.Lock()

    # Standard PaperTrading.txt columns. Shared by write_paper_trade()
    # (writer) and fetch_paper_trades() (reader) so the two can never
    # drift apart again.
    _PAPER_TRADE_HEADERS = [
        "Time", "Type", "Instrument", "Token", "Product",
        "Qty.", "Avg. price", "Status", "Duration",
        "PnL Rate", "PnL %", "PnL"
    ]
```

### A2. `_calculate_option_lots` — replace PAPER branch (lines 165-168)

OLD:
```python
        # PAPER mode always sizes from the configured paper trading
        # cash balance, independent of the live broker's funds.
        if self._mode == "PAPER":
            return int(((self._CASH_BALANCE_PAPER_TRADING * margin_pct) / price) / safe_lot_size)
```

NEW (depletes paper cash by open paper positions so sizing is realistic):
```python
        # PAPER mode always sizes from the configured paper trading
        # cash balance, independent of the live broker's funds. Cash
        # already committed to open paper positions is subtracted so
        # the paper account cannot over-allocate.
        if self._mode == "PAPER":
            invested = 0.0
            for pos in self._position_data.values():
                try:
                    invested += (
                        float(pos.get("average_price", 0) or 0)
                        * int(pos.get("net_quantity", 0) or 0)
                        * int(pos.get("lotsize", 1) or 1)
                    )
                except (TypeError, ValueError):
                    continue
            available = self._CASH_BALANCE_PAPER_TRADING - invested
            if available <= 0:
                return 0
            return int(((available * margin_pct) / price) / safe_lot_size)
```

LIVE branch below (cash_balance path) unchanged.

### A3. Watcher `stop_loss_book_profit_core` — replace PAPER else-branch (lines 1215-1242)

OLD (note: final line is `                        )` followed by exactly 20 trailing spaces):
```python
                else:
                    if should_log_eval:
                        logger.debug(f"PAPER: Sell conditions met for {tradingsymbol} at LTP {ltp} - Decision To Sell: {sell}")

                    filename = "PaperTrading.txt"

                    # Create header if file doesn't exist
                    if not os.path.exists(filename):
                        with open(filename, "w", encoding="utf-8") as f:
                            f.write(
                                "Time\tType\tInstrument\tToken\tProduct\tQty.\tAvg. price\tStatus\n"
                            )

                    timestamp = datetime.now().strftime("%-m/%-d/%Y %H:%M")
                    # On Windows use:
                    # timestamp = datetime.now().strftime("%#m/%#d/%Y %H:%M")

                    with open(filename, "a", encoding="utf-8") as f:
                        f.write(
                            f"{timestamp}\t"
                            f"SELL\t"
                            f"{tradingsymbol}\t"
                            f"{instrument_token}\t"
                            f"MIS\t"
                            f"{qty}/{qty}\t"
                            f"{ltp}\t"
                            f"COMPLETE\n"
                        )<20 trailing spaces>
```

NEW:
```python
                else:
                    # PAPER mode: mirror the LIVE branch - only record the
                    # SELL when the decision maker actually says sell, then
                    # rebuild the paper position grid. A failure here must
                    # not kill the watcher thread.
                    if not sell:
                        if should_log_eval:
                            logger.debug(f"PAPER: No SELL action taken for {tradingsymbol} at LTP {ltp}")
                    else:
                        try:
                            logger.info(f"PAPER: Recording paper SELL for {tradingsymbol} {instrument_token} : {qty} lots at LTP {ltp}")

                            self.write_paper_trade(
                                transaction_type="SELL",
                                tradingsymbol=tradingsymbol,
                                token=instrument_token,
                                qty=qty,
                                ltp=ltp,
                                product="MIS",
                                buy_price=buy_price,
                                lot_size=data.get("lotsize", 1),
                                buy_time=self.get_open_position_buy_time(
                                    instrument_token,
                                    tradingsymbol=tradingsymbol
                                )
                            )

                            # Recalculate paper positions so the sold
                            # quantity disappears from the grid.
                            self.refresh_paper_positions()
                        except Exception as paper_sell_error:
                            logger.error(
                                f"PAPER auto-sell failed for {tradingsymbol}: {paper_sell_error}",
                                exc_info=True
                            )
```

Fixes: unconditional SELL write (no `if sell`), `datetime.now()` AttributeError (thread death), `%-m` Windows ValueError, 8-col inline writer format mismatch, missing position refresh. LIVE branch (lines 1208-1214) untouched.

### A4. Delete dead `stop_loss_core` (lines 1246-1317 plus the two blank lines 1318-1319)

Zero references confirmed (only `stop_loss_book_profit_core` is launched from `start_trading_watcher_thread` line 2756). Contains NameError (`write_paper_trade` nested-scope call), TypeError (`qty["lots"]` on int), and a mode check that treats PLAYBACK as paper. Replace whole block with nothing, leaving line 1245 (`    ` + 4 spaces) before `    # ---------------------- DECISION MAKER ----------------------------`.

### A5. `on_start` — mode guard (lines 2787-2789)

OLD:
```python
    def on_start(self):
        logger.info("Trader on_start initialization...")
        self.refresh_open_pos_buy_price()
```
(line 2791 is `    ` + 4 spaces — keep)

NEW:
```python
    def on_start(self):
        logger.info("Trader on_start initialization...")
        if self._mode == "PAPER":
            # PAPER mode: the position grid comes from the paper trade
            # log, NOT from live broker REST calls. refresh_open_pos_
            # buy_price() would overwrite/clear paper positions.
            self.refresh_paper_positions()
        else:
            self.refresh_open_pos_buy_price()
```

### A6. `get_open_position_buy_time` — replace whole method (lines 2861-2891)

NEW (symbol-first matching, robust across broker/exchange token-space switches):
```python
    def get_open_position_buy_time(self, token, tradingsymbol=None):
        """
        Find the latest BUY time for a position from the paper trade log.

        Matches by tradingsymbol first (robust when the position token
        was re-bound to the active broker's token space), falling back
        to the token recorded in the trade file.
        """
        trades = self.fetch_paper_trades()

        token = str(token)
        symbol = str(tradingsymbol).strip().upper() if tradingsymbol else None

        def _latest_buy_time(match_symbol=None, match_token=None):
            for trade in reversed(trades):
                if match_symbol is not None:
                    if str(trade.get("Instrument", "")).strip().upper() != match_symbol:
                        continue
                if match_token is not None:
                    if str(trade.get("Token")) != match_token:
                        continue
                if trade.get("Type", "").upper() != "BUY":
                    continue
                buy_time = trade.get("Time")
                if buy_time:
                    return buy_time
            return None

        buy_time = None
        if symbol:
            buy_time = _latest_buy_time(match_symbol=symbol)
        if buy_time is None:
            buy_time = _latest_buy_time(match_token=token)

        if buy_time:
            logger.debug(f"Latest BUY time for {symbol or token}: {buy_time}")
            return buy_time

        logger.warning(f"No BUY time found for {symbol or token}")
        return None
```

### A7. Insert NEW class method `write_paper_trade` immediately BEFORE `def fetch_paper_trades` (line 2894)

Single shared writer for watcher + buy handler + sell handler, with:
- self-healing header (prepends the 12-col header when the file exists without one — recovers the current on-disk file),
- `buy_time=None` no longer crashes (duration skipped, PnL still computed),
- platform-independent timestamp (`m/d/Y H:M:S` built manually — no `%-m`/`%#m`),
- lock so header repair + append are atomic vs. the watcher thread.

```python
    def write_paper_trade(
        self,
        transaction_type,
        tradingsymbol,
        token,
        qty,
        ltp,
        product="MIS",
        buy_price=None,
        lot_size=1,
        buy_time=None
    ):
        """
        Append one row to PaperTrading.txt.

        Shared by the buy/sell socket handlers and the auto-sell watcher
        so every writer produces the identical 12-column format that
        fetch_paper_trades() parses. Repairs a missing header line so
        legacy files become readable again.
        """

        filename = "PaperTrading.txt"

        # ---------------------------------------------------------
        # Fixed column widths
        # ---------------------------------------------------------

        WIDTH_TIME       = 20
        WIDTH_TYPE       = 8
        WIDTH_INSTRUMENT = 24
        WIDTH_TOKEN      = 12
        WIDTH_PRODUCT    = 10
        WIDTH_QTY        = 10
        WIDTH_PRICE      = 12
        WIDTH_STATUS     = 12
        WIDTH_DURATION   = 12
        WIDTH_PNL_RATE   = 12
        WIDTH_PNL_PCT    = 12
        WIDTH_PNL        = 16

        headers = [
            "Time".ljust(WIDTH_TIME),
            "Type".ljust(WIDTH_TYPE),
            "Instrument".ljust(WIDTH_INSTRUMENT),
            "Token".ljust(WIDTH_TOKEN),
            "Product".ljust(WIDTH_PRODUCT),
            "Qty.".ljust(WIDTH_QTY),
            "Avg. price".ljust(WIDTH_PRICE),
            "Status".ljust(WIDTH_STATUS),
            "Duration".rjust(WIDTH_DURATION),
            "PnL Rate".rjust(WIDTH_PNL_RATE),
            "PnL %".rjust(WIDTH_PNL_PCT),
            "PnL".rjust(WIDTH_PNL)
        ]

        header_line = "\t".join(headers)

        # ---------------------------------------------------------
        # Default PnL values (BUY rows keep these blank)
        # ---------------------------------------------------------

        pnl_rate = ""
        pnl = ""
        pnl_pct = ""
        duration = ""

        # ---------------------------------------------------------
        # Calculate PnL ONLY for SELL. Duration is optional: a missing
        # or unparsable buy_time must not fail the SELL record.
        # ---------------------------------------------------------

        if transaction_type.upper() == "SELL" and buy_price is not None:

            sell_time = datetime.datetime.now()

            parsed_buy_time = None
            if isinstance(buy_time, datetime.datetime):
                parsed_buy_time = buy_time
            elif isinstance(buy_time, str) and buy_time.strip():
                try:
                    parsed_buy_time = datetime.datetime.strptime(
                        buy_time.strip(),
                        "%m/%d/%Y %H:%M:%S"
                    )
                except ValueError:
                    logger.warning(
                        f"Unparsable buy_time '{buy_time}' for "
                        f"{tradingsymbol}; duration will be blank"
                    )

            if parsed_buy_time is not None:
                elapsed = sell_time - parsed_buy_time
                total_seconds = int(elapsed.total_seconds())

                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                seconds = total_seconds % 60

                if hours > 0:
                    duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                else:
                    duration = f"{minutes:02d}:{seconds:02d}"

            buy_price = float(buy_price)
            sell_price = float(ltp)
            quantity = int(qty) * int(lot_size)

            pnl_rate_value = sell_price - buy_price
            pnl_value = pnl_rate_value * quantity
            pnl_pct_value = (
                (pnl_rate_value * 100) / buy_price
                if buy_price != 0
                else 0
            )

            pnl_rate = f"{pnl_rate_value:.2f}"
            pnl = f"{pnl_value:,.0f}"
            pnl_pct = f"{pnl_pct_value:.2f}"

        # ---------------------------------------------------------
        # Timestamp (platform independent: no %-m / %#m directives)
        # ---------------------------------------------------------

        now = datetime.datetime.now()
        timestamp = f"{now.month}/{now.day}/{now.year} {now:%H:%M:%S}"

        # ---------------------------------------------------------
        # Prepare row
        # ---------------------------------------------------------

        values = [
            timestamp.ljust(WIDTH_TIME),
            transaction_type.ljust(WIDTH_TYPE),
            tradingsymbol.ljust(WIDTH_INSTRUMENT),
            str(token).ljust(WIDTH_TOKEN),
            product.ljust(WIDTH_PRODUCT),
            f"{qty}/{qty}".ljust(WIDTH_QTY),
            f"{float(ltp):.2f}".rjust(WIDTH_PRICE),
            "COMPLETE".ljust(WIDTH_STATUS),
            duration.rjust(WIDTH_DURATION),
            pnl_rate.rjust(WIDTH_PNL_RATE),
            pnl_pct.rjust(WIDTH_PNL_PCT),
            pnl.rjust(WIDTH_PNL)
        ]

        # ---------------------------------------------------------
        # Write transaction. The header check + repair + append run
        # under a lock so the watcher thread and socket handlers can
        # never interleave a repair with a row append.
        # ---------------------------------------------------------

        with self._paper_trade_lock:

            needs_header = True
            if os.path.exists(filename):
                try:
                    with open(filename, "r", encoding="utf-8") as f:
                        first_line = f.readline()
                    if first_line.split("\t")[0].strip() == "Time":
                        needs_header = False
                except Exception as e:
                    logger.warning(f"Could not inspect {filename}: {e}")

            if needs_header:
                existing = ""
                if os.path.exists(filename):
                    try:
                        with open(filename, "r", encoding="utf-8") as f:
                            existing = f.read()
                    except Exception as e:
                        logger.warning(
                            f"Could not read {filename} for header repair: {e}"
                        )

                with open(filename, "w", encoding="utf-8") as f:
                    f.write(header_line + "\n")
                    if existing:
                        f.write(existing)

                logger.info(
                    "PaperTrading.txt header written/repaired"
                )

            with open(filename, "a", encoding="utf-8") as f:
                f.write("\t".join(values) + "\n")
```

### A8. `fetch_paper_trades` — replace header block (lines 2911-2926)

OLD:
```python
            # ---------------------------------------------------------
            # Read header
            # ---------------------------------------------------------

            headers = [
                h.strip()
                for h in lines[0].rstrip("\n").split("\t")
            ]

            logger.debug(f"PaperTrading headers: {headers}")

            # ---------------------------------------------------------
            # Read transactions
            # ---------------------------------------------------------

            for line_number, line in enumerate(lines[1:], start=2):
```

NEW:
```python
            # ---------------------------------------------------------
            # Read header.
            #
            # Legacy files were written without a header line (the
            # first line is a DATA row). In that case fall back to the
            # standard header and parse every line as data.
            # ---------------------------------------------------------

            first_fields = lines[0].rstrip("\n").split("\t")
            header_is_present = first_fields[0].strip() == "Time"

            if header_is_present:
                headers = [h.strip() for h in first_fields]
                data_lines = lines[1:]
            else:
                headers = list(self._PAPER_TRADE_HEADERS)
                data_lines = lines
                logger.warning(
                    "PaperTrading.txt has no header line; "
                    "using default header for all rows"
                )

            logger.debug(f"PaperTrading headers: {headers}")

            # ---------------------------------------------------------
            # Read transactions
            # ---------------------------------------------------------

            for line_number, line in enumerate(
                data_lines,
                start=2 if header_is_present else 1
            ):
```

Then, immediately after `values = line.split("\t")` (current line 2933), insert legacy-row padding BEFORE the column-count check:
```python
                # Legacy 8-column rows (old inline writers): pad the
                # Duration/PnL columns so they still parse.
                if len(values) == 8 and len(headers) == len(self._PAPER_TRADE_HEADERS):
                    values = values + ["", "", "", ""]
```

### A9. `calculate_paper_positions` — replace whole method (lines 3021-3065)

Aggregation key changes from token to tradingsymbol so BUY/SELL rows recorded under different broker token spaces still net correctly.

```python
    def calculate_paper_positions(self):
        """
        Aggregate paper trades into net positions keyed by tradingsymbol.

        Keying by symbol (not token) keeps accounting correct when the
        same instrument was recorded under different broker/exchange
        token spaces (e.g. after switching BROKER or UNDERLYING).
        """

        trades = self.fetch_paper_trades()

        positions = {}

        for trade in trades:

            try:
                symbol = str(trade["Instrument"]).strip()
                token = str(trade["Token"]).strip()

                qty = int(str(trade["Qty."]).split("/")[0])
                price = float(trade["Avg. price"])
                transaction_type = trade["Type"].upper()

            except Exception as e:
                logger.warning(
                    f"Skipping invalid paper trade {trade}: {e}"
                )
                continue

            if not symbol:
                continue

            if symbol not in positions:
                positions[symbol] = {
                    "tradingsymbol": symbol,
                    "instrument_token": token,
                    "buy_quantity": 0,
                    "sell_quantity": 0,
                    "buy_value": 0.0,
                    "sell_value": 0.0,
                }

            position = positions[symbol]

            if transaction_type == "BUY":

                position["buy_quantity"] += qty
                position["buy_value"] += qty * price

            elif transaction_type == "SELL":

                position["sell_quantity"] += qty
                position["sell_value"] += qty * price

        return positions
```

### A10. `refresh_paper_positions` — replace loop body (lines 3084-3164)

OLD: iterates `for token, position in paper_positions.items():`, uses file token as key, `total_pl = pts_pl * net_quantity` (missing lotsize), silent lotsize=1 fallback.

NEW:
```python
        for symbol, position in paper_positions.items():

            net_quantity = (
                position["buy_quantity"]
                - position["sell_quantity"]
            )

            logger.debug(
                f"Symbol={symbol} "
                f"BUY={position['buy_quantity']} "
                f"SELL={position['sell_quantity']} "
                f"NET={net_quantity}"
            )

            if net_quantity <= 0:
                continue

            average_buy_price = (
                position["buy_value"]
                / position["buy_quantity"]
            )

            # ---------------------------------------------------------
            # Resolve the instrument through the ACTIVE broker master:
            #   1. correct lot size for PnL math
            #   2. re-bind the position to the active broker's token
            #      space so websocket ticks keep updating positions
            #      recorded under a different broker/exchange earlier.
            # Fall back to the token recorded in the trade file when
            # the instrument is not in today's master.
            # ---------------------------------------------------------

            lotsize = 1
            exchange = self._exchange
            active_token = str(position["instrument_token"])

            instrument_data = None
            try:
                instrument_data = self._broker.get_instrument_details(
                    symbol
                )
            except Exception as e:
                logger.warning(
                    f"Could not get instrument details for "
                    f"{symbol}: {e}"
                )

            if instrument_data:
                try:
                    lotsize = int(
                        instrument_data.get("lot_size", 1) or 1
                    )
                    resolved_token = str(
                        instrument_data.get("instrument_token") or ""
                    ).strip()
                    if resolved_token:
                        active_token = resolved_token
                    resolved_exchange = str(
                        instrument_data.get("exchange") or ""
                    ).strip()
                    if resolved_exchange:
                        exchange = resolved_exchange
                except Exception as e:
                    logger.warning(
                        f"Instrument detail parsing failed for "
                        f"{symbol}: {e}"
                    )
            else:
                logger.warning(
                    f"Instrument {symbol} not found in the active "
                    f"broker instrument master. Using lotsize=1 and "
                    f"the token recorded in the paper trade file; PnL "
                    f"and lot sizing for this position may be wrong."
                )

            latest_price = self.get_latest_price(active_token)

            if not latest_price:
                latest_price = average_buy_price

            pts_pl = self.calculate_point_difference(
                average_buy_price,
                latest_price
            )

            pct_pl = self.calculate_pctg_difference(
                average_buy_price,
                latest_price
            )

            # net_quantity is in LOTS - multiply by lotsize for units,
            # consistent with position_data_update() and
            # write_paper_trade().
            total_pl = pts_pl * net_quantity * lotsize

            new_position_data[active_token] = {
                "tradingsymbol": position["tradingsymbol"],
                "average_price": average_buy_price,
                "net_quantity": net_quantity,
                "latest_price": latest_price,
                "instrument_token": active_token,
                "exchange": exchange,
                "lotsize": lotsize,
                "lots": net_quantity,
                "pts_pl": pts_pl,
                "pct_pl": pct_pl,
                "total_pl": total_pl,
                "order_strategy":
                    self._order_strategy_mapping.get(
                        active_token,
                        "ULTRA_SCALPING"
                    )
            }
```

Keep the existing tail (3166-3180): `self._position_data = new_position_data`, the two logger calls, and the socket emit (line 3180 ends with 2 trailing spaces — preserve as-is or trim, harmless either way).

### A11. `refresh-options-table` handler — PAPER guard (lines 3284-3288)

OLD:
```python
        @self.frontend_data_socket.on('refresh-options-table')
        def refresh_table():
            logger.info("Refreshing frontend options table")
            self.refresh_open_pos_buy_price()
            self.setup_woc_subscriptions()
```

NEW:
```python
        @self.frontend_data_socket.on('refresh-options-table')
        def refresh_table():
            logger.info("Refreshing frontend options table")
            if self._mode == "PAPER":
                # PAPER mode: rebuild from the paper trade log. The
                # live REST refresh would wipe the paper grid.
                self.refresh_paper_positions()
                self.refresh_subscriptions()
            else:
                self.refresh_open_pos_buy_price()
            self.setup_woc_subscriptions()
```

### A12. `handle_sell_order` PAPER branch — replace lines 3449-3483

OLD (line 3465 ends with `)` + 16 trailing spaces; line 3487 `else:` + 20 trailing spaces — LIVE else branch after it is NOT modified):
```python
                if self._mode not in ("LIVE", "SIMULATION", "PLAYBACK"):

                    buy_price = self._position_data[order_details["token"]]["average_price"]
                    lot_size = self._position_data[order_details["token"]]["lotsize"]
                    buy_time = self.get_open_position_buy_time(order_details["token"])

                    write_paper_trade(
                        transaction_type="SELL",
                        tradingsymbol=order_details["tradingsymbol"],
                        token=order_details["token"],
                        qty=order_details["lots"],
                        ltp=ltp,
                        product="MIS",
                        buy_price=buy_price,
                        lot_size=lot_size,
                        buy_time=buy_time
                    )<16 trailing spaces>
                    logger.info(
                    f"Paper SELL recorded for "
                    f"{order_details['tradingsymbol']} "
                    f"({order_details['lots']} lots) @ {ltp}"
                )

                    # Recalculate paper positions
                    self.refresh_paper_positions()

                    self.frontend_data_socket.emit(
                        'sell_order_result',
                        {
                            "success": True,
                            "tradingsymbol": order_details["tradingsymbol"],
                            "lots": order_details["lots"],
                            "paper": True
                        }
                    )
```

NEW (adds oversell validation; existing outer try/except at 3437/3493 already converts raised exceptions into a `sell_order_result` failure emit):
```python
                if self._mode not in ("LIVE", "SIMULATION", "PLAYBACK"):

                    position = self._position_data.get(order_details["token"])
                    if not position:
                        raise Exception(
                            f"No open paper position for token "
                            f"{order_details['token']}"
                        )

                    held_lots = int(position.get("net_quantity", 0) or 0)
                    sell_lots = int(order_details["lots"])

                    if sell_lots <= 0:
                        raise Exception("Sell lots must be greater than zero")

                    if sell_lots > held_lots:
                        raise Exception(
                            f"Cannot sell {sell_lots} lots of "
                            f"{order_details['tradingsymbol']}; only "
                            f"{held_lots} lots held"
                        )

                    buy_price = position["average_price"]
                    lot_size = position["lotsize"]
                    buy_time = self.get_open_position_buy_time(
                        order_details["token"],
                        tradingsymbol=order_details["tradingsymbol"]
                    )

                    self.write_paper_trade(
                        transaction_type="SELL",
                        tradingsymbol=order_details["tradingsymbol"],
                        token=order_details["token"],
                        qty=sell_lots,
                        ltp=ltp,
                        product="MIS",
                        buy_price=buy_price,
                        lot_size=lot_size,
                        buy_time=buy_time
                    )
                    logger.info(
                        f"Paper SELL recorded for "
                        f"{order_details['tradingsymbol']} "
                        f"({sell_lots} lots) @ {ltp}"
                    )

                    # Recalculate paper positions
                    self.refresh_paper_positions()

                    self.frontend_data_socket.emit(
                        'sell_order_result',
                        {
                            "success": True,
                            "tradingsymbol": order_details["tradingsymbol"],
                            "lots": sell_lots,
                            "paper": True
                        }
                    )
```

Note: the earlier `ltp = self._position_data[order_details["token"]]["latest_price"]` (line 3439) stays; if the token is missing it raises KeyError into the existing except — acceptable. LIVE/SIMULATION else branch (3487-3492) unchanged.

### A13. Remove nested `write_paper_trade` (lines 3534-3687, inside `register_socket_handlers`)

Delete the entire nested `def write_paper_trade(...)` block (from `        def write_paper_trade(` through `                f.write(\n                    "\t".join(values) + "\n"\n                )`), leaving the two blank lines and `        @self.frontend_data_socket.on('place_buy_order')` intact. The class method from A7 replaces it.

### A14. `handle_buy_order` PAPER branch — line 3710

OLD:
```python
                    write_paper_trade(transaction_type="BUY",tradingsymbol=order_details["tradingsymbol"],token=order_details["token"],qty=order_details["lots"],ltp=ltp,product="MIS")
```
NEW:
```python
                    self.write_paper_trade(transaction_type="BUY",tradingsymbol=order_details["tradingsymbol"],token=order_details["token"],qty=order_details["lots"],ltp=ltp,product="MIS")
```
(Line 3719 ends with 20 trailing spaces — leave that line untouched.)

---

## B. server.py

### B1. Import — line 22

OLD:
```python
from utils import is_market_open, fetch_from_json, load_json_with_retry, backup_old_logs, resolve_day_start_cash, check_internet_connectivity
```
NEW:
```python
from utils import is_market_open, fetch_from_json, load_json_with_retry, backup_old_logs, resolve_day_start_cash, check_internet_connectivity, clear_json_cache
```

### B2. `/save_settings` (lines 249-259) — clear the settings cache after writing

After `json.dump(data, f, indent=4)` and before `return jsonify({"status": "success"})` insert:
```python
        # settings.json is cached by fetch_from_json(); clear it so
        # runtime re-reads (e.g. MODE in the order handlers) see the
        # new values instead of the stale import-time cache.
        clear_json_cache(SETTINGS_FILE)
```

### B3. `/update_sell_mode` (lines 262-279) — same after the second `json.dump`

After `json.dump(settings, f, indent=4)` insert:
```python
        clear_json_cache("settings.json")
```

---

## C. adapter/kite_adapter.py  (LIVE behavior change — user approved)

### C1. `buy_units` MCX branch (lines 307-320)

OLD:
```python
            if exchange == "MCX":
                order_type = self.kite.ORDER_TYPE_LIMIT
                price = float(ltp)+float(self._limit_margin)

                order_id = self.kite.place_order(
```
NEW:
```python
            if exchange == "MCX":
                order_type = self.kite.ORDER_TYPE_LIMIT
                price = float(ltp)+float(self._limit_margin)
                # The frontend sends quantity in LOTS; convert to units
                # (same as the NFO/BFO path below) so the live order
                # size matches what paper mode records.
                quantity = int(quantity) * int(lotsize)

                order_id = self.kite.place_order(
```

### C2. `sell_units` MCX branch (lines 409-421)

OLD:
```python
            if exchange == "MCX":
                order_type = self.kite.ORDER_TYPE_LIMIT
                price = float(ltp)-float(self._limit_margin)
                order_id = self.kite.place_order(
```
NEW:
```python
            if exchange == "MCX":
                order_type = self.kite.ORDER_TYPE_LIMIT
                price = float(ltp)-float(self._limit_margin)
                # The frontend sends quantity in LOTS; convert to units
                # (same as the NFO/BFO path below) so the live order
                # size matches what paper mode records.
                quantity = int(quantity) * int(lotsize)

                order_id = self.kite.place_order(
```

Note: for MCX, `lot_size` comes from constants.json via kite_utils.instr_det_attrib_mgmt (LOT_SIZE_CRUDEOILM="10", LOT_SIZE_CRUDEOIL="100"). FLAG FOR USER: these hardcoded values may be stale vs the daily kite_instruments.csv lot_size column — if Kite rejects an order as a non-lot multiple, verify these constants.

---

## D. Verification checklist (run after edits)

1. `venv\Scripts\python.exe -m py_compile core\trade_logic.py server.py adapter\kite_adapter.py`
2. `Select-String -Path *.py,core\*.py,adapter\*.py -Pattern "stop_loss_core"` → only backup files (trade_logic_3 sEP 2026.py, trade_logic_before_crude_change.py) may match.
3. `Select-String -Path core\trade_logic.py -Pattern "write_paper_trade"` → only: class def (A7), watcher call (A3), handle_sell_order call (A12), handle_buy_order call (A14). No nested def remaining.
4. `git diff` review — confirm zero changes inside: watcher LIVE branch, handle_buy_order LIVE branch, handle_sell_order LIVE branch, refresh_open_pos_buy_price, mstock_adapter.py, mstock_connector.py.
5. Functional smoke (no live account risk): set MODE=PAPER in settings.json, start app, verify:
   - log shows `Refreshing PAPER positions` and `PAPER POSITION DATA READY` with the 5 recovered NIFTY positions from PaperTrading.txt (header auto-repaired by first write OR readable via default-header fallback),
   - place a paper BUY → row appended with 12 columns,
   - place a paper SELL for more lots than held → `sell_order_result` failure,
   - paper SELL within limits → position nets down,
   - auto-sell watcher: with a position open and PTS thresholds met, a single SELL row is written (not one every 2s), thread survives.
6. Switch MODE back to LIVE and restart for the live session. Optional pre-flight: `git stash` / `git checkout -- <files>` reverts everything instantly.

## Residual risks / notes

- No automated test suite exists; verification is compile + grep + diff + manual smoke.
- The settings-cache fix (B) changes runtime behavior only when MODE is changed via the UI mid-session — today such changes silently do nothing; after the fix they take effect on the next order. This is the intended fix, but be aware when switching modes live.
- Paper sizing now depletes with open positions (A2) — the options table "Lots" column in PAPER mode shows available capacity instead of always full cash.
- Kite MCX lot constants staleness (see C note).
- `is_market_open()` end-time is 23:45 (utils.py:184) — pre-existing testing override; the watcher now survives errors, but consider restoring 15:30 before live use.
