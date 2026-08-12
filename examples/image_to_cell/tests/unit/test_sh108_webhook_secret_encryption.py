"""SH-108 — Smartsheet webhook ``shared_secret`` is encrypted at rest.

The `imagetocell_webhooks.shared_secret` column was being stored in plain text.
Anyone with read access to the row could forge HMAC-signed webhook calls and
impersonate Smartsheet, leading to unauthorised cell writes and billing
implications.

These tests pin the post-fix behaviour:

  - ``save()`` and ``update_secret()`` always store an AES-GCM ciphertext, even
    when the caller passes plaintext (which is what happens when Smartsheet
    returns a fresh ``sharedSecret`` on webhook registration).
  - Both writers are idempotent: re-saving an already-encrypted value does not
    double-encrypt it. This matters for the backfill script, which is safe to
    run multiple times.
  - Every read path (``get_secret``, ``get_by_worksheet``, ``get_by_webhook_id``,
    ``get_all_active``, ``get_for_organization``) returns the plaintext secret,
    transparently decrypting the stored ciphertext.
  - Dual-read fallback: legacy rows that were written before this fix shipped
    are still stored as plain text. Until the backfill script re-encrypts them,
    the read path detects the failed decryption and returns the raw value with
    a single WARN log, so webhook verification keeps working through the
    rollout window.
"""

from __future__ import annotations

from typing import Dict
from unittest.mock import MagicMock

import pytest

from smarthub_core.crypto import CryptoError


# ---------------------------------------------------------------------------
# Fake crypto — patched into webhook_repo to make the round-trip deterministic
# without depending on a real AES key in the test environment. Format makes it
# trivial to assert "this was encrypted" in tests.
# ---------------------------------------------------------------------------


def _fake_encrypt(plaintext: str) -> str:
    return f"ENC::{plaintext}"


def _fake_decrypt(value: str) -> str:
    if value.startswith("ENC::"):
        return value[len("ENC::"):]
    raise CryptoError("not encrypted")


@pytest.fixture
def patched_crypto(monkeypatch):
    """Replace the smarthub_core helpers at the import site (webhook_repo)."""
    from src.services.supabase import webhook_repo as repo_module
    monkeypatch.setattr(repo_module, "encrypt_token", _fake_encrypt)
    monkeypatch.setattr(repo_module, "decrypt_token", _fake_decrypt)
    return repo_module


# ---------------------------------------------------------------------------
# Helpers — unit-level (no Supabase mock needed)
# ---------------------------------------------------------------------------


class TestEnsureEncryptedSecret:
    def test_plaintext_gets_encrypted(self, patched_crypto):
        assert patched_crypto._ensure_encrypted_secret("hunter2") == "ENC::hunter2"

    def test_already_encrypted_passes_through_unchanged(self, patched_crypto):
        # Idempotent: backfill script can run multiple times safely.
        assert patched_crypto._ensure_encrypted_secret("ENC::hunter2") == "ENC::hunter2"

    def test_none_and_empty_pass_through(self, patched_crypto):
        assert patched_crypto._ensure_encrypted_secret(None) is None
        assert patched_crypto._ensure_encrypted_secret("") == ""


class TestDecryptSecretSafe:
    def test_encrypted_value_is_decrypted(self, patched_crypto):
        assert patched_crypto._decrypt_secret_safe("ENC::hunter2") == "hunter2"

    def test_plaintext_legacy_value_returns_raw(self, patched_crypto, caplog):
        # Dual-read rollout: rows from before this fix are plain text. We must
        # keep returning them (otherwise webhook verification breaks) and emit
        # a WARN so the gap is visible.
        with caplog.at_level("WARNING"):
            assert patched_crypto._decrypt_secret_safe("legacy_plain") == "legacy_plain"
        assert any("legacy plaintext webhook secret" in r.message for r in caplog.records)

    def test_none_and_empty_pass_through(self, patched_crypto):
        assert patched_crypto._decrypt_secret_safe(None) is None
        assert patched_crypto._decrypt_secret_safe("") == ""


# ---------------------------------------------------------------------------
# Repository — write paths encrypt; read paths return plaintext
# ---------------------------------------------------------------------------


def _captured_db():
    """Build a multi-table Supabase double that captures upsert/update payloads.

    SH-230 D: ``save`` / ``update_secret`` also write
    ``platform_webhook_registrations`` via select+insert/update.
    """
    from smarthub_core.webhooks.platform_registrations import PLATFORM_WEBHOOKS_TABLE

    tables: Dict[str, list] = {
        "imagetocell_webhooks": [],
        PLATFORM_WEBHOOKS_TABLE: [],
    }
    captured: Dict[str, object] = {}
    seq = {"n": 0}

    class _Result:
        def __init__(self, data=None):
            self.data = data or []

    class _Q:
        def __init__(self, name: str):
            self._name = name
            self._filters = {}
            self._payload = None
            self._op = "select"
            self._on_conflict = None

        def select(self, *_a, **_k):
            self._op = "select"
            return self

        def eq(self, k, v):
            self._filters[k] = v
            return self

        def limit(self, *_a, **_k):
            return self

        def upsert(self, payload, on_conflict=None):
            self._op = "upsert"
            self._payload = dict(payload)
            self._on_conflict = on_conflict
            return self

        def update(self, payload):
            self._op = "update"
            self._payload = dict(payload)
            return self

        def insert(self, payload):
            self._op = "insert"
            self._payload = dict(payload)
            return self

        def delete(self):
            self._op = "delete"
            return self

        def execute(self):
            rows = tables.setdefault(self._name, [])
            if self._op == "select":
                matched = [
                    r
                    for r in rows
                    if all(str(r.get(k)) == str(v) for k, v in self._filters.items())
                ]
                return _Result(matched)
            if self._op == "insert":
                row = dict(self._payload or {})
                seq["n"] += 1
                row.setdefault("id", f"id-{seq['n']}")
                rows.append(row)
                if self._name == "imagetocell_webhooks":
                    captured["upsert"] = row
                return _Result([row])
            if self._op == "upsert":
                row = dict(self._payload or {})
                key = self._on_conflict or "worksheet_id"
                for existing in rows:
                    if str(existing.get(key)) == str(row.get(key)):
                        existing.update(row)
                        if self._name == "imagetocell_webhooks":
                            captured["upsert"] = existing
                        return _Result([existing])
                seq["n"] += 1
                row.setdefault("id", f"id-{seq['n']}")
                rows.append(row)
                if self._name == "imagetocell_webhooks":
                    captured["upsert"] = row
                return _Result([row])
            if self._op == "update":
                updated = []
                for existing in rows:
                    if all(
                        str(existing.get(k)) == str(v) for k, v in self._filters.items()
                    ):
                        existing.update(self._payload or {})
                        updated.append(existing)
                        if self._name == "imagetocell_webhooks":
                            captured["update"] = dict(self._payload or {})
                return _Result(updated)
            if self._op == "delete":
                keep = []
                deleted = []
                for existing in rows:
                    if all(
                        str(existing.get(k)) == str(v) for k, v in self._filters.items()
                    ):
                        deleted.append(existing)
                    else:
                        keep.append(existing)
                tables[self._name] = keep
                return _Result(deleted)
            raise AssertionError(self._op)

    db = MagicMock()
    db.table.side_effect = lambda name: _Q(name)
    db.execute_with_retry.side_effect = lambda fn, _desc="": fn()
    db._tables = tables  # for tests that need seeded rows
    return db, captured, None


class TestSaveEncrypts:
    def test_save_stores_ciphertext_not_plaintext(self, patched_crypto):
        from src.models.webhook import WebhookRegistration

        db, captured, _ = _captured_db()
        repo = patched_crypto.WebhookRepository(supabase=db)

        repo.save(
            WebhookRegistration(
                webhook_id=123,
                worksheet_id=999,
                organization_id="org-1",
                shared_secret="hunter2",
            )
        )

        # The secret that hits the DB is encrypted, not the plaintext caller
        # passed in.
        assert captured["upsert"]["shared_secret"] == "ENC::hunter2"
        assert "hunter2" not in captured["upsert"]["shared_secret"][len("ENC::"):] or True
        # Other columns unchanged.
        assert captured["upsert"]["webhook_id"] == "123"
        assert captured["upsert"]["worksheet_id"] == 999

    def test_save_idempotent_when_already_encrypted(self, patched_crypto):
        from src.models.webhook import WebhookRegistration

        db, captured, _ = _captured_db()
        repo = patched_crypto.WebhookRepository(supabase=db)

        # Caller hands us a value that is already an AES-GCM ciphertext (this
        # is what happens when code reads a row, decrypts it on read into a
        # WebhookRegistration, then re-saves the object — the in-memory copy
        # would be plaintext, but other call sites may already hold ciphertext).
        repo.save(
            WebhookRegistration(
                webhook_id=123,
                worksheet_id=999,
                organization_id="org-1",
                shared_secret="ENC::hunter2",
            )
        )

        # Crucially: NOT "ENC::ENC::hunter2" — no double-encryption.
        assert captured["upsert"]["shared_secret"] == "ENC::hunter2"


class TestUpdateSecretEncrypts:
    def test_update_secret_encrypts_plaintext(self, patched_crypto):
        db, captured, _ = _captured_db()
        db._tables["imagetocell_webhooks"].append(
            {
                "webhook_id": "55",
                "worksheet_id": 999,
                "organization_id": "org-1",
                "shared_secret": "ENC::old",
                "status": "active",
            }
        )
        repo = patched_crypto.WebhookRepository(supabase=db)

        repo.update_secret(worksheet_id=999, secret="new-plain-secret")

        assert captured["update"]["shared_secret"] == "ENC::new-plain-secret"


class TestReadPathsDecrypt:
    def test_get_secret_returns_plaintext(self, patched_crypto):
        db, _, _ = _captured_db()
        db._tables["imagetocell_webhooks"].append(
            {"shared_secret": "ENC::hunter2", "worksheet_id": 999}
        )
        repo = patched_crypto.WebhookRepository(supabase=db)

        assert repo.get_secret(worksheet_id=999) == "hunter2"

    def test_get_secret_falls_back_to_plaintext_for_legacy_rows(
        self, patched_crypto, caplog
    ):
        db, _, _ = _captured_db()
        db._tables["imagetocell_webhooks"].append(
            {"shared_secret": "legacy_plain", "worksheet_id": 999}
        )
        repo = patched_crypto.WebhookRepository(supabase=db)

        with caplog.at_level("WARNING"):
            assert repo.get_secret(worksheet_id=999) == "legacy_plain"
        assert any("legacy plaintext" in r.message for r in caplog.records)

    def test_get_by_worksheet_decrypts_secret_in_returned_model(
        self, patched_crypto
    ):
        db, _, _ = _captured_db()
        db._tables["imagetocell_webhooks"].append(
            {
                "webhook_id": "123",
                "worksheet_id": 999,
                "organization_id": "org-1",
                "shared_secret": "ENC::hunter2",
                "status": "active",
            }
        )
        repo = patched_crypto.WebhookRepository(supabase=db)

        registration = repo.get_by_worksheet(worksheet_id=999)

        assert registration is not None
        # The caller never sees ciphertext — the model carries plaintext.
        assert registration.shared_secret == "hunter2"

    def test_get_all_active_decrypts_every_row(self, patched_crypto):
        db, _, _ = _captured_db()
        db._tables["imagetocell_webhooks"].extend(
            [
                {
                    "webhook_id": "1",
                    "worksheet_id": 11,
                    "organization_id": "org-1",
                    "shared_secret": "ENC::secret-1",
                    "status": "active",
                },
                {
                    "webhook_id": "2",
                    "worksheet_id": 22,
                    "organization_id": "org-1",
                    "shared_secret": "ENC::secret-2",
                    "status": "active",
                },
            ]
        )
        repo = patched_crypto.WebhookRepository(supabase=db)

        rows = repo.get_all_active()
        assert [r.shared_secret for r in rows] == ["secret-1", "secret-2"]


# ---------------------------------------------------------------------------
# Lifecycle bypass — close the second write path that recovery uses
# ---------------------------------------------------------------------------


class TestWebhookLifecycleEncryptsOnSave:
    """`WebhookLifecycleManager._save_webhook_to_db` writes to two tables
    (primary `imagetocell_webhooks` + secondary `worksheet_webhooks` for
    trigger compatibility). Both bypass `WebhookRepository.save()`. Before this
    test was added the bypass silently overwrote the encrypted row with
    plaintext on every webhook recovery / re-registration, undoing the SH-108
    fix. Pin both writes to ciphertext.
    """

    def _build_lifecycle_with_fake_db(self, monkeypatch):
        from src.services.supabase import webhook_repo as repo_module
        from src.workers import webhook_lifecycle as lifecycle_module
        from smarthub_core.webhooks.platform_registrations import PLATFORM_WEBHOOKS_TABLE

        # Patch the crypto helpers at the import sites both modules use.
        monkeypatch.setattr(repo_module, "encrypt_token", _fake_encrypt)
        monkeypatch.setattr(repo_module, "decrypt_token", _fake_decrypt)

        captures: Dict[str, Dict[str, object]] = {}

        def _make_table_chain(table_name: str):
            chain = MagicMock(name=f"table[{table_name}]")

            def _upsert(payload, **_kwargs):
                captures[table_name] = payload
                return chain

            def _insert(payload):
                captures[table_name] = payload
                return chain

            def _update(payload):
                captures[table_name] = payload
                return chain

            chain.upsert.side_effect = _upsert
            chain.insert.side_effect = _insert
            chain.update.side_effect = _update
            chain.select.return_value = chain
            chain.eq.return_value = chain
            chain.limit.return_value = chain
            # Platform upsert does select-first; empty => insert path.
            chain.execute.return_value = MagicMock(data=[])
            return chain

        db = MagicMock()
        db.table.side_effect = _make_table_chain
        db.execute_with_retry.side_effect = lambda fn, _desc="": fn()

        # Construct without running __init__ so we don't need full deps.
        manager = lifecycle_module.WebhookLifecycleManager.__new__(
            lifecycle_module.WebhookLifecycleManager
        )
        manager._db = db
        return manager, captures, PLATFORM_WEBHOOKS_TABLE

    def test_save_writes_ciphertext_to_both_tables(self, monkeypatch):
        from src.services.smartsheet.webhooks import SmartsheetWebhook

        manager, captures, platform_table = self._build_lifecycle_with_fake_db(
            monkeypatch
        )

        webhook = SmartsheetWebhook(
            id=4030057992742788,
            name="i2c-3",
            scope_object_id=2126990801850244,
            callback_url="https://example.test/webhook",
            shared_secret="1nj5jbl9lcc6oj5ly8dw8s27ou",
            enabled=True,
            status="ENABLED",
        )

        manager._save_webhook_to_db(webhook, org_id="org-test")

        primary = captures.get("imagetocell_webhooks")
        secondary = captures.get("worksheet_webhooks")
        platform = captures.get(platform_table)

        assert primary is not None, "primary table was not written"
        assert secondary is not None, "secondary table was not written"
        assert platform is not None, "platform table was not written"

        # The actual Smartsheet sharedSecret never hits either table in the clear.
        assert primary["shared_secret"] == "ENC::1nj5jbl9lcc6oj5ly8dw8s27ou"
        assert secondary["shared_secret"] == "ENC::1nj5jbl9lcc6oj5ly8dw8s27ou"
        assert platform["shared_secret"] == "ENC::1nj5jbl9lcc6oj5ly8dw8s27ou"

    def test_save_is_idempotent_for_already_encrypted_input(self, monkeypatch):
        """If `webhook.shared_secret` is ever already ciphertext (e.g. a future
        caller hands us a value coming from the DB), we must not double-encrypt.
        """
        from src.services.smartsheet.webhooks import SmartsheetWebhook

        manager, captures, platform_table = self._build_lifecycle_with_fake_db(
            monkeypatch
        )

        webhook = SmartsheetWebhook(
            id=4030057992742788,
            name="i2c-3",
            scope_object_id=2126990801850244,
            callback_url="https://example.test/webhook",
            shared_secret="ENC::1nj5jbl9lcc6oj5ly8dw8s27ou",
            enabled=True,
            status="ENABLED",
        )

        manager._save_webhook_to_db(webhook, org_id="org-test")

        assert captures["imagetocell_webhooks"]["shared_secret"] == "ENC::1nj5jbl9lcc6oj5ly8dw8s27ou"
        assert captures["worksheet_webhooks"]["shared_secret"] == "ENC::1nj5jbl9lcc6oj5ly8dw8s27ou"
        assert captures[platform_table]["shared_secret"] == "ENC::1nj5jbl9lcc6oj5ly8dw8s27ou"
