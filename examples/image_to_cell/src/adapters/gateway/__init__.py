"""Gateway adapters for I2C (Pulse cutover)."""

from .client import ensure_webhook_registration, push_webhook_secrets

__all__ = ["ensure_webhook_registration", "push_webhook_secrets"]
