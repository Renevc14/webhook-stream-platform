"""Write-mode policy (pure)."""

from __future__ import annotations

from typing import Literal

WriteMode = Literal["replace", "fill_if_empty"]


def should_write(write_mode: WriteMode, target_has_image: bool) -> bool:
    if write_mode == "replace":
        return True
    if write_mode == "fill_if_empty":
        return not target_has_image
    return True
