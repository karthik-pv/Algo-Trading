import os
import json
import ijson
import logging
import csv
from datetime import datetime, time
from typing import Generator, Dict, Any , List, Optional
from logging import Logger
from loguru import logger
from datetime import datetime
import pytz

_JSON_CACHE = {}

def get_access_token_from_json():
    logger.info("Fetching access token from JSON file.")
    current_dir = os.path.dirname(os.path.abspath(__file__))
    token_file_path = os.path.join(current_dir, "access_token.json")
    with open(token_file_path, "r") as f:
        data = json.load(f)
    return data.get("access_token")


def fetch_from_json(file, attribute):
    global _JSON_CACHE

    logger.info(f"Fetching {attribute} from {file}.")

    if file not in _JSON_CACHE:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        token_file_path = os.path.join(current_dir, file)

        logger.debug(f"Loading {file} into memory cache")

        with open(token_file_path, "r") as f:
            _JSON_CACHE[file] = json.load(f)

    return _JSON_CACHE[file].get(attribute)


def clear_json_cache(file):
    global _JSON_CACHE
    if file in _JSON_CACHE:
        del _JSON_CACHE[file]

def get_trading_symbols_from_json():
    logger.info("Fetching trading symbols from JSON file.")
    current_dir = os.path.dirname(os.path.abspath(__file__))
    symbols_file_path = os.path.join(current_dir, "trading_symbols.json")
    with open(symbols_file_path, "r") as file:
        data = json.load(file)
    return data.get("trading_symbols", [])


def find_matching_object(filepath: str, attribute_name: str, attribute_value: Any) -> List[Dict]:
    logger.info(f"Searching for object in {filepath} where {attribute_name} == {attribute_value}")
    try:
        with open(filepath, 'rb') as f:
            objects = ijson.items(f, 'item')
            for item in objects:
                if item.get(attribute_name) == attribute_value:
                    return item  
    except FileNotFoundError:
        logger.error(f"Error: The file '{filepath}' was not found.")
    except Exception as e:
        logger.error(f"An error occurred while processing the file: {e}")



def find_matching_row_in_csv(filepath: str, column_name: str, value_to_match: Any) -> Optional[Dict]:
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
    logger.info(f"Data to write is {data_to_write} file path is  {file_path}")
    try:
        try:
            with open(file_path, 'r') as f:
                existing_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            existing_data = {}

        existing_data.update(data_to_write)

        with open(file_path, 'w') as f:
            json.dump(existing_data, f, indent=4)
        
        logger.info(f"Successfully wrote updates to {file_path}")   
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


def is_market_open():
    """
    Returns True if NSE market hours are active.
    """
    tz = pytz.timezone("Asia/Kolkata")
    now = datetime.now(tz)

    # Weekend check
    if now.weekday() >= 5:  # Saturday=5 Sunday=6
        return False

    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    #end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    end = now.replace(hour=23, minute=45, second=0, microsecond=0)

    return start <= now <= end


import os
import shutil
from datetime import datetime, timedelta


def backup_old_logs(base_path="logs"):
    logs_path = os.path.abspath(base_path)
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


