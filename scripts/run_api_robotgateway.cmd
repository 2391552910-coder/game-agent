@echo off
set PREFECT_API_URL=http://127.0.0.1:4200/api
set GAME_DATA_SOURCE=robotgateway
set ROBOTGATEWAY_BASE_URL=http://127.0.0.1:9000
set ROBOTGATEWAY_CALLBACK_URL=http://127.0.0.1:9000/callbacks/analysis
"%~dp0..\.venv\Scripts\python.exe" -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000
