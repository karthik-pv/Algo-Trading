import os
import json
import ijson
import logging
import csv
from datetime import datetime, time
from typing import Generator, Dict, Any , List, Optional
from logging import Logger
from loguru import logger



def get_access_token_from_json():
    logger.info("Fetching access token from JSON file.")
    current_dir = os.path.dirname(os.path.abspath(__file__))
    token_file_path = os.path.join(current_dir, "access_token.json")
    with open(token_file_path, "r") as file:
        data = json.load(file)
    return data.get("access_token")


def fetch_from_json(file, attribute):
    logger.info(f"Fetching {attribute} from {file}.")
    current_dir = os.path.dirname(os.path.abspath(__file__))
    token_file_path = os.path.join(current_dir, file)
    with open(token_file_path, "r") as file:
        data = json.load(file)
    return data.get(attribute)


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
    
    return None



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
    

def is_market_open() -> bool:
    now = datetime.now()
    market_start = time(9, 15)
    market_end = time(15, 30)
    is_weekday = now.weekday() < 5
    is_market_hours = market_start <= now.time() <= market_end
    return is_weekday and is_market_hours