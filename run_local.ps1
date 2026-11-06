$env:DATABASE_URL = "sqlite:///./local_dev.db"
$env:PYTHONIOENCODING = "utf-8"
Set-Location $PSScriptRoot
if (-not (Test-Path "local_dev.db")) { & .\venv\Scripts\python.exe init_db.py }
& .\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 9920 --reload
