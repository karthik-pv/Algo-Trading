# Navigate to venv\Scripts
Set-Location ".\venv\Scripts"

# Activate the virtual environment
.\activate

# Navigate back to AlgoTrading
Set-Location "../.."

# Run the Python server
python server.py
