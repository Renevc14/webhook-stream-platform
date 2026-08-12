"""
Configuration Repository
========================

Data access layer for imagetocell_config table.
"""

import logging
from typing import Optional, List
from datetime import datetime

from src.config.constants import TABLE_NAMES
from src.models.config import ImageToCellConfig, ProcessingMode
from src.services.supabase.client import SupabaseService, get_supabase_service


log = logging.getLogger(__name__)


class ConfigRepository:
    """
    Repository for Image-to-Cell configurations.

    Provides data access methods for the imagetocell_config table.
    """

    def __init__(self, supabase: Optional[SupabaseService] = None):
        """
        Initialize repository.

        Args:
            supabase: Supabase service (uses singleton if not provided)
        """
        self._db = supabase or get_supabase_service()

    def get_by_id(self, config_id: str) -> Optional[ImageToCellConfig]:
        """
        Get configuration by ID.

        Args:
            config_id: Configuration UUID

        Returns:
            Configuration or None if not found
        """
        result = self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.CONFIG)
                .select("*")
                .eq("id", config_id)
                .single()
                .execute(),
            f"get config {config_id}"
        )

        if result.data:
            return ImageToCellConfig(**result.data)
        return None

    def get_by_worksheet(
        self,
        worksheet_id: int,
        active_only: bool = True,
    ) -> List[ImageToCellConfig]:
        """
        Get configurations for a worksheet.

        Args:
            worksheet_id: Smartsheet worksheet ID
            active_only: Only return non-paused configurations

        Returns:
            List of configurations
        """
        query = (
            self._db.table(TABLE_NAMES.CONFIG)
            .select("*")
            .eq("worksheet_id", worksheet_id)
        )

        if active_only:
            query = query.eq("paused", False)

        result = self._db.execute_with_retry(
            lambda: query.execute(),
            f"get configs for worksheet {worksheet_id}"
        )

        return [ImageToCellConfig(**row) for row in (result.data or [])]

    def get_active_webhook_configs(
        self,
        worksheet_id: int,
        modes: Optional[List[str]] = None,
    ) -> List[ImageToCellConfig]:
        """
        Get active webhook configurations for a worksheet.

        Args:
            worksheet_id: Smartsheet worksheet ID
            modes: Processing modes to include (default: ["webhook", "webhook_v3"])

        Returns:
            List of active webhook configurations
        """
        if modes is None:
            modes = [ProcessingMode.WEBHOOK.value, ProcessingMode.WEBHOOK_V3.value]

        result = self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.CONFIG)
                .select("*")
                .eq("worksheet_id", worksheet_id)
                .in_("processing_mode", modes)
                .eq("paused", False)
                .execute(),
            f"get active webhook configs for {worksheet_id}"
        )

        return [ImageToCellConfig(**row) for row in (result.data or [])]

    def get_active_for_organization(
        self,
        organization_id: str,
        processing_mode: Optional[ProcessingMode] = None,
    ) -> List[ImageToCellConfig]:
        """
        Get active configurations for an organization.

        Args:
            organization_id: Organization UUID
            processing_mode: Optional filter by processing mode

        Returns:
            List of configurations
        """
        query = (
            self._db.table(TABLE_NAMES.CONFIG)
            .select("*")
            .eq("organization_id", organization_id)
            .eq("paused", False)
        )

        if processing_mode:
            query = query.eq("processing_mode", processing_mode.value)

        result = self._db.execute_with_retry(
            lambda: query.execute(),
            f"get configs for org {organization_id}"
        )

        return [ImageToCellConfig(**row) for row in (result.data or [])]

    def update_last_run(self, config_id: str) -> None:
        """
        Update the last_run_at timestamp.

        Args:
            config_id: Configuration ID
        """
        now = datetime.utcnow().isoformat()

        self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.CONFIG)
                .update({"last_run_at": now, "updated_at": now})
                .eq("id", config_id)
                .execute(),
            f"update last_run_at for {config_id}"
        )

    def update_last_run_batch(self, config_ids: List[str]) -> None:
        """
        Update last_run_at for multiple configurations.

        Args:
            config_ids: List of configuration IDs
        """
        if not config_ids:
            return

        now = datetime.utcnow().isoformat()

        self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.CONFIG)
                .update({"last_run_at": now, "updated_at": now})
                .in_("id", config_ids)
                .execute(),
            f"batch update last_run_at for {len(config_ids)} configs"
        )

    def set_paused(self, config_id: str, paused: bool) -> None:
        """
        Set the paused state of a configuration.

        Args:
            config_id: Configuration ID
            paused: New paused state
        """
        now = datetime.utcnow().isoformat()

        self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.CONFIG)
                .update({"paused": paused, "updated_at": now})
                .eq("id", config_id)
                .execute(),
            f"set paused={paused} for {config_id}"
        )

    def set_runner(self, config_id: str, runner_user_id: str) -> None:
        """
        SH-178: reassign the config's runner identity (failover or admin
        reassign). Clears needs_connection — a new runner means background
        work can resume.

        Args:
            config_id: Configuration ID
            runner_user_id: User whose token will execute background work
        """
        now = datetime.utcnow().isoformat()

        self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.CONFIG)
                .update({
                    "runner_user_id": runner_user_id,
                    "connection_status": "active",
                    "updated_at": now,
                })
                .eq("id", config_id)
                .execute(),
            f"set runner for {config_id}"
        )
        log.info(f"SH-178: config {config_id} runner reassigned")

    def mark_needs_connection(self, config_id: str) -> None:
        """
        SH-178: flag the config as disconnected — its runner's OAuth is gone
        and no org member qualified for automatic failover. The UI surfaces
        this so someone reconnects; background work is on hold meanwhile.

        Args:
            config_id: Configuration ID
        """
        now = datetime.utcnow().isoformat()

        self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.CONFIG)
                .update({
                    "connection_status": "needs_connection",
                    "updated_at": now,
                })
                .eq("id", config_id)
                .execute(),
            f"mark needs_connection for {config_id}"
        )
        log.warning(
            f"SH-178: config {config_id} marked needs_connection — "
            f"runner token gone and no failover candidate"
        )

    def get_organizations_with_active_configs(
        self,
        processing_mode: Optional[ProcessingMode] = None,
    ) -> List[str]:
        """
        Get unique organization IDs that have active configurations.

        Args:
            processing_mode: Optional filter by processing mode

        Returns:
            List of organization IDs
        """
        query = (
            self._db.table(TABLE_NAMES.CONFIG)
            .select("organization_id")
            .eq("paused", False)
        )

        if processing_mode:
            query = query.eq("processing_mode", processing_mode.value)

        result = self._db.execute_with_retry(
            lambda: query.execute(),
            "get organizations with active configs"
        )

        # Deduplicate
        org_ids = set()
        for row in (result.data or []):
            if row.get("organization_id"):
                org_ids.add(row["organization_id"])

        return list(org_ids)
