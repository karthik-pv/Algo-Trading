import os
import json
import math
import ijson
import logging
import csv
import calendar
from time import sleep
from datetime import datetime, time
from typing import Generator, Dict, Any , List, Optional
from logging import Logger
from loguru import logger
from datetime import datetime
import pytz
import requests

_JSON_CACHE = {}

# Project layout: hand-edited settings live in config/, everything the
# app generates or persists at runtime lives in data/. All path
# resolution funnels through resolve_data_path() so callers can keep
# passing bare filenames.
# This module lives in core/, so the project root is one level up -
# everything (config/, data/, root files) resolves from there.
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(_BASE_DIR, "config")
DATA_DIR = os.path.join(_BASE_DIR, "data")

_CONFIG_FILES = {
    "appconfig.json",
    "simulation.json",
    "Playback.json",
}

_DATA_FILES = {
    "access_token.json",
    "day_cash.json",
    "last_known_prices.json",
    "order_selections.json",
    "kite_instruments.csv",
    "mstock_instrument_list.json",
    "mstock_instrument_list_reduced.json",
    "PaperTrading.txt",
    "PlaybackPrice.csv",
}


def resolve_data_path(filename):
    """
    Resolve a bare config/data filename to its absolute location:
    known config files -> config/, known runtime data files -> data/.

    Absolute paths and paths that already include a directory are
    returned unchanged. Unknown bare names stay in the project root
    (same behavior as before the config/data split).
    """
    name = str(filename)
    if os.path.isabs(name) or os.path.dirname(name):
        return name
    if name in _CONFIG_FILES:
        return os.path.join(CONFIG_DIR, name)
    if name in _DATA_FILES:
        return os.path.join(DATA_DIR, name)
    return os.path.join(_BASE_DIR, name)


def get_access_token_from_json():
    logger.info("Fetching access token from JSON file.")
    token_file_path = resolve_data_path("access_token.json")
    with open(token_file_path, "r") as f:
        data = json.load(f)
    return data.get("access_token")


_JSON_READ_RETRIES = 5
_JSON_READ_RETRY_DELAY_SECONDS = 1.0

def load_json_with_retry(file_path, retries=_JSON_READ_RETRIES, delay=_JSON_READ_RETRY_DELAY_SECONDS):
    """
    Loads and parses a JSON file, retrying on transient errors such as
    file locks held by sync/antivirus tools or partially written files.
    """
    for attempt in range(1, retries + 1):
        try:
            with open(file_path, "r", encoding="utf-8") as json_file:
                return json.load(json_file)
        except (OSError, json.JSONDecodeError) as error:
            if attempt >= retries:
                logger.exception(f"Failed to load '{file_path}' after {retries} attempts.")
                raise
            logger.warning(
                f"Transient error loading '{file_path}' "
                f"(attempt {attempt}/{retries}): {error}. Retrying in {delay} seconds."
            )
            sleep(delay)

def _flatten_config(data):
    """
    Flatten a grouped (nested) config dict into a flat {key: value} map.

    appconfig.json stores its keys nested under logical groups (session,
    pct, pnt, ...). Keys are globally unique across groups, so expanding
    each group into the flat view is lossless, and every
    fetch_from_json(file, KEY) call site keeps working unchanged.
    Top-level non-dict entries (e.g. the runtime-written SELL_MODE) are
    kept as-is.
    """
    if not isinstance(data, dict):
        return data
    flat = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


def fetch_from_json(file, attribute):
    global _JSON_CACHE

    if file not in _JSON_CACHE:
        logger.info(f"Fetching {attribute} from {file}.")
        token_file_path = resolve_data_path(file)

        logger.debug(f"Loading {file} into memory cache")

        _JSON_CACHE[file] = _flatten_config(load_json_with_retry(token_file_path))
    else:
        logger.trace(f"Fetching {attribute} from {file} (cached).")

    return _JSON_CACHE[file].get(attribute)


def clear_json_cache(file):
    global _JSON_CACHE, _LIMIT_MARGIN_CONFIG_CACHE
    # Cache keys are bare filenames; callers may pass resolved absolute
    # paths, so match on the basename.
    key = os.path.basename(str(file))
    if key in _JSON_CACHE:
        del _JSON_CACHE[key]
    # Keep the tiered limit-margin config in sync with appconfig.json
    # so runtime edits (config UI save) take effect immediately.
    if key.endswith("appconfig.json"):
        _LIMIT_MARGIN_CONFIG_CACHE = None


# Exchange is fully determined by the underlying; the single shared map
# replaces the old instrument-derived EXCHANGE constant.
UNDERLYING_TO_EXCHANGE = {
    "NIFTY": "NFO",
    "SENSEX": "BFO",
    "CRUDEOIL": "MCX",
    "CRUDEOILM": "MCX",
}


def get_exchange_for_underlying(underlying) -> Optional[str]:
    return UNDERLYING_TO_EXCHANGE.get(str(underlying or "").strip().upper())


# Longest-first so CRUDEOILM wins over CRUDEOIL when matching prefixes.
_UNDERLYING_PREFIXES = sorted(
    UNDERLYING_TO_EXCHANGE.keys(), key=len, reverse=True
)


def get_underlying_for_symbol(symbol) -> Optional[str]:
    """
    Map an instrument symbol (e.g. CRUDEOILM26SEP10300CE,
    SENSEX2691774400PE, NIFTY26SEPFUT) to its underlying by
    longest-prefix match. Returns None when no known underlying
    prefix matches.
    """
    text = str(symbol or "").strip().upper()
    for prefix in _UNDERLYING_PREFIXES:
        if text.startswith(prefix):
            return prefix
    return None


# Instrument files that carry a meta block use this shape:
#   {"meta": {...}, "instruments": [...]}
INSTRUMENTS_SECTION_PREFIX = "instruments.item"


def read_instrument_meta(filepath: str) -> Dict:
    """
    Read the derived meta block from an instrument file of the shape
    {"meta": ..., "instruments": [...]}. Returns {} for legacy
    array-shaped files (no meta) or read errors.
    """
    filepath = resolve_data_path(filepath)
    try:
        with open(filepath, "r", encoding="utf-8") as instrument_file:
            data = json.load(instrument_file)
        if isinstance(data, dict):
            return data.get("meta") or {}
    except (OSError, json.JSONDecodeError) as error:
        logger.warning(f"Could not read instrument meta from '{filepath}': {error}")
    return {}


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# Memoized tiered limit-margin config. compute_limit_margin() runs on
# every websocket tick (lot sizing for each contract), so the config
# must be read (and logged) once, not per call. Reset via
# clear_json_cache("appconfig.json") whenever appconfig.json changes.
_LIMIT_MARGIN_CONFIG_CACHE = None


def _load_limit_margin_config():
    global _LIMIT_MARGIN_CONFIG_CACHE
    if _LIMIT_MARGIN_CONFIG_CACHE is not None:
        return _LIMIT_MARGIN_CONFIG_CACHE

    config = {
        # Missing tiers fall back to the standard market-protection
        # percentages (5/3/2/1) so behavior never degrades.
        "pct_lt_10": _to_float(
            fetch_from_json("appconfig.json", "LIMIT_MARGIN_PCT_LT_10"), default=5.0
        ),
        "pct_10_100": _to_float(
            fetch_from_json("appconfig.json", "LIMIT_MARGIN_PCT_10_100"), default=3.0
        ),
        "pct_100_500": _to_float(
            fetch_from_json("appconfig.json", "LIMIT_MARGIN_PCT_100_500"), default=2.0
        ),
        "pct_gt_500": _to_float(
            fetch_from_json("appconfig.json", "LIMIT_MARGIN_PCT_GT_500"), default=1.0
        ),
        "min_pts": _to_float(
            fetch_from_json("appconfig.json", "LIMIT_MARGIN_MIN_PTS"), default=0.05
        ) or 0.05,
        "max_pts": _to_float(
            fetch_from_json("appconfig.json", "LIMIT_MARGIN_MAX_PTS"), default=3.0
        ),
    }
    if config["max_pts"] is None:
        config["max_pts"] = 3.0
    if config["min_pts"] > config["max_pts"]:
        config["min_pts"] = config["max_pts"]

    _LIMIT_MARGIN_CONFIG_CACHE = config
    return config


def compute_limit_margin(ltp):
    """
    Tiered market-protection buffer (in points) for a given LTP.

    Mirrors Zerodha's market-protection percentages for options:
        LTP < 10        -> 5%
        10 <= LTP < 100 -> 3%
        100 <= LTP < 500-> 2%
        LTP >= 500      -> 1%

    The result is clamped between LIMIT_MARGIN_MIN_PTS (default 0.05,
    one tick, so ultra-cheap options stay marketable) and
    LIMIT_MARGIN_MAX_PTS (default 3.0).
    """
    try:
        ltp = float(ltp)
    except (TypeError, ValueError):
        ltp = 0.0

    config = _load_limit_margin_config()

    if ltp < 10:
        pct = config["pct_lt_10"]
    elif ltp < 100:
        pct = config["pct_10_100"]
    elif ltp < 500:
        pct = config["pct_100_500"]
    else:
        pct = config["pct_gt_500"]

    buffer = ltp * pct / 100.0

    return min(config["max_pts"], max(config["min_pts"], buffer))


def round_to_tick(price, tick=0.05, up=True):
    """
    Round a price onto the exchange tick grid.

    up=True  -> rounds UP (buy limits stay marketable),
    up=False -> rounds DOWN (sell limits stay marketable).
    Invalid/zero tick falls back to 0.05. The result is always >= one tick.
    """
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = 0.0

    try:
        tick = float(tick)
    except (TypeError, ValueError):
        tick = 0.05

    if tick <= 0:
        tick = 0.05

    steps = price / tick
    if up:
        rounded = math.ceil(steps) * tick
    else:
        rounded = math.floor(steps) * tick

    rounded = max(tick, round(rounded, 2))
    return rounded

def get_trading_symbols_from_json():
    logger.info("Fetching trading symbols from JSON file.")
    symbols_file_path = os.path.join(_BASE_DIR, "trading_symbols.json")
    with open(symbols_file_path, "r") as file:
        data = json.load(file)
    return data.get("trading_symbols", [])


_OBJECT_SEARCH_CACHE = {}
_OBJECT_SEARCH_CACHE_MAX_FILE_SIZE = 5 * 1024 * 1024

def find_matching_object(
    filepath: str,
    attribute_name: str,
    attribute_value: Any,
    prefix: str = "item",
) -> List[Dict]:
    """
    Search an array inside a JSON file for the first object whose
    attribute matches. `prefix` is the ijson path of the array: "item"
    for a top-level array, or "instruments.item" for the wrapped
    {"meta": ..., "instruments": [...]} instrument-file shape.
    """
    filepath = resolve_data_path(filepath)
    logger.info(f"Searching for object in {filepath} where {attribute_name} == {attribute_value}")
    try:
        file_stat = os.stat(filepath)
        file_signature = (file_stat.st_mtime_ns, file_stat.st_size)

        if file_stat.st_size <= _OBJECT_SEARCH_CACHE_MAX_FILE_SIZE:
            cache_key = (filepath, attribute_name, prefix)
            cached = _OBJECT_SEARCH_CACHE.get(cache_key)

            if cached is None or cached[0] != file_signature:
                with open(filepath, 'rb') as f:
                    index = {}
                    for item in ijson.items(f, prefix):
                        key = item.get(attribute_name)
                        if key is not None and key not in index:
                            index[key] = item
                _OBJECT_SEARCH_CACHE[cache_key] = (file_signature, index)

            item = _OBJECT_SEARCH_CACHE[cache_key][1].get(attribute_value)
            if item is not None:
                return item
            return None

        with open(filepath, 'rb') as f:
            objects = ijson.items(f, prefix)
            for item in objects:
                if item.get(attribute_name) == attribute_value:
                    return item
    except FileNotFoundError:
        logger.error(f"Error: The file '{filepath}' was not found.")
    except Exception as e:
        logger.error(f"An error occurred while processing the file: {e}")



def find_matching_row_in_csv(filepath: str, column_name: str, value_to_match: Any) -> Optional[Dict]:
    filepath = resolve_data_path(filepath)
    logger.info(f"Searching for row in {filepath} where {column_name} == {value_to_match}")
    try:
        with open(filepath, mode='r', newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            value_str = str(value_to_match)
            for row in reader:
                if row.get(column_name) == value_str:
                    return dict(row) 
                    
    except FileNotFoundError:
        logger.error(f"Error: The file '{filepath}' was not found.")
    except Exception as e:
        logger.error(f"An error occurred while processing the CSV file: {e}")
        
    
    return None


def write_to_json(data_to_write: dict, file_path: str) -> bool:
    #logger.info(f"Data to write is {data_to_write} file path is  {file_path}")  # leaks tokens when writing access_token.json
    # Mask token values so the payload stays visible in logs without exposing credentials
    safe_data = {
        key: ("***MASKED***" if "token" in str(key).lower() else value)
        for key, value in data_to_write.items()
    }
    logger.debug(f"Data to write is {safe_data} file path is  {file_path}")
    try:
        file_path = resolve_data_path(file_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        try:
            with open(file_path, 'r') as f:
                existing_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            existing_data = {}

        existing_data.update(data_to_write)

        with open(file_path, 'w') as f:
            json.dump(existing_data, f, indent=4)

        clear_json_cache(file_path)
        
        logger.debug(f"Successfully wrote updates to {file_path}")   
        return True

    except (IOError, TypeError) as e:
        Logger.error(f"Failed to write to JSON file at {file_path}: {e}")

    return False
    

# def is_market_open() -> bool:
#     now = datetime.now()
#     market_start = time(9, 15)
#     market_end = time(15, 30)
#     is_weekday = now.weekday() < 5
#     is_market_hours = market_start <= now.time() <= market_end
#     return is_weekday and is_market_hours


_MARKET_SESSIONS = {
    # exchange -> (start_h, start_m, end_h, end_m) in IST
    # NSE/BSE equity + F&O segments: 09:15 - 15:30
    "NSE":  (9, 15, 15, 30),
    "BSE":  (9, 15, 15, 30),
    "NFO":  (9, 15, 15, 30),
    "BFO":  (9, 15, 15, 30),
}


def _mcx_session(now):
    """
    MCX session for internationally linked commodities (crude oil).

    Start is fixed at 09:00. The close follows US daylight time:
    23:30 while the US is on DST (second Sunday of March to first
    Sunday of November), 23:55 otherwise. The switch dates are
    computed, so the seasonal change needs no manual updates.
    """
    year = now.year

    def _nth_sunday(month, n):
        last_day = calendar.monthrange(year, month)[1]
        sundays = [
            d for d in range(1, last_day + 1)
            if datetime(year, month, d).weekday() == 6
        ]
        return sundays[n - 1] if len(sundays) >= n else last_day

    dst_start = datetime(year, 3, _nth_sunday(3, 2))
    dst_end = datetime(year, 11, _nth_sunday(11, 1))

    if dst_start <= now.replace(tzinfo=None) < dst_end:
        return (9, 0, 23, 30)

    return (9, 0, 23, 55)


def _active_market_session():
    """
    (start_h, start_m, end_h, end_m) for the configured exchange.

    NSE/BSE segments close 15:30; MCX follows its own long session
    (see _mcx_session). The session is picked from the active
    UNDERLYING in appconfig.json (exchange derived via the shared
    UNDERLYING_TO_EXCHANGE map), so switching underlying needs no
    manual time changes.

    An explicit MARKET_CLOSE_TIME in appconfig.json overrides the
    session end - an emergency correction valve if exchange timings
    ever change unexpectedly. It is NOT for extending PAPER trading
    past real hours: LIVE and PAPER are designed to run during market
    hours only (use PLAYBACK/SIMULATION for off-hours testing).
    """
    underlying = str(fetch_from_json("appconfig.json", "UNDERLYING") or "").upper()
    exchange = get_exchange_for_underlying(underlying) or "NFO"

    if exchange == "MCX":
        session = _mcx_session(datetime.now(pytz.timezone("Asia/Kolkata")))
    else:
        session = _MARKET_SESSIONS.get(exchange, (9, 15, 15, 30))

    raw = fetch_from_json("appconfig.json", "MARKET_CLOSE_TIME")
    try:
        hour_s, minute_s = str(raw).strip().split(":")
        hour, minute = int(hour_s), int(minute_s)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return session[0], session[1], hour, minute
        logger.warning(
            f"Invalid MARKET_CLOSE_TIME '{raw}'; using the default "
            f"session end for exchange {exchange or 'NFO'}."
        )
    except (AttributeError, ValueError):
        pass

    return session


def is_market_open():
    """
    Returns True if the ACTIVE exchange's market hours are active.
    """
    tz = pytz.timezone("Asia/Kolkata")
    now = datetime.now(tz)

    # Weekend check
    if now.weekday() >= 5:  # Saturday=5 Sunday=6
        return False

    start_h, start_m, end_h, end_m = _active_market_session()

    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

    return start <= now <= end


_INTERNET_CHECK_URLS = (
    "https://api.kite.trade",
    "https://api.mstock.trade",
    "https://www.google.com/generate_204",
    "https://1.1.1.1",
)
_INTERNET_CHECK_TIMEOUT_SECONDS = 3
_INTERNET_CHECK_RETRIES = 3


def check_internet_connectivity(
    urls=_INTERNET_CHECK_URLS,
    timeout=_INTERNET_CHECK_TIMEOUT_SECONDS,
    retries=_INTERNET_CHECK_RETRIES,
):
    """
    Returns True quickly if any known endpoint responds, else False.
    """
    for attempt in range(1, retries + 1):
        for url in urls:
            try:
                response = requests.get(url, timeout=timeout)
                if response.status_code < 500:
                    logger.info(f"Internet connectivity confirmed via {url}")
                    return True
            except requests.RequestException as error:
                logger.debug(f"Connectivity probe failed for {url}: {error}")

        if attempt < retries:
            logger.warning(
                f"No internet connectivity detected (attempt {attempt}/{retries}). "
                f"Retrying in {_INTERNET_CHECK_TIMEOUT_SECONDS} seconds."
            )
            sleep(_INTERNET_CHECK_TIMEOUT_SECONDS)

    return False


import os
import shutil
from datetime import datetime, timedelta


def backup_old_logs(base_path=None):
    logs_path = os.path.abspath(base_path or os.path.join(DATA_DIR, "logs"))
    backup_path = os.path.join(logs_path, "0_backup")

    # Ensure backup folder exists
    os.makedirs(backup_path, exist_ok=True)

    # Calculate current week (Monday → Sunday)
    today = datetime.today()
    start_of_week = today - timedelta(days=today.weekday())

    current_week_folders = {
        (start_of_week + timedelta(days=i)).strftime("%m_%d")
        for i in range(7)
    }

    # Iterate through folders in logs directory
    for item in os.listdir(logs_path):
        src_path = os.path.join(logs_path, item)

        # Skip non-directories and backup folder itself
        if not os.path.isdir(src_path) or item == "0_backup":
            continue

        # Skip current week folders
        if item in current_week_folders:
            print(f"Skipping current week folder: {item}")
            continue

        dest_path = os.path.join(backup_path, item)

        # Move folder if not already moved
        if not os.path.exists(dest_path):
            print(f"Moving: {item} → 0_backup")
            shutil.move(src_path, dest_path)
        else:
            print(f"Already exists in backup, skipping: {item}")


DAY_CASH_FILE = resolve_data_path("day_cash.json")


def _migrate_day_cash_entries(data):
    """
    Normalize the day-cash payload into a per-date entries map.

    The legacy file was a single-slot payload
    ({"date": ..., "cash_balance": ...}) overwritten every day; the
    v2 format keeps one entry per trade date
    ({"version": 2, "entries": {"<date>": {...}}}).
    """
    if not isinstance(data, dict):
        return {}
    entries = data.get("entries")
    if isinstance(entries, dict):
        return {str(k): v for k, v in entries.items() if isinstance(v, dict)}
    if data.get("date"):
        try:
            return {
                str(data.get("date")): {
                    "cash_balance": float(data.get("cash_balance")),
                    "source": "persisted",
                }
            }
        except (TypeError, ValueError):
            return {}
    return {}


def load_day_cash():
    try:
        with open(DAY_CASH_FILE, "r", encoding="utf-8") as day_cash_file:
            data = json.load(day_cash_file)
        return _migrate_day_cash_entries(data)
    except (OSError, json.JSONDecodeError) as error:
        logger.warning(f"Could not load day cash file: {error}")
        return {}


def save_day_cash(trade_date, cash_balance, source=None):
    entries = load_day_cash()
    entries[str(trade_date)] = {
        "cash_balance": float(cash_balance),
        "source": str(source or "persisted"),
    }
    payload = {"version": 2, "entries": entries}
    tmp_path = DAY_CASH_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as tmp_file:
            json.dump(payload, tmp_file, indent=2)
        os.replace(tmp_path, DAY_CASH_FILE)
        logger.info(f"Persisted day-start cash for {trade_date}: {cash_balance}")
    except OSError as error:
        logger.error(f"Could not persist day-start cash: {error}")


def resolve_day_start_cash(broker, trade_date=None):
    tz = pytz.timezone("Asia/Kolkata")
    today = datetime.now(tz).strftime("%Y-%m-%d")
    target_date = str(trade_date) if trade_date else today

    if target_date != today:
        # Broker fund APIs only describe the current day, so a
        # historical opening balance can only come from what was
        # persisted on that day.
        persisted = load_day_cash()
        entry = persisted.get(target_date)
        if isinstance(entry, dict):
            try:
                persisted_value = float(entry.get("cash_balance"))
                if math.isfinite(persisted_value):
                    return persisted_value, "persisted"
            except (TypeError, ValueError):
                pass
        return None, "unavailable"

    def _persisted_today():
        entry = load_day_cash().get(today)
        if isinstance(entry, dict):
            try:
                persisted_value = float(entry.get("cash_balance"))
                if math.isfinite(persisted_value):
                    return persisted_value
            except (TypeError, ValueError):
                pass
        return None

    fetcher = getattr(broker, "fetch_day_start_cash", None)
    broker_value = fetcher() if fetcher else None
    if broker_value is not None:
        try:
            broker_value = float(broker_value)
        except (TypeError, ValueError):
            broker_value = None
    if broker_value is not None and math.isfinite(broker_value):
        # Persist every broker-sourced figure so the date stays
        # resolvable once it becomes historical.
        save_day_cash(today, broker_value, source="broker")
        return broker_value, "broker"

    persisted_value = _persisted_today()
    if persisted_value is not None:
        return persisted_value, "persisted"

    try:
        fund_summary = broker.fetch_fund_summary() or {}
        current_cash = fund_summary.get("cash_balance")
    except Exception as error:
        logger.error(f"Could not fetch fund summary for day-start cash: {error}")
        current_cash = None
    if current_cash is not None:
        try:
            current_cash = float(current_cash)
        except (TypeError, ValueError):
            current_cash = None
    if current_cash is not None and math.isfinite(current_cash):
        save_day_cash(today, current_cash, source="captured")
        return current_cash, "captured"

    return None, "unavailable"


