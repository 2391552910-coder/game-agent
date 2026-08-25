from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static" / "gateway_monitor"


@router.get("/gateway")
async def gateway_monitor_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")


@router.get("/gateway/assets/app.css")
async def gateway_monitor_stylesheet() -> FileResponse:
    return FileResponse(_STATIC_DIR / "app.css", media_type="text/css; charset=utf-8")


@router.get("/gateway/assets/app.js")
async def gateway_monitor_script() -> FileResponse:
    return FileResponse(_STATIC_DIR / "app.js", media_type="application/javascript; charset=utf-8")
