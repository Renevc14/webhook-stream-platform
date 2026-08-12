"""
State Repository
================

Data access layer for imagetocell_row_states table.
"""

import logging
from typing import Optional, Dict, List, Set
from datetime import datetime

from src.config.constants import TABLE_NAMES
from src.models.row_state import RowState
from src.services.supabase.client import SupabaseService, get_supabase_service


log = logging.getLogger(__name__)


# Maximum row IDs per `in.(...)` filter sent to PostgREST/Supabase in one request.
# Smartsheet row IDs are 16-19 digit integers; serialized + comma-separated +
# URL-encoded they consume ~20 bytes each. Cloudflare (Supabase Cloud's edge)
# rejects query strings over ~8 KB with a generic 400 ("JSON could not be
# generated"). 200 IDs yields ~4 KB of filter, well under that ceiling and well
# under the supabase-py default 32 KB internal cap. Tuning down is safe; tuning
# up risks silent backfill failure on large sheets.
_GET_BATCH_CHUNK_SIZE = 200


class StateRepository:
    """
    Repository for row processing states.

    Provides data access methods for the imagetocell_row_states table.
    """

    def __init__(self, supabase: Optional[SupabaseService] = None):
        """
        Initialize repository.

        Args:
            supabase: Supabase service (uses singleton if not provided)
        """
        self._db = supabase or get_supabase_service()

    def get(self, config_id: str, row_id: int) -> Optional[RowState]:
        """
        Get state for a specific row.

        Args:
            config_id: Configuration ID
            row_id: Smartsheet row ID

        Returns:
            RowState or None if not found
        """
        try:
            # Use limit(1) instead of single/maybe_single to avoid errors
            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.ROW_STATES)
                    .select("*")
                    .eq("config_id", config_id)
                    .eq("row_id", row_id)
                    .limit(1)
                    .execute(),
                f"get row state {config_id}/{row_id}"
            )

            if result and result.data and len(result.data) > 0:
                return self._parse_row_state(result.data[0])
            return None
        except Exception as e:
            # Handle case where row state doesn't exist
            error_str = str(e)
            if "0 rows" in error_str or "PGRST116" in error_str:
                return None
            log.error(f"Error getting row state: {e}")
            raise

    def get_batch(
        self,
        config_id: str,
        row_ids: List[int],
    ) -> Dict[int, RowState]:
        """
        Get states for multiple rows.

        Implementation note
        -------------------
        PostgREST translates ``.in_("row_id", [...])`` into a
        ``row_id=in.(id1,id2,...)`` query-string filter. With 1000+ Smartsheet
        row IDs that string blows past Supabase Cloud's edge URL limit
        (~8 KB), causing a generic 400 "JSON could not be generated".
        Symptom: backfill instantly fails with 0 rows processed on any
        moderately-large sheet.

        We therefore page the request in chunks of ``_GET_BATCH_CHUNK_SIZE``
        and merge the results. Each chunk is a normal point-in-time read; the
        order of chunks does not matter because ``config_id`` + ``row_id`` is
        unique in the table.

        Args:
            config_id: Configuration ID
            row_ids: List of row IDs

        Returns:
            Dict mapping row_id to RowState
        """
        if not row_ids:
            return {}

        # De-duplicate while preserving deterministic chunk boundaries so
        # retried failures look identical in logs.
        unique_ids = sorted({int(rid) for rid in row_ids})
        total = len(unique_ids)

        states: Dict[int, RowState] = {}
        chunks_attempted = 0
        rows_returned = 0

        for start in range(0, total, _GET_BATCH_CHUNK_SIZE):
            chunk = unique_ids[start:start + _GET_BATCH_CHUNK_SIZE]
            chunk_strs = [str(rid) for rid in chunk]
            chunks_attempted += 1

            result = self._db.execute_with_retry(
                lambda c=chunk_strs: self._db.table(TABLE_NAMES.ROW_STATES)
                    .select("*")
                    .eq("config_id", config_id)
                    .in_("row_id", c)
                    .execute(),
                f"batch get {len(chunk)} row states "
                f"(chunk {chunks_attempted}, offset {start}/{total})"
            )

            data = (result.data or []) if result is not None else []
            rows_returned += len(data)
            for row in data:
                state = self._parse_row_state(row)
                states[state.row_id] = state

        if total > _GET_BATCH_CHUNK_SIZE:
            log.info(
                f"State batch read complete: requested={total} unique rows "
                f"chunks={chunks_attempted} (size={_GET_BATCH_CHUNK_SIZE}) "
                f"returned={rows_returned} hits={len(states)}"
            )

        return states

    def save(self, state: RowState) -> None:
        """
        Save or update a row state (upsert).

        Args:
            state: RowState to save
        """
        now = datetime.utcnow().isoformat()

        data = {
            "config_id": state.config_id,
            "row_id": state.row_id,
            "last_ts": state.last_ts,
            "last_ids": list(state.last_ids),
            "locked_columns": state.locked_columns,
            "failed_attempts": state.failed_attempts or {},
            "updated_at": now,
        }

        self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.ROW_STATES)
                .upsert(data, on_conflict="config_id,row_id")
                .execute(),
            f"save row state {state.config_id}/{state.row_id}"
        )

    def save_batch(self, states: List[RowState]) -> None:
        """
        Save multiple row states in a single operation.

        Args:
            states: List of RowStates to save
        """
        if not states:
            return

        now = datetime.utcnow().isoformat()

        data = [
            {
                "config_id": state.config_id,
                "row_id": state.row_id,
                "last_ts": state.last_ts,
                "last_ids": list(state.last_ids),
                "locked_columns": state.locked_columns,
                "failed_attempts": state.failed_attempts or {},
                "updated_at": now,
            }
            for state in states
        ]

        self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.ROW_STATES)
                .upsert(data, on_conflict="config_id,row_id")
                .execute(),
            f"batch save {len(states)} row states"
        )

    def delete(self, config_id: str, row_id: int) -> None:
        """
        Delete a row state.

        Args:
            config_id: Configuration ID
            row_id: Row ID
        """
        self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.ROW_STATES)
                .delete()
                .eq("config_id", config_id)
                .eq("row_id", row_id)
                .execute(),
            f"delete row state {config_id}/{row_id}"
        )

    def delete_for_config(self, config_id: str) -> int:
        """
        Delete all row states for a configuration.

        Args:
            config_id: Configuration ID

        Returns:
            Number of deleted rows
        """
        result = self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.ROW_STATES)
                .delete()
                .eq("config_id", config_id)
                .execute(),
            f"delete all row states for config {config_id}"
        )

        return len(result.data or [])

    def _parse_row_state(self, data: dict) -> RowState:
        """Parse database row into RowState model."""
        # Handle last_ids which may be stored as list
        last_ids = data.get("last_ids", [])
        if isinstance(last_ids, list):
            last_ids = set(last_ids)

        # failed_attempts is name_map-only; absent/null on legacy rows → {}
        failed_attempts = data.get("failed_attempts") or {}
        if not isinstance(failed_attempts, dict):
            failed_attempts = {}

        return RowState(
            config_id=data["config_id"],
            row_id=int(data["row_id"]),
            last_ts=float(data.get("last_ts", 0.0)),
            last_ids=last_ids,
            locked_columns=data.get("locked_columns", []),
            failed_attempts=failed_attempts,
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )
