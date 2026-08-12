"""SH-227 / SH-228 — runtime tool registry for gateway control plane.

Built-in seed covers I2C + SmartQR (parity with SH-208 / SH-210). Extra tools
are added by ``GATEWAY_TOOL_REGISTRY_JSON`` without editing Python constants.
Env ``WEBHOOK_REGISTRAR_TOOLS`` remains an enablement allowlist only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

DEFAULT_PROVIDER = "smartsheet"


class UnknownToolError(LookupError):
    """URL / publish slug is not in the registry — fail closed (no orphan stream)."""


@dataclass(frozen=True)
class ToolRegistryEntry:
    """One tool's control-plane mapping (tables, stream, provider, flags)."""

    tool_slug: str
    stream_name: str
    provider: str = DEFAULT_PROVIDER
    configs_table: str = ""
    webhooks_table: str = ""
    secret_column: str = "shared_secret"
    paused_column: str = "paused"
    processing_mode_column: Optional[str] = "processing_mode"
    processing_modes: Tuple[str, ...] = ("webhook",)
    # None = tool webhooks table has no runner column (e.g. imagetocell_webhooks).
    runner_column: Optional[str] = "runner_user_id"
    # Tool-table column for the Smartsheet callback URL (I2C uses webhook_url).
    callback_column: str = "callback_url"
    name_prefix: str = "Smarthub"
    secret_sync_enabled: bool = True
    # When False the entry is ignored by secret sync even if seeded.
    # Registrar enablement still uses WEBHOOK_REGISTRAR_TOOLS allowlist.

    def ownership_prefix(self, callback_base_url: str) -> str:
        base = callback_base_url.rstrip("/")
        return f"{base}/webhook/{self.provider}/{self.tool_slug}/"

    def stream_key(self) -> str:
        return f"stream:{self.stream_name}"


def _i2c_entry(*, tool_slug: str = "smart-image-to-cell") -> ToolRegistryEntry:
    return ToolRegistryEntry(
        tool_slug=tool_slug,
        stream_name="i2c",
        provider=DEFAULT_PROVIDER,
        configs_table="imagetocell_config",
        webhooks_table="imagetocell_webhooks",
        secret_column="shared_secret",
        paused_column="paused",
        processing_mode_column="processing_mode",
        processing_modes=("webhook", "webhook_v3"),
        runner_column=None,
        callback_column="webhook_url",
        name_prefix="Image-to-Cell",
        secret_sync_enabled=True,
    )


def _smartqr_entry(
    *,
    tool_slug: str = "smart-qr",
    secret_sync_enabled: bool = True,
) -> ToolRegistryEntry:
    return ToolRegistryEntry(
        tool_slug=tool_slug,
        stream_name="smart-qr",
        provider=DEFAULT_PROVIDER,
        configs_table="smartqr_configs",
        webhooks_table="smartqr_webhooks",
        secret_column="shared_secret_encrypted",
        paused_column="pause",
        processing_mode_column=None,
        processing_modes=(),
        runner_column="runner_user_id",
        name_prefix="smart-qr",
        secret_sync_enabled=secret_sync_enabled,
    )


def builtin_entries(
    *,
    i2c_tool_slug: str = "smart-image-to-cell",
    smartqr_tool_slug: str = "smart-qr",
    smartqr_secret_sync_enabled: bool = True,
) -> List[ToolRegistryEntry]:
    """Seed used when no JSON overlay is present (I2C + SmartQR)."""
    return [
        _i2c_entry(tool_slug=i2c_tool_slug),
        _smartqr_entry(
            tool_slug=smartqr_tool_slug,
            secret_sync_enabled=smartqr_secret_sync_enabled,
        ),
    ]


def _modes_from_raw(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(s.strip() for s in raw.split(",") if s.strip())
    if isinstance(raw, (list, tuple)):
        return tuple(str(x) for x in raw)
    raise ValueError(f"processing_modes must be list or string, got {type(raw)}")


def _optional_column(
    data: Mapping[str, Any], key: str, *, default: Optional[str]
) -> Optional[str]:
    """Parse an optional DB column name; explicit null/\"\" means absent."""
    if key not in data:
        return default
    raw = data.get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def entry_from_mapping(data: Mapping[str, Any]) -> ToolRegistryEntry:
    """Parse one registry row / JSON object into a frozen entry."""
    slug = str(data.get("tool_slug") or "").strip()
    if not slug:
        raise ValueError("tool_slug is required")
    stream = str(data.get("stream_name") or slug).strip()
    mode_col = data.get("processing_mode_column", "processing_mode")
    if mode_col is not None:
        mode_col = str(mode_col) or None
    return ToolRegistryEntry(
        tool_slug=slug,
        stream_name=stream,
        provider=str(data.get("provider") or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER,
        configs_table=str(data.get("configs_table") or ""),
        webhooks_table=str(data.get("webhooks_table") or ""),
        secret_column=str(data.get("secret_column") or "shared_secret"),
        paused_column=str(data.get("paused_column") or "paused"),
        processing_mode_column=mode_col,
        processing_modes=_modes_from_raw(
            data.get("processing_modes", ("webhook",) if mode_col else ())
        ),
        runner_column=_optional_column(
            data, "runner_column", default="runner_user_id"
        ),
        callback_column=str(data.get("callback_column") or "callback_url"),
        name_prefix=str(data.get("name_prefix") or "Smarthub"),
        secret_sync_enabled=bool(data.get("secret_sync_enabled", True)),
    )


def parse_registry_json(raw: str) -> List[ToolRegistryEntry]:
    """Parse ``GATEWAY_TOOL_REGISTRY_JSON`` — a JSON array of entry objects."""
    text = (raw or "").strip()
    if not text:
        return []
    payload = json.loads(text)
    if not isinstance(payload, list):
        raise ValueError("GATEWAY_TOOL_REGISTRY_JSON must be a JSON array")
    return [entry_from_mapping(item) for item in payload]


class ToolRegistry:
    """In-memory map of tool_slug → entry. Fail closed on unknown slugs."""

    def __init__(self, entries: Sequence[ToolRegistryEntry]) -> None:
        self._by_slug: Dict[str, ToolRegistryEntry] = {}
        for entry in entries:
            self._by_slug[entry.tool_slug] = entry

    def merge(self, overlays: Sequence[ToolRegistryEntry]) -> "ToolRegistry":
        merged = dict(self._by_slug)
        for entry in overlays:
            merged[entry.tool_slug] = entry
        return ToolRegistry(list(merged.values()))

    def get(self, tool_slug: str) -> Optional[ToolRegistryEntry]:
        slug = (tool_slug or "").strip()
        if not slug:
            return None
        hit = self._by_slug.get(slug)
        if hit is not None:
            return hit
        # Legacy short name: publishers/workers that still say ``i2c``.
        if slug == "i2c":
            for entry in self._by_slug.values():
                if entry.stream_name == "i2c":
                    return entry
        return None

    def require(self, tool_slug: str) -> ToolRegistryEntry:
        entry = self.get(tool_slug)
        if entry is None:
            log.warning("webhook_gateway: unknown_tool_slug=%s", tool_slug)
            raise UnknownToolError(tool_slug)
        return entry

    def all_entries(self) -> List[ToolRegistryEntry]:
        return list(self._by_slug.values())

    def secret_sync_entries(self) -> List[ToolRegistryEntry]:
        return [
            e
            for e in self.all_entries()
            if e.secret_sync_enabled and e.webhooks_table
        ]

    def registrar_entries(self, allowlist: Sequence[str]) -> List[ToolRegistryEntry]:
        """Resolve WEBHOOK_REGISTRAR_TOOLS against the registry."""
        out: List[ToolRegistryEntry] = []
        for slug in allowlist:
            slug = (slug or "").strip()
            if not slug:
                continue
            entry = self.get(slug)
            if entry is None:
                log.warning(
                    "webhook_gateway: registrar has no registry entry for tool=%s — skipped",
                    slug,
                )
                continue
            if not entry.configs_table or not entry.webhooks_table:
                log.warning(
                    "webhook_gateway: registrar entry incomplete for tool=%s — skipped",
                    slug,
                )
                continue
            out.append(entry)
        return out

    def as_list(self) -> List[Dict[str, Any]]:
        return [asdict(e) for e in self.all_entries()]


def load_tool_registry(
    *,
    i2c_tool_slug: str = "smart-image-to-cell",
    smartqr_tool_slug: str = "smart-qr",
    smartqr_secret_sync_enabled: bool = True,
    registry_json: Optional[str] = None,
) -> ToolRegistry:
    """Build registry from builtins + optional JSON overlay."""
    base = ToolRegistry(
        builtin_entries(
            i2c_tool_slug=i2c_tool_slug,
            smartqr_tool_slug=smartqr_tool_slug,
            smartqr_secret_sync_enabled=smartqr_secret_sync_enabled,
        )
    )
    if not registry_json:
        return base
    try:
        overlays = parse_registry_json(registry_json)
    except Exception as exc:
        log.error("webhook_gateway: GATEWAY_TOOL_REGISTRY_JSON invalid: %s", exc)
        raise
    if overlays:
        log.info(
            "webhook_gateway: registry overlay tools=%s",
            [e.tool_slug for e in overlays],
        )
        return base.merge(overlays)
    return base
