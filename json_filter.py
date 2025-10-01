import json
import sys
from typing import List, Dict, Any

def load_json_file(filepath: str) -> Any:
    """Loads and parses a JSON file."""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: The file '{filepath}' was not found.", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from the file '{filepath}'. Please check its format.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)

def item_matches_patterns(item: Dict[str, Any], attribute: str, patterns: List[str]) -> bool:
    """
    Checks if an item's attribute contains all specified patterns (case-insensitive).
    Returns True if it matches, False otherwise.
    """
    if attribute not in item:
        return False

    value_to_check = item[attribute]

    # Ensure the value we are checking is a string before searching
    if not isinstance(value_to_check, str):
        return False

    # Check if all patterns are present in the value (case-insensitive)
    for pattern in patterns:
        if pattern.lower() not in value_to_check.lower():
            return False  # If any pattern is not found, it's not a match

    return True  # All patterns were found

def main():
    """Main function to prompt for inputs and run the filter."""
    print("--- Interactive JSON Filter ---")
    
    # 1. Get inputs from the user
    filepath = input("Enter the path to the JSON file: ").strip()
    if not filepath:
        print("Error: File path cannot be empty.", file=sys.stderr)
        sys.exit(1)

    attribute = input("Enter the attribute name to search in (e.g., symbol): ").strip()
    if not attribute:
        print("Error: Attribute name cannot be empty.", file=sys.stderr)
        sys.exit(1)

    patterns_str = input("Enter patterns to search for, separated by spaces (e.g., NIFTY FUT): ").strip()
    if not patterns_str:
        print("Error: Patterns cannot be empty.", file=sys.stderr)
        sys.exit(1)
        
    patterns = patterns_str.split()

    # 2. Load and process the data
    data = load_json_file(filepath)

    if not isinstance(data, list):
        print("Error: This script is designed to filter a list of objects.", file=sys.stderr)
        sys.exit(1)
    
    # 3. Apply the filter
    filtered_result = [
        item for item in data 
        if item_matches_patterns(item, attribute, patterns)
    ]

    # 4. Print the output
    if filtered_result:
        print("\n--- Filtered Results ---")
        print(json.dumps(filtered_result, indent=2))
    else:
        print(f"\n--- No items found where the '{attribute}' attribute contains all specified patterns. ---")

