"""
Webhook Repository
==================

Data access for webhook registrations.

SH-230 D: ``platform_webhook_registrations`` is the authoritative write SoT
(required). ``imagetocell_webhooks`` remains a soft-fail mirror for rollback
until a follow-up retires the tool table.

SH-108: the Smartsheet webhook `shared_secret` (used to verify the HMAC of every
incoming webhook event) was stored as plain text. Anyone with read access to the
DB row could forge signed webhook calls. This module now encrypts the secret
with the same AES-256-GCM key (`APP_CRYPTO_KEY`) used elsewhere for Smartsheet
PATs, transparently to callers. A dual-read fallback decrypts legacy plaintext
rows during the rollout window so old data keeps working until the backfill
script (`scripts/migrate_encrypt_webhook_secrets.py`) re-saves them encrypted.
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime

from smarthub_core.crypto import CryptoError, decrypt_token, encrypt_token
from smarthub_core.webhooks.platform_registrations import (
    DEFAULT_PROVIDER,
    delete_platform_registration,
    delete_platform_registration_by_worksheet,
    upsert_platform_registration,
)

from src.config.constants import PRODUCT_SLUG, TABLE_NAMES
from src.models.webhook import WebhookRegistration
from src.services.supabase.client import SupabaseService, get_supabase_service


log = logging.getLogger(__name__)


def _ensure_encrypted_secret(secret: Optional[str]) -> Optional[str]:
    """Return an AES-GCM ciphertext representation of ``secret``, idempotent.

    Strategy: try to decrypt the input. If it decrypts cleanly the value is
    already encrypted and we pass it through. If decryption raises
    ``CryptoError`` we treat the input as plaintext and encrypt it with the
    current key.

    This makes ``save()`` safe to call with either plaintext (new registrations
    coming back from the Smartsheet API) or already-encrypted values (re-saving
    a row that was decrypted on read), and it lets the backfill script run
    multiple times without re-encrypting rows that are already encrypted.
    """
    if not secret:
        return secret
    try:
        decrypt_token(secret)
        return secret
    except CryptoError:
        return encrypt_token(secret)


def _decrypt_secret_safe(secret: Optional[str]) -> Optional[str]:
    """Decrypt ``secret``, returning plaintext on legacy unencrypted rows.

    SH-108 rollout: rows written before this fix shipped are still stored in
    plain text. Refusing to read them would break webhook signature validation
    for every existing Smartsheet integration. Until the backfill script
    re-saves them encrypted, this function falls back to returning the stored
    value as-is and emits a single WARN log so the gap is visible in
    operational dashboards.
    """
    if not secret:
        return secret
    try:
        return decrypt_token(secret)
    except CryptoError:
        log.warning(
            "[SH-108] legacy plaintext webhook secret encountered — will be "
            "re-encrypted on next save (or by the backfill script). "
            "Continuing to honour the value to avoid breaking webhook verification."
        )
        return secret


def _registration_from_row(row: Dict[str, Any]) -> WebhookRegistration:
    """Build a WebhookRegistration from a DB row, decrypting the secret.

    Centralised so every read path (`get_by_worksheet`, `get_by_webhook_id`,
    `get_all_active`, `get_for_organization`) goes through the same decryption
    logic — no caller can forget it.
    """
    if "shared_secret" in row and row["shared_secret"]:
        row = dict(row)
        row["shared_secret"] = _decrypt_secret_safe(row["shared_secret"])
    return WebhookRegistration(**row)


class WebhookRepository:
    """
    Repository for webhook registrations.

    Writes platform_webhook_registrations first (required), then mirrors into
    imagetocell_webhooks (soft-fail).
    """

    def __init__(self, supabase: Optional[SupabaseService] = None):
        """
        Initialize repository.

        Args:
            supabase: Supabase service (uses singleton if not provided)
        """
        self._db = supabase or get_supabase_service()

    def get_by_worksheet(self, worksheet_id: int) -> Optional[WebhookRegistration]:
        """
        Get webhook registration for a worksheet.

        Args:
            worksheet_id: Smartsheet worksheet ID

        Returns:
            WebhookRegistration or None if not found
        """
        try:
            # Use .limit(1) instead of .single() to avoid error when no row exists
            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .select("*")
                    .eq("worksheet_id", worksheet_id)
                    .limit(1)
                    .execute(),
                f"get webhook for worksheet {worksheet_id}"
            )

            if result.data and len(result.data) > 0:
                return _registration_from_row(result.data[0])
            return None
        except Exception as e:
            log.debug(f"Error getting webhook for worksheet {worksheet_id}: {e}")
            return None

    def get_by_worksheet_all_orgs(self, worksheet_id: int) -> List[Dict[str, Any]]:
        """
        SH-178: get ALL webhook rows for a worksheet across every org.

        Used to identify webhooks owned by OTHER orgs on the same sheet
        (visible because one Smartsheet user can back multiple orgs) so
        registration/cleanup never deletes them. Returns raw rows with
        ``webhook_id`` and ``organization_id`` — no secrets are decrypted.

        Raises on DB failure so callers can fail closed.
        """
        result = self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                .select("webhook_id, organization_id, worksheet_id")
                .eq("worksheet_id", worksheet_id)
                .execute(),
            f"get all-org webhooks for worksheet {worksheet_id}"
        )
        return result.data or []

    def get_by_webhook_id(self, webhook_id: int) -> Optional[WebhookRegistration]:
        """
        Get webhook registration by Smartsheet webhook ID.

        Args:
            webhook_id: Smartsheet webhook ID

        Returns:
            WebhookRegistration or None if not found
        """
        try:
            # Use .limit(1) instead of .single() to avoid error when no row exists
            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .select("*")
                    .eq("webhook_id", str(webhook_id))
                    .limit(1)
                    .execute(),
                f"get webhook {webhook_id}"
            )

            if result.data and len(result.data) > 0:
                return _registration_from_row(result.data[0])
            return None
        except Exception as e:
            log.debug(f"Error getting webhook {webhook_id}: {e}")
            return None

    def get_secret(self, worksheet_id: int) -> Optional[str]:
        """
        Get the shared secret for a worksheet's webhook.

        Args:
            worksheet_id: Smartsheet worksheet ID

        Returns:
            Shared secret or None if not found
        """
        try:
            # Use .limit(1) instead of .single() to avoid error when no row exists
            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .select("shared_secret")
                    .eq("worksheet_id", worksheet_id)
                    .limit(1)
                    .execute(),
                f"get webhook secret for {worksheet_id}"
            )

            if result.data and len(result.data) > 0:
                return _decrypt_secret_safe(result.data[0].get("shared_secret"))
            return None
        except Exception as e:
            log.debug(f"Error getting secret for worksheet {worksheet_id}: {e}")
            return None

    def _mirror_tool_upsert(self, data: Dict[str, Any], worksheet_id: int) -> None:
        try:
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .upsert(data, on_conflict="worksheet_id")
                    .execute(),
                f"mirror tool webhook for worksheet {worksheet_id}"
            )
        except Exception as exc:
            log.warning(
                "[WEBHOOK:DB] tool-table mirror soft-fail sheet=%s: %s",
                worksheet_id,
                exc,
            )

    def save(self, registration: WebhookRegistration) -> None:
        """
        Save or update a webhook registration.

        Platform write is required; imagetocell_webhooks is soft-fail mirrored.
        """
        now = datetime.utcnow().isoformat()
        encrypted = _ensure_encrypted_secret(registration.shared_secret)

        upsert_platform_registration(
            self._db,
            provider=DEFAULT_PROVIDER,
            tool_slug=PRODUCT_SLUG,
            organization_id=registration.organization_id,
            worksheet_id=int(registration.worksheet_id),
            webhook_id=int(registration.webhook_id),
            shared_secret_ciphertext=encrypted,
            callback_url=registration.webhook_url,
            status=registration.status or "active",
            soft_fail=False,
        )

        data = {
            "webhook_id": str(registration.webhook_id),
            "worksheet_id": registration.worksheet_id,
            "organization_id": registration.organization_id,
            "shared_secret": encrypted,
            "status": registration.status,
            "updated_at": now,
        }
        if registration.webhook_url:
            data["webhook_url"] = registration.webhook_url
        self._mirror_tool_upsert(data, registration.worksheet_id)

    def update_secret(self, worksheet_id: int, secret: str) -> None:
        """
        Update the shared secret for a webhook.

        Loads identity from the tool table (or fails closed if missing) so the
        platform row can be updated by the identity key.
        """
        now = datetime.utcnow().isoformat()
        encrypted = _ensure_encrypted_secret(secret)

        existing = self.get_by_worksheet(worksheet_id)
        if existing is None:
            raise ValueError(
                f"Cannot update webhook secret: no registration for sheet {worksheet_id}"
            )

        upsert_platform_registration(
            self._db,
            provider=DEFAULT_PROVIDER,
            tool_slug=PRODUCT_SLUG,
            organization_id=existing.organization_id,
            worksheet_id=int(worksheet_id),
            webhook_id=int(existing.webhook_id),
            shared_secret_ciphertext=encrypted,
            callback_url=existing.webhook_url,
            status=existing.status or "active",
            soft_fail=False,
        )

        try:
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .update({
                        "shared_secret": encrypted,
                        "updated_at": now,
                    })
                    .eq("worksheet_id", worksheet_id)
                    .execute(),
                f"mirror update webhook secret for {worksheet_id}"
            )
        except Exception as exc:
            log.warning(
                "[WEBHOOK:DB] tool-table secret mirror soft-fail sheet=%s: %s",
                worksheet_id,
                exc,
            )

    def update_status(self, worksheet_id: int, status: str) -> None:
        """
        Update webhook status.

        Args:
            worksheet_id: Worksheet ID
            status: New status
        """
        now = datetime.utcnow().isoformat()
        existing = self.get_by_worksheet(worksheet_id)
        if existing is not None:
            try:
                upsert_platform_registration(
                    self._db,
                    provider=DEFAULT_PROVIDER,
                    tool_slug=PRODUCT_SLUG,
                    organization_id=existing.organization_id,
                    worksheet_id=int(worksheet_id),
                    webhook_id=int(existing.webhook_id),
                    status=status,
                    soft_fail=False,
                )
            except Exception as exc:
                log.error(
                    "[WEBHOOK:DB] platform status update failed sheet=%s: %s",
                    worksheet_id,
                    exc,
                )
                raise

        try:
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .update({"status": status, "updated_at": now})
                    .eq("worksheet_id", worksheet_id)
                    .execute(),
                f"update webhook status for {worksheet_id}"
            )
        except Exception as exc:
            log.warning(
                "[WEBHOOK:DB] tool-table status mirror soft-fail sheet=%s: %s",
                worksheet_id,
                exc,
            )

    def record_event(self, worksheet_id: int) -> None:
        """
        Record that an event was received for this webhook.

        Args:
            worksheet_id: Worksheet ID
        """
        now = datetime.utcnow().isoformat()

        self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                .update({"updated_at": now})
                .eq("worksheet_id", worksheet_id)
                .execute(),
            f"record webhook event for {worksheet_id}"
        )

    def delete(self, worksheet_id: int) -> None:
        """
        Delete a webhook registration.

        Args:
            worksheet_id: Worksheet ID
        """
        existing = self.get_by_worksheet(worksheet_id)
        if existing is not None:
            delete_platform_registration_by_worksheet(
                self._db,
                provider=DEFAULT_PROVIDER,
                tool_slug=PRODUCT_SLUG,
                organization_id=existing.organization_id,
                worksheet_id=int(worksheet_id),
                soft_fail=False,
            )
        else:
            log.warning(
                "[WEBHOOK:DB] delete: no tool row for sheet=%s — skipping "
                "platform identity delete",
                worksheet_id,
            )

        try:
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .delete()
                    .eq("worksheet_id", worksheet_id)
                    .execute(),
                f"delete webhook for {worksheet_id}"
            )
        except Exception as exc:
            log.warning(
                "[WEBHOOK:DB] tool-table delete soft-fail sheet=%s: %s",
                worksheet_id,
                exc,
            )

    def delete_by_webhook_id(self, webhook_id: int) -> None:
        """
        Delete a webhook registration by Smartsheet webhook ID.

        Args:
            webhook_id: Smartsheet webhook ID
        """
        delete_platform_registration(
            self._db,
            provider=DEFAULT_PROVIDER,
            tool_slug=PRODUCT_SLUG,
            webhook_id=int(webhook_id),
            soft_fail=False,
        )

        try:
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .delete()
                    .eq("webhook_id", str(webhook_id))
                    .execute(),
                f"delete webhook {webhook_id}"
            )
        except Exception as exc:
            log.warning(
                "[WEBHOOK:DB] tool-table delete soft-fail webhook=%s: %s",
                webhook_id,
                exc,
            )

    def get_all_active(self) -> List[WebhookRegistration]:
        """
        Get all active webhook registrations.

        Returns:
            List of active webhook registrations
        """
        result = self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                .select("*")
                .eq("status", "active")
                .execute(),
            "get all active webhooks"
        )

        return [_registration_from_row(row) for row in (result.data or [])]

    def get_for_organization(self, organization_id: str) -> List[WebhookRegistration]:
        """
        Get all webhooks for an organization.

        Args:
            organization_id: Organization ID

        Returns:
            List of webhook registrations
        """
        result = self._db.execute_with_retry(
            lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                .select("*")
                .eq("organization_id", organization_id)
                .execute(),
            f"get webhooks for org {organization_id}"
        )

        return [_registration_from_row(row) for row in (result.data or [])]
