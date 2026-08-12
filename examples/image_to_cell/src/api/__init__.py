"""
API Module
==========

Avoid eager imports of `app` here so `from src.api.jwt_auth import ...` works in unit tests
without loading the full FastAPI stack.
"""

from typing import Any

__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from src.api.app import app as _app

        return _app
    if name == "create_app":
        from src.api.app import create_app as _create_app

        return _create_app
    raise AttributeError(name)
