import base64
import hashlib
import os

import pytest

import oauth_smartsheet as om


KEY = b"0" * 32


# ── AAD-bound crypto ─────────────────────────────────────────────────

def test_encrypt_decrypt_roundtrip_with_aad():
    aad = om._aad("org1", "user1", "tool1", "access")
    ct = om.encrypt_bound(KEY, "secret-token", aad)
    assert om.decrypt_bound(KEY, ct, aad) == "secret-token"


def test_decrypt_fails_with_wrong_context():
    aad = om._aad("org1", "user1", "tool1", "access")
    ct = om.encrypt_bound(KEY, "secret-token", aad)
    wrong = om._aad("org1", "user1", "tool2", "access")
    with pytest.raises(Exception):
        om.decrypt_bound(KEY, ct, wrong)


def test_decrypt_fails_with_wrong_kind():
    aad = om._aad("org1", "user1", "tool1", "access")
    ct = om.encrypt_bound(KEY, "secret-token", aad)
    with pytest.raises(Exception):
        om.decrypt_bound(KEY, ct, om._aad("org1", "user1", "tool1", "refresh"))


def test_ciphertext_is_base64_iv_plus_ct():
    aad = om._aad("o", "u", "t", "access")
    blob = base64.b64decode(om.encrypt_bound(KEY, "x", aad))
    # 12-byte IV + 1 byte plaintext + 16 byte GCM tag
    assert len(blob) == 12 + 1 + 16


# ── Scope map ────────────────────────────────────────────────────────

def test_every_tool_has_scopes():
    for slug, scopes in om.TOOL_SCOPES.items():
        assert scopes, f"{slug} has empty scopes"
        assert all(s.isupper() for s in scopes)


def test_required_tool_slugs_are_covered_at_import():
    """Boot-time assert: every live slug has an explicit TOOL_SCOPES entry."""
    om.assert_tool_scopes_complete()
    assert om.REQUIRED_TOOL_SLUGS <= frozenset(om.TOOL_SCOPES.keys())


def test_assert_tool_scopes_complete_fails_when_a_required_slug_is_missing():
    with pytest.raises(RuntimeError, match="smart-qr"):
        om.assert_tool_scopes_complete(
            required=frozenset({"smart-qr"}),
            scopes={"column-id-checker": ["READ_SHEETS"]},
        )


def test_unknown_tool_gets_default_readonly():
    assert om.scopes_for_tool("not-a-tool") == ["READ_SHEETS"]


def test_attachment_alias_matches_canonical():
    assert om.scopes_for_tool("attachment-info") == om.scopes_for_tool("count-my-attachments")


def test_smart_qr_can_write_cells_and_manage_webhooks():
    """SmartQR writes cell images and registers its own sheet webhooks."""
    assert set(om.scopes_for_tool("smart-qr")) == {
        "READ_SHEETS", "WRITE_SHEETS", "ADMIN_WEBHOOKS",
    }


def test_single_cell_audit_has_write_admin_webhook_and_read_users_scopes():
    """SH-226: READ_USERS needed for Changed By name/email resolution."""
    scopes = set(om.scopes_for_tool("single-cell-audit"))
    assert {
        "READ_SHEETS", "WRITE_SHEETS", "ADMIN_SHEETS", "ADMIN_WEBHOOKS",
        "READ_USERS",
    } <= scopes


def test_missing_scopes_for_tool_detects_under_scoped_token():
    missing = om.missing_scopes_for_tool("single-cell-audit", ["READ_SHEETS"])
    assert missing == [
        "ADMIN_SHEETS", "ADMIN_WEBHOOKS", "READ_USERS", "WRITE_SHEETS",
    ]


def test_missing_scopes_flags_read_users_when_only_sh201_scopes_present():
    """Tokens issued after SH-201 still need reconnect for SH-226."""
    have = ["READ_SHEETS", "WRITE_SHEETS", "ADMIN_SHEETS", "ADMIN_WEBHOOKS"]
    assert om.missing_scopes_for_tool("single-cell-audit", have) == ["READ_USERS"]


def test_missing_scopes_for_tool_empty_when_sufficient():
    have = om.scopes_for_tool("single-cell-audit")
    assert om.missing_scopes_for_tool("single-cell-audit", have) == []


def test_tools_status_payload_flags_under_scoped_active_token():
    tools = om._tools_status_payload(
        [
            {
                "product_slug": "single-cell-audit",
                "status": "active",
                "scopes": ["READ_SHEETS"],
                "expires_at": "2026-08-01T00:00:00Z",
                "last_refreshed_at": "2026-07-28T00:00:00Z",
            }
        ]
    )
    assert tools[0]["needs_reconnect"] is True
    assert "WRITE_SHEETS" in tools[0]["missing_scopes"]
    assert "ADMIN_SHEETS" in tools[0]["missing_scopes"]


def test_tools_status_payload_ok_when_scopes_match():
    scopes = om.scopes_for_tool("single-cell-audit")
    tools = om._tools_status_payload(
        [
            {
                "product_slug": "single-cell-audit",
                "status": "active",
                "scopes": scopes,
                "expires_at": "2026-08-01T00:00:00Z",
                "last_refreshed_at": "2026-07-28T00:00:00Z",
            }
        ]
    )
    assert tools[0]["needs_reconnect"] is False
    assert tools[0]["missing_scopes"] == []


# ── Token endpoint helpers (Smartsheet hash scheme) ──────────────────

class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_exchange_sends_secret_hash_not_secret(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse(200, {
            "access_token": "at", "refresh_token": "rt", "expires_in": 604799,
        })

    monkeypatch.setattr(om.requests, "post", fake_post)
    monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_ID_US", "cid")
    monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_SECRET_US", "csecret")

    ok, body = om.exchange_authorization_code("https://api.smartsheet.com/2.0", "US", "authcode")
    assert ok and body["access_token"] == "at"
    assert captured["url"] == "https://api.smartsheet.com/2.0/token"
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["client_id"] == "cid"
    expected_hash = hashlib.sha256(b"csecret|authcode").hexdigest()
    assert captured["data"]["hash"] == expected_hash
    assert "client_secret" not in captured["data"]


def test_refresh_uses_refresh_grant(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["data"] = data
        return _FakeResponse(200, {
            "access_token": "new-at", "refresh_token": "new-rt", "expires_in": 604799,
        })

    monkeypatch.setattr(om.requests, "post", fake_post)
    monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_ID_EU", "cid-eu")
    monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_SECRET_EU", "cs-eu")

    ok, body = om.refresh_token_pair("https://api.smartsheet.eu/2.0", "EU", "old-rt")
    assert ok and body["refresh_token"] == "new-rt"
    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["refresh_token"] == "old-rt"
    assert captured["data"]["hash"] == hashlib.sha256(b"cs-eu|old-rt").hexdigest()


def test_exchange_unconfigured_region_fails(monkeypatch):
    monkeypatch.delenv("SMARTSHEET_OAUTH_CLIENT_ID_US", raising=False)
    monkeypatch.delenv("SMARTSHEET_OAUTH_CLIENT_SECRET_US", raising=False)
    ok, body = om.exchange_authorization_code("https://api.smartsheet.com/2.0", "US", "code")
    assert not ok
    assert body["error_code"] == "REGION_UNSUPPORTED"


def test_exchange_smartsheet_rejection(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(400, {"error": "invalid_grant", "errorCode": 1004})

    monkeypatch.setattr(om.requests, "post", fake_post)
    monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_ID_US", "cid")
    monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_SECRET_US", "cs")

    ok, body = om.exchange_authorization_code("https://api.smartsheet.com/2.0", "US", "expired")
    assert not ok
    assert body["error_code"] == "OAUTH_EXCHANGE_FAILED"
    assert body["smartsheet_error_code"] == 1004


def test_exchange_network_error(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        raise om.requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(om.requests, "post", fake_post)
    monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_ID_US", "cid")
    monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_SECRET_US", "cs")

    ok, body = om.exchange_authorization_code("https://api.smartsheet.com/2.0", "US", "code")
    assert not ok
    assert body["error_code"] == "SMARTSHEET_UNREACHABLE"


# ── Disconnect scope guard ───────────────────────────────────────────
# Revoking one tool must never cascade to the whole connection: the
# endpoint requires an explicit scope (product_slug OR disconnect_all).

class _FakeQuery:
    """Minimal supabase table stub — records updates, returns canned data."""

    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._filters = {}
        self._update_payload = None

    def select(self, *_args, **_kw):
        return self

    def update(self, payload):
        self._update_payload = payload
        return self

    def insert(self, payload):
        self._store.setdefault("inserts", []).append((self._table, payload))
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def or_(self, *_args, **_kw):
        return self

    def limit(self, *_args, **_kw):
        return self

    def execute(self):
        if self._update_payload is not None:
            self._store.setdefault("updates", []).append(
                (self._table, dict(self._filters), self._update_payload)
            )
            # Apply the update to stored rows matching every eq() filter so
            # stateful flows (refresh marking a row terminal) behave like the
            # real table. or_() filters are ignored by this stub.
            matched = [
                r
                for r in self._store.get(("rows", self._table), [])
                if all(r.get(k) == v for k, v in self._filters.items() if k in r)
            ]
            for r in matched:
                r.update(self._update_payload)

            class R:
                data = matched

            return R()

        class R:
            data = self._store.get(("rows", self._table), [])

        return R()


class _FakeSb:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeQuery(self.store, name)


def _make_disconnect_app(monkeypatch, fake_sb):
    from flask import Flask

    monkeypatch.setenv("SMARTSHEET_BROKER_SECRET", "broker-secret-test")
    bp = om.create_oauth_blueprint(
        sb=fake_sb,
        crypto_key=KEY,
        resolve_base_url=lambda org_id: ("https://api.smartsheet.com/2.0", "US"),
        user_member_of_org=lambda user_id, org_id: True,
        verify_jwt=lambda _auth, _secret: {"sub": "11111111-1111-1111-1111-111111111111"},
        jwt_secret="test",
    )
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app.test_client()


def test_disconnect_without_scope_is_rejected(monkeypatch):
    fake = _FakeSb()
    client = _make_disconnect_app(monkeypatch, fake)
    resp = client.post(
        "/oauth/disconnect",
        json={"organization_id": "22222222-2222-2222-2222-222222222222"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "DISCONNECT_SCOPE_REQUIRED"
    # Nothing was revoked.
    assert not fake.store.get("updates")


def test_disconnect_single_tool_touches_only_that_row(monkeypatch):
    fake = _FakeSb()
    conn_id = "33333333-3333-3333-3333-333333333333"
    fake.store[("rows", "smartsheet_oauth_connections")] = [
        {"id": conn_id, "organization_id": "o", "user_id": "u", "status": "active"}
    ]
    fake.store[("rows", "smartsheet_oauth_tokens")] = [
        {
            "id": "tok-1",
            "connection_id": conn_id,
            "product_slug": "column-id-checker",
            "status": "active",
            "encrypted_access_token": "",
            "encrypted_refresh_token": "",
        }
    ]
    monkeypatch.setattr(om, "revoke_single_token", lambda *_a, **_k: True)
    client = _make_disconnect_app(monkeypatch, fake)

    resp = client.post(
        "/oauth/disconnect",
        json={
            "organization_id": "22222222-2222-2222-2222-222222222222",
            "product_slug": "column-id-checker",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200

    token_updates = [u for u in fake.store.get("updates", []) if u[0] == "smartsheet_oauth_tokens"]
    conn_updates = [u for u in fake.store.get("updates", []) if u[0] == "smartsheet_oauth_connections"]
    # Exactly one token row revoked, targeted by its id — and the
    # connection row itself was never touched.
    assert len(token_updates) == 1
    _, filters, payload = token_updates[0]
    assert filters == {"id": "tok-1"}
    assert payload["status"] == "revoked"
    assert conn_updates == []


def test_disconnect_all_requires_explicit_flag(monkeypatch):
    fake = _FakeSb()
    conn_id = "44444444-4444-4444-4444-444444444444"
    fake.store[("rows", "smartsheet_oauth_connections")] = [
        {"id": conn_id, "organization_id": "o", "user_id": "u", "status": "active", "ss_region": "US"}
    ]
    fake.store[("rows", "smartsheet_oauth_tokens")] = []
    client = _make_disconnect_app(monkeypatch, fake)

    resp = client.post(
        "/oauth/disconnect",
        json={
            "organization_id": "22222222-2222-2222-2222-222222222222",
            "disconnect_all": True,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200
    conn_updates = [u for u in fake.store.get("updates", []) if u[0] == "smartsheet_oauth_connections"]
    assert len(conn_updates) == 1
    assert conn_updates[0][2]["status"] == "revoked"


# ── Token broker hardening ───────────────────────────────────────────
# The broker returns plaintext tokens: it must be invisible on the public
# /encrypt path, deny spoofed non-member user ids, and lock out brute force.

def _make_broker_app(monkeypatch, fake_sb, *, member=True):
    from flask import Flask

    om._rate_windows.clear()
    monkeypatch.setenv("SMARTSHEET_BROKER_SECRET", "broker-secret-test")
    bp = om.create_oauth_blueprint(
        sb=fake_sb,
        crypto_key=KEY,
        resolve_base_url=lambda org_id: ("https://api.smartsheet.com/2.0", "US"),
        user_member_of_org=lambda user_id, org_id: member,
        verify_jwt=lambda _auth, _secret: None,
        jwt_secret="test",
    )
    app = Flask(__name__)
    app.register_blueprint(bp)
    app.register_blueprint(bp, url_prefix="/encrypt", name="oauth_under_encrypt")
    return app.test_client()


def test_broker_route_is_404_on_public_encrypt_prefix(monkeypatch):
    client = _make_broker_app(monkeypatch, _FakeSb())
    resp = client.post(
        "/encrypt/internal/token/resolve",
        json={"organization_id": "o", "product_slug": "column-id-checker"},
        headers={"X-Broker-Secret": "broker-secret-test"},
    )
    assert resp.status_code == 404


def test_broker_denies_oauth_token_for_non_member(monkeypatch):
    fake = _FakeSb()
    conn_id = "55555555-5555-5555-5555-555555555555"
    fake.store[("rows", "smartsheet_oauth_connections")] = [
        {"id": conn_id, "organization_id": "o", "user_id": "u", "status": "active"}
    ]
    fake.store[("rows", "smartsheet_oauth_tokens")] = [
        {
            "id": "tok-1",
            "connection_id": conn_id,
            "product_slug": "column-id-checker",
            "status": "active",
            "encrypted_access_token": om.encrypt_bound(
                KEY, "victim-token", om._aad("o", "u", "column-id-checker", "access")
            ),
            "expires_at": None,
        }
    ]
    client = _make_broker_app(monkeypatch, fake, member=False)
    resp = client.post(
        "/internal/token/resolve",
        json={"organization_id": "o", "user_id": "u", "product_slug": "column-id-checker"},
        headers={"X-Broker-Secret": "broker-secret-test"},
    )
    # Spoofed/non-member user id never yields the per-user OAuth token; with
    # no org PAT stored either, the broker reports no credentials.
    assert resp.status_code == 404
    assert resp.get_json()["error_code"] == "NO_CREDENTIALS"


def test_broker_locks_out_after_repeated_bad_secrets(monkeypatch):
    client = _make_broker_app(monkeypatch, _FakeSb())
    body = {"organization_id": "o", "product_slug": "column-id-checker"}
    for _ in range(om.BROKER_AUTH_FAIL_LIMIT):
        resp = client.post("/internal/token/resolve", json=body,
                           headers={"X-Broker-Secret": "wrong"})
        assert resp.status_code == 401
    resp = client.post("/internal/token/resolve", json=body,
                       headers={"X-Broker-Secret": "wrong"})
    assert resp.status_code == 429
    assert resp.get_json()["error_code"] == "BROKER_AUTH_LOCKED"
    # Even the correct secret is refused while the IP is locked out.
    resp = client.post("/internal/token/resolve", json=body,
                       headers={"X-Broker-Secret": "broker-secret-test"})
    assert resp.status_code == 429


def test_oauth_start_rejects_absolute_return_path(monkeypatch):
    """Open-redirect guard: non-relative return_path is dropped, not stored."""
    fake = _FakeSb()
    client = _make_disconnect_app(monkeypatch, fake)
    monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_ID_US", "cid")
    monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_SECRET_US", "cs")
    resp = client.post(
        "/oauth/start",
        json={
            "organization_id": "22222222-2222-2222-2222-222222222222",
            "product_slug": "column-id-checker",
            "return_path": "https://evil.example.com/phish",
        },
        headers={"Authorization": "Bearer x"},
    )
    inserted_states = [
        p for (t, p) in fake.store.get("inserts", []) if t == "smartsheet_oauth_states"
    ]
    if inserted_states:
        assert inserted_states[0].get("return_path") in (None, "")
    # Regardless of downstream outcome, the malicious path never survives.
    assert "evil.example.com" not in (resp.get_data(as_text=True) or "")


# ── SH-182 resolution matrix ─────────────────────────────────────────
# Every broker response must carry an explicit OAuth state so consumers
# (SH-178 runner failover) can tell a healthy runner from transition-PAT
# continuity — and revoked/terminal with no PAT is a typed 409, never a
# generic NO_CREDENTIALS.

M_ORG = "o"
M_USER = "u"
M_SLUG = "column-id-checker"
M_CONN_ID = "66666666-6666-6666-6666-666666666666"
OAUTH_PLAINTEXT = "oauth-access-plain-secret"
PAT_PLAINTEXT = "pat-plain-secret"


def _seed_connection(fake, *, conn_status="active", token_status="active",
                     expires_at=None):
    fake.store[("rows", "smartsheet_oauth_connections")] = [{
        "id": M_CONN_ID, "organization_id": M_ORG, "user_id": M_USER,
        "status": conn_status, "ss_region": "US",
        "smartsheet_user_email": "runner@example.com",
    }]
    fake.store[("rows", "smartsheet_oauth_tokens")] = [{
        "id": "tok-m", "connection_id": M_CONN_ID, "product_slug": M_SLUG,
        "status": token_status,
        "encrypted_access_token": om.encrypt_bound(
            KEY, OAUTH_PLAINTEXT, om._aad(M_ORG, M_USER, M_SLUG, "access")),
        "encrypted_refresh_token": om.encrypt_bound(
            KEY, "oauth-refresh-plain", om._aad(M_ORG, M_USER, M_SLUG, "refresh")),
        "expires_at": expires_at,
        "refresh_claimed_at": None,
        "refresh_failure_count": 0,
        "last_refreshed_at": None,
        "scopes": [],
    }]


def _seed_pat(fake):
    iv = os.urandom(12)
    ct = om.AESGCM(KEY).encrypt(iv, PAT_PLAINTEXT.encode(), None)
    fake.store[("rows", "smartsheet_tokens")] = [
        {"encrypted_token": base64.b64encode(iv + ct).decode()}
    ]


def _resolve(client, **overrides):
    body = {"organization_id": M_ORG, "user_id": M_USER, "product_slug": M_SLUG}
    body.update(overrides)
    return client.post("/internal/token/resolve", json=body,
                       headers={"X-Broker-Secret": "broker-secret-test"})


def test_matrix_1_healthy_oauth_reports_active_state(monkeypatch):
    fake = _FakeSb()
    _seed_connection(fake)
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source"] == "oauth"
    assert data["oauth_state"] == "active"
    assert data["fallback_used"] is False
    assert "fallback_reason" not in data
    assert data["access_token"] == OAUTH_PLAINTEXT


def test_matrix_2_revoked_with_pat_falls_back_and_reports_revoked(monkeypatch):
    fake = _FakeSb()
    _seed_connection(fake, token_status="revoked")
    _seed_pat(fake)
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source"] == "pat"
    assert data["oauth_state"] == "revoked"
    assert data["fallback_used"] is True
    assert data["fallback_reason"] == "CONNECTION_REVOKED"
    assert data["access_token"] == PAT_PLAINTEXT
    # Non-secret identity metadata only.
    assert data["smartsheet_user_email"] == "runner@example.com"


def test_matrix_2b_revoked_connection_with_pat_reports_revoked(monkeypatch):
    """Whole-connection revoke (disconnect_all) is also oauth_state=revoked."""
    fake = _FakeSb()
    _seed_connection(fake, conn_status="revoked")
    _seed_pat(fake)
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source"] == "pat"
    assert data["oauth_state"] == "revoked"
    assert data["fallback_reason"] == "CONNECTION_REVOKED"


def test_matrix_3_terminal_refresh_with_pat_reports_error(monkeypatch):
    fake = _FakeSb()
    _seed_connection(fake, token_status="error")
    _seed_pat(fake)
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source"] == "pat"
    assert data["oauth_state"] == "error"
    assert data["fallback_used"] is True
    assert data["fallback_reason"] == "TOKEN_REFRESH_FAILED"


def test_matrix_4_missing_oauth_with_pat_is_rollout_not_revocation(monkeypatch):
    fake = _FakeSb()
    _seed_pat(fake)  # no OAuth connection at all
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source"] == "pat"
    assert data["oauth_state"] == "missing"
    assert data["fallback_used"] is True
    assert data["fallback_reason"] == "NO_OAUTH_TOKEN"


def test_matrix_5_revoked_without_pat_is_typed_409(monkeypatch):
    fake = _FakeSb()
    _seed_connection(fake, token_status="revoked")
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["error_code"] == "CONNECTION_REVOKED"
    assert data["oauth_state"] == "revoked"
    assert "access_token" not in data


def test_matrix_5b_terminal_without_pat_is_typed_409(monkeypatch):
    fake = _FakeSb()
    _seed_connection(fake, token_status="error")
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["error_code"] == "TOKEN_REFRESH_FAILED"
    assert data["oauth_state"] == "error"


def test_matrix_6_no_oauth_no_pat_stays_404(monkeypatch):
    fake = _FakeSb()
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["error_code"] == "NO_CREDENTIALS"
    assert data["oauth_state"] == "missing"


def test_matrix_7_transient_refresh_failure_is_not_a_revocation(monkeypatch):
    """A network blip during refresh must stay distinguishable from
    revoked/terminal: 503 without a PAT, oauth_state=unknown with one."""
    fake = _FakeSb()
    _seed_connection(fake, expires_at="2020-01-01T00:00:00+00:00")  # forces refresh
    monkeypatch.setattr(
        om, "refresh_token_pair",
        lambda *_a, **_k: (False, {"error_code": "SMARTSHEET_UNREACHABLE"}),
    )
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["error_code"] == "OAUTH_TEMPORARILY_UNAVAILABLE"
    assert data["oauth_state"] == "unknown"


def test_matrix_7b_transient_refresh_failure_with_pat_reports_unknown(monkeypatch):
    fake = _FakeSb()
    _seed_connection(fake, expires_at="2020-01-01T00:00:00+00:00")
    _seed_pat(fake)
    monkeypatch.setattr(
        om, "refresh_token_pair",
        lambda *_a, **_k: (False, {"error_code": "SMARTSHEET_UNREACHABLE"}),
    )
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source"] == "pat"
    assert data["oauth_state"] == "unknown"
    assert data["fallback_reason"] == "REFRESH_UNAVAILABLE"


def test_matrix_refresh_turned_terminal_becomes_typed_409(monkeypatch):
    """Smartsheet 1004 (invalid grant) during refresh flips the row terminal;
    with no PAT the broker must answer the typed 409, not 503 or 404."""
    fake = _FakeSb()
    _seed_connection(fake, expires_at="2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(
        om, "refresh_token_pair",
        lambda *_a, **_k: (False, {"error_code": "OAUTH_EXCHANGE_FAILED",
                                   "smartsheet_error_code": 1004}),
    )
    client = _make_broker_app(monkeypatch, fake)
    resp = _resolve(client)
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["error_code"] == "TOKEN_REFRESH_FAILED"
    assert data["oauth_state"] == "error"


def test_matrix_responses_never_leak_plaintext_to_audit(monkeypatch, capsys):
    """Plaintext OAuth/PAT tokens must not appear in audit output for any
    matrix row — only in the protected access_token response field."""
    for seed in (
        lambda f: _seed_connection(f),                                  # active
        lambda f: (_seed_connection(f, token_status="revoked"), _seed_pat(f)),
        lambda f: (_seed_connection(f, token_status="error"),),         # 409
        lambda f: (_seed_pat(f),),                                      # missing+PAT
    ):
        fake = _FakeSb()
        seed(fake)
        client = _make_broker_app(monkeypatch, fake)
        _resolve(client)
    # Every audit event (including ones persisted to Supabase) is mirrored to
    # stderr by record_oauth_event, so this covers persisted meta too.
    err = capsys.readouterr().err
    assert OAUTH_PLAINTEXT not in err
    assert PAT_PLAINTEXT not in err


# ── Audit hygiene ────────────────────────────────────────────────────

def test_audit_strips_secret_keys(capsys):
    om.record_oauth_event(
        None,
        request_id="rid",
        event_type="connect",
        outcome="test",
        meta={"access_token": "LEAK", "refresh_token": "LEAK", "state": "LEAK", "region": "US"},
    )
    err = capsys.readouterr().err
    assert "LEAK" not in err
    assert '"region": "US"' in err
