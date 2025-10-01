#!/bin/bash

echo "Activating virtual environment..."

source venv/bin/activate

echo "Starting server... Press Ctrl+C to stop."

python3 server.py

