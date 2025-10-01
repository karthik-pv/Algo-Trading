import os
import json
import ijson
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
                    return item  # Return the first match and exit immediately
    except FileNotFoundError:
        print(f"Error: The file '{filepath}' was not found.")
    except Exception as e:
        print(f"An error occurred while processing the file: {e}")
    
    return None # Return None if the loop finishes without finding a match


print(find_matching_object("instrument_list.json" , "name" , fetch_from_json("constants.json" , "NIFTY_NEAR_MONTH_FUTURE_TOKEN")))