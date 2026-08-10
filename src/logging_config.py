from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from uvicorn.logging import AccessFormatter

SENSITIVE_LOGGER_LEVELS: Mapping[str, int] = {
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "openai": logging.WARNING,
    "anthropic": logging.WARNING,
    "langchain": logging.WARNING,
    "langgraph": logging.WARNING,
    "prefect": logging.WARNING,
}
UVICORN_ACCESS_LOG_FORMAT = (
    '%(asctime)s [%(levelname)s] %(client_addr)s - "%(request_line)s" %(status_code)s'
)


def configure_logging() -> None:
    configured_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, configured_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    access_formatter = AccessFormatter(fmt=UVICORN_ACCESS_LOG_FORMAT, use_colors=False)
    for handler in logging.getLogger("uvicorn.access").handlers:
        handler.setFormatter(access_formatter)
    for logger_name, minimum_level in SENSITIVE_LOGGER_LEVELS.items():
        logging.getLogger(logger_name).setLevel(max(level, minimum_level))
