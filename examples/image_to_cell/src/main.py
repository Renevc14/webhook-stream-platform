"""
Image-to-Cell Service Entry Point
==================================

Main entry point for running the Image-to-Cell service.
"""

import asyncio
import logging
import sys
from pathlib import Path

import uvicorn

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.settings import get_settings
from src.utils.logging import setup_logging


def main():
    """Run the Image-to-Cell service."""
    # Setup logging
    settings = get_settings()
    setup_logging(
        level=settings.log_level,
        json_format=settings.log_json_format,
    )

    log = logging.getLogger(__name__)
    log.info("Starting Image-to-Cell service v3.0.0")
    log.info(f"Environment: {settings.environment}")

    # Run the FastAPI app
    uvicorn.run(
        "src.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "development",
        workers=1 if settings.environment == "development" else settings.workers,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
