"""Uvicorn entrypoint."""

import logging
import os

from gateway.app import create_app

_level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _level_name, logging.INFO),
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)

app = create_app()
