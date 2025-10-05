import os
import json
import ijson
import logging
from typing import Generator, Dict, Any , List



def get_access_token_from_json():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    token_file_path = os.path.join(current_dir, "access_token.json")
    with open(token_file_path, "r") as file:
        data = json.load(file)
    return data.get("access_token")


def fetch_from_json(file, attribute):
    current_dir = os.path.dirname(os.path.abspath(__file__))
    token_file_path = os.path.join(current_dir, file)
    with open(token_file_path, "r") as file:
        data = json.load(file)
    return data.get(attribute)


def get_trading_symbols_from_json():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    symbols_file_path = os.path.join(current_dir, "trading_symbols.json")
    with open(symbols_file_path, "r") as file:
        data = json.load(file)
    return data.get("trading_symbols", [])


def find_matching_object(filepath: str, attribute_name: str, attribute_value: Any) -> List[Dict]:
    try:
        with open(filepath, 'rb') as f:
            objects = ijson.items(f, 'item')
            for item in objects:
                if item.get(attribute_name) == attribute_value:
                    return item  
    except FileNotFoundError:
        print(f"Error: The file '{filepath}' was not found.")
    except Exception as e:
        print(f"An error occurred while processing the file: {e}")
    
    return None


def write_to_json(data_to_write: dict, file_path: str) -> bool:
    try:
        try:
            with open(file_path, 'r') as f:
                existing_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            existing_data = {}

        existing_data.update(data_to_write)

        with open(file_path, 'w') as f:
            json.dump(existing_data, f, indent=4)
        
        logging.info(f"Successfully wrote updates to {file_path}")
        return True

    except (IOError, TypeError) as e:
        logging.error(f"Failed to write to JSON file at {file_path}: {e}")
        return False