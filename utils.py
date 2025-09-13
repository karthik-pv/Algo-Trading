import os
import json


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
