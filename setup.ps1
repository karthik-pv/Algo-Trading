# 1. Create the virtual environment named 'venv'
python -m venv venv

# 2. Activate the virtual environment
# The dot '.' is necessary to run the script in the current shell scope
. .\venv\Scripts\Activate.ps1

# 3. Install packages from requirements.txt
pip install -r requirements.txt