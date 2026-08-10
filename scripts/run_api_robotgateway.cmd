@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set PREFECT_API_URL=http://127.0.0.1:4200/api
set GAME_DATA_SOURCE=robotgateway
set ROBOTGATEWAY_BASE_URL=http://127.0.0.1:9000
set ROBOTGATEWAY_CALLBACK_URL=http://127.0.0.1:9000/callbacks/analysis
"%~dp0..\.venv\Scripts\python.exe" -m uvicorn src.api.main:app --host 0.0.0.0 --port 8000
