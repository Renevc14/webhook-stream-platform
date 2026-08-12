"""
Base Worker
===========

Abstract base class for all workers.
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, Any

from src.core.processor import ImageProcessor
from src.config.settings import Settings, get_settings
from src.utils.logging import LoggerMixin


class BaseWorker(ABC, LoggerMixin):
    """
    Abstract base class for background workers.

    All workers share common functionality:
    - Access to ImageProcessor
    - Settings management
    - Logging
    - Lifecycle management
    """

    def __init__(
        self,
        processor: Optional[ImageProcessor] = None,
        settings: Optional[Settings] = None,
    ):
        """
        Initialize worker.

        Args:
            processor: Image processor (creates new if not provided)
            settings: Application settings (uses singleton if not provided)
        """
        self._settings = settings or get_settings()
        self._processor = processor or ImageProcessor(settings=self._settings)
        self._running = False

    @property
    def processor(self) -> ImageProcessor:
        """Get the image processor."""
        return self._processor

    @property
    def settings(self) -> Settings:
        """Get application settings."""
        return self._settings

    @property
    def is_running(self) -> bool:
        """Check if worker is running."""
        return self._running

    @abstractmethod
    async def start(self) -> None:
        """
        Start the worker.

        This should be overridden by subclasses to implement
        the main worker loop.
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop the worker.

        This should gracefully shut down the worker.
        """
        pass

    @abstractmethod
    async def process_once(self) -> Any:
        """
        Run a single processing cycle.

        Useful for testing or manual triggering.

        Returns:
            Processing result
        """
        pass

    def _log_start(self) -> None:
        """Log worker start."""
        self.logger.info(f"{self.__class__.__name__} starting...")

    def _log_stop(self) -> None:
        """Log worker stop."""
        self.logger.info(f"{self.__class__.__name__} stopped")

    def _log_cycle(self, duration_ms: int, items_processed: int) -> None:
        """Log a processing cycle."""
        self.logger.debug(
            f"{self.__class__.__name__} cycle completed: "
            f"{items_processed} items in {duration_ms}ms"
        )
