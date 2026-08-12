"""
Configuration Validator
=======================

Validates all configurations on application startup.
Logs warnings for invalid or problematic configurations.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from src.config.constants import TABLE_NAMES, CONFIG_TYPES
from src.services.supabase.client import get_supabase_service
from src.utils.logging import FlowLogger, log_separator


log = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of configuration validation."""
    total_configs: int = 0
    valid_configs: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


class ConfigValidator:
    """
    Validates Image-to-Cell configurations.

    Checks:
    - Required fields are present
    - Column IDs are valid integers
    - Processing mode is valid
    - Organization has valid token
    """

    def __init__(self):
        self._db = get_supabase_service()

    def validate_all(self) -> ValidationResult:
        """
        Validate all active configurations.

        Returns:
            ValidationResult with counts and issues found
        """
        result = ValidationResult()
        flow = FlowLogger(log, "CONFIG:VALIDATE")

        flow.start("Validating all configurations")

        try:
            # Get all active configs
            configs = self._get_all_configs()
            result.total_configs = len(configs)

            flow.step(1, f"Found {len(configs)} configurations")

            if not configs:
                flow.success("No configurations to validate")
                return result

            # Validate each config
            for config in configs:
                issues = self._validate_config(config)

                if issues["errors"]:
                    result.errors.extend(issues["errors"])
                elif issues["warnings"]:
                    result.warnings.extend(issues["warnings"])
                    result.valid_configs += 1
                else:
                    result.valid_configs += 1

            # Log summary
            flow.step(2, "Validation complete")

            if result.errors:
                for error in result.errors:
                    log.error(f"[CONFIG:VALIDATE] {error}")

            if result.warnings:
                for warning in result.warnings:
                    log.warning(f"[CONFIG:VALIDATE] {warning}")

            flow.success(
                "Validation finished",
                total=result.total_configs,
                valid=result.valid_configs,
                warnings=len(result.warnings),
                errors=len(result.errors),
            )

        except Exception as e:
            flow.failure("Validation failed", e)
            result.errors.append(f"Validation failed: {e}")

        return result

    def _get_all_configs(self) -> List[Dict]:
        """Get all configurations from database."""
        try:
            response = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.CONFIG)
                    .select("*")
                    .execute(),
                "get all configs"
            )
            return response.data or []
        except Exception as e:
            log.error(f"Failed to get configs: {e}")
            return []

    def _validate_config(self, config: Dict) -> Dict[str, List[str]]:
        """
        Validate a single configuration.

        Returns:
            Dict with "errors" and "warnings" lists
        """
        issues = {"errors": [], "warnings": []}
        config_id = config.get("id", "unknown")[:8]
        config_name = config.get("configuration_name", "unnamed")

        # Check required fields
        required_fields = [
            "organization_id",
            "worksheet_id",
            "source_column_id",
            "target_column_id",
        ]

        for field in required_fields:
            if not config.get(field):
                issues["errors"].append(
                    f"Config '{config_name}' ({config_id}): missing required field '{field}'"
                )

        # Check processing mode
        mode = config.get("processing_mode")
        if mode and mode not in ["webhook", "polling"]:
            issues["warnings"].append(
                f"Config '{config_name}' ({config_id}): unknown processing_mode '{mode}'"
            )

        # Check config type
        config_type = config.get("config_type")
        valid_types = [CONFIG_TYPES.DEFAULT, CONFIG_TYPES.REPLACE, CONFIG_TYPES.NAME_MAP]
        if config_type and config_type not in valid_types:
            issues["warnings"].append(
                f"Config '{config_name}' ({config_id}): unknown config_type '{config_type}'"
            )

        # Check if paused for a long time
        if config.get("paused"):
            issues["warnings"].append(
                f"Config '{config_name}' ({config_id}): configuration is paused"
            )

        # Check column IDs are valid integers
        for col_field in ["source_column_id", "target_column_id"]:
            value = config.get(col_field)
            if value:
                try:
                    int(value)
                except (ValueError, TypeError):
                    issues["errors"].append(
                        f"Config '{config_name}' ({config_id}): {col_field} is not a valid integer"
                    )

        # Check worksheet_id is valid
        worksheet_id = config.get("worksheet_id")
        if worksheet_id:
            try:
                int(worksheet_id)
            except (ValueError, TypeError):
                issues["errors"].append(
                    f"Config '{config_name}' ({config_id}): worksheet_id is not a valid integer"
                )

        return issues

    def validate_organization_tokens(self) -> Dict[str, bool]:
        """
        Check if all organizations with configs have valid tokens.

        Returns:
            Dict mapping org_id to has_token status
        """
        result = {}

        try:
            # Get unique org IDs from configs
            response = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.CONFIG)
                    .select("organization_id")
                    .eq("paused", False)
                    .execute(),
                "get org ids"
            )

            org_ids = set(r.get("organization_id") for r in (response.data or []) if r.get("organization_id"))

            # Check each org has a token
            for org_id in org_ids:
                try:
                    token_response = self._db.execute_with_retry(
                        lambda: self._db.table(TABLE_NAMES.TOKENS)
                            .select("id")
                            .eq("organization_id", org_id)
                            .limit(1)
                            .execute(),
                        f"check token for {org_id[:8]}"
                    )
                    result[org_id] = bool(token_response.data)

                    if not token_response.data:
                        log.warning(
                            f"[CONFIG:VALIDATE] Organization {org_id[:8]} has no Smartsheet token"
                        )
                except Exception:
                    result[org_id] = False

        except Exception as e:
            log.error(f"Failed to validate org tokens: {e}")

        return result


def validate_configs_on_startup() -> ValidationResult:
    """
    Run configuration validation on startup.

    Call this during application lifespan startup.
    """
    log_separator(log, "CONFIGURATION VALIDATION")

    validator = ConfigValidator()
    result = validator.validate_all()

    # Also check tokens
    token_status = validator.validate_organization_tokens()
    orgs_without_tokens = sum(1 for has_token in token_status.values() if not has_token)

    if orgs_without_tokens:
        result.warnings.append(
            f"{orgs_without_tokens} organization(s) have no Smartsheet token"
        )

    if result.has_errors:
        log.error(
            f"[CONFIG:VALIDATE] Found {len(result.errors)} configuration errors"
        )
    elif result.has_warnings:
        log.warning(
            f"[CONFIG:VALIDATE] Found {len(result.warnings)} configuration warnings"
        )
    else:
        log.info(
            f"[CONFIG:VALIDATE] All {result.valid_configs} configurations are valid"
        )

    return result
