"""Handler registry — maps URL provider segment to BaseWebhookHandler impl."""

from __future__ import annotations

import logging
from typing import Dict, Mapping, Optional

from smarthub_core.platforms.shared.webhook_base import BaseWebhookHandler
from smarthub_core.platforms.smartsheet.webhook import SmartsheetWebhookHandler

log = logging.getLogger(__name__)


class HandlerRegistry:
    """Dispatch registry keyed by provider slug (``smartsheet``, ``slack``, …)."""

    def __init__(self, handlers: Optional[Mapping[str, BaseWebhookHandler]] = None) -> None:
        self._handlers: Dict[str, BaseWebhookHandler] = dict(handlers or _default_handlers())

    @staticmethod
    def default() -> "HandlerRegistry":
        return HandlerRegistry(_default_handlers())

    def get(self, provider: str) -> BaseWebhookHandler:
        key = provider.lower().strip()
        handler = self._handlers.get(key)
        if handler is None:
            log.warning("webhook_gateway: unknown provider=%s", provider)
            raise KeyError(key)
        return handler

    def register(self, handler: BaseWebhookHandler) -> None:
        self._handlers[handler.provider] = handler
        log.info("webhook_gateway: registered provider=%s", handler.provider)


def _default_handlers() -> Dict[str, BaseWebhookHandler]:
    smartsheet = SmartsheetWebhookHandler()
    return {smartsheet.provider: smartsheet}
