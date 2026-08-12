from types import SimpleNamespace

import pytest

from smart_qr.adapters.billing.port import MockBilling
from smart_qr.adapters.smartsheet.client import MockSmartsheet, StaticSmartsheetProvider
from smart_qr.adapters.smartsheet.credentials import (
    CredentialsMissing,
    CredentialsRevoked,
    CredentialsTemporarilyUnavailable,
)
from smart_qr.application.process_row import ProcessRowService
from smart_qr.domain.hashutil import payload_hash


class FakeConfigs:
    def __init__(self, cfg):
        self.cfg = cfg

    async def get(self, config_id, organization_id):
        return self.cfg

    async def get_logo_bytes(self, config_id, organization_id):
        return None


class FakeRowStates:
    def __init__(self):
        self.store = {}
        self.attachments = {}

    async def get_hash(self, config_id, row_id):
        return self.store.get((config_id, row_id))

    async def get(self, config_id, row_id):
        ph = self.store.get((config_id, row_id))
        if ph is None:
            return None
        attachment_id, attachment_hash = self.attachments.get(
            (config_id, row_id), (None, None)
        )
        return SimpleNamespace(
            payload_hash=ph,
            attachment_id=attachment_id,
            attachment_payload_hash=attachment_hash,
        )

    async def upsert_hash(self, config_id, row_id, ph):
        self.store[(config_id, row_id)] = ph

    async def set_attachment(self, config_id, row_id, ph, attachment_id):
        self.attachments[(config_id, row_id)] = (attachment_id, ph)

    async def clear_for_config(self, config_id):
        """Mirror RowStateRepository.clear_for_config (SH-233): keep attachment_id."""
        n = 0
        for key in list(self.store):
            if key[0] != config_id:
                continue
            self.store[key] = "invalidated"
            aid, _ = self.attachments.get(key, (None, None))
            if aid is not None:
                self.attachments[key] = (aid, None)
            n += 1
        return n


class FakeUsage:
    def __init__(self):
        self.rows = []
        self.duplicate_keys: set[str] = set()

    async def log_success(self, *args, **kwargs):
        key = kwargs.get("operation_key")
        if key and key in self.duplicate_keys:
            self.rows.append((args, kwargs))
            return False
        if key:
            self.duplicate_keys.add(key)
        self.rows.append((args, kwargs))
        return True


@pytest.mark.asyncio
async def test_process_row_ok_and_skip_hash():
    from datetime import datetime, timezone
    from uuid import uuid4

    from smart_qr.adapters.db.repositories import ConfigRecord

    org = uuid4()
    cfg_id = uuid4()
    cfg = ConfigRecord(
        id=cfg_id,
        organization_id=org,
        worksheet_id=1,
        name="t",
        content_mode="identify",
        form_base_url=None,
        content_column_ids=[11],
        content_column_titles=["Asset ID"],
        template=None,
        form_label_overrides={},
        watched_column_ids=[11],
        target_column_id=22,
        write_mode="replace",
        code_type="qr",
        qr_ecc="M",
        qr_box_size=4,
        qr_border=2,
        pause=False,
        runner_user_id=None,
        last_errors=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    ss = MockSmartsheet()
    billing = MockBilling()
    states = FakeRowStates()
    usage = FakeUsage()
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=states,
        usage=usage,
        smartsheet=StaticSmartsheetProvider(ss),
        billing=billing,
    )
    r1 = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=True
    )
    assert r1.status == "ok"
    assert (1, 100, 22) in ss.images
    r2 = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=False
    )
    assert r2.status == "skipped"
    assert r2.reason == "hash_unchanged"


@pytest.mark.asyncio
async def test_optional_row_attachment_uses_stable_column_and_row_filename():
    from uuid import uuid4

    org, cfg_id = uuid4(), uuid4()
    cfg = _identify_config(org, cfg_id)
    cfg.attach_to_row = True
    ss = MockSmartsheet()
    usage = FakeUsage()
    states = FakeRowStates()
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=states,
        usage=usage,
        smartsheet=StaticSmartsheetProvider(ss),
        billing=MockBilling(),
    )

    result = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=True
    )

    assert result.status == "ok"
    assert "+attachment" in (result.reason or "")
    assert len(usage.rows) == 1
    assert len(ss.attachments) == 1
    filename, attachment_png = next(iter(ss.attachments.values()))
    assert filename == "smartqr-22-100.png"
    assert attachment_png == ss.images[(1, 100, 22)]


class FailFirstAttachmentSmartsheet(MockSmartsheet):
    def __init__(self):
        super().__init__()
        self.fail_attachment = True
        self.cell_uploads = 0

    async def upload_cell_image(self, *args, **kwargs):
        self.cell_uploads += 1
        await super().upload_cell_image(*args, **kwargs)

    async def upload_row_attachment(self, *args, **kwargs):
        if self.fail_attachment:
            self.fail_attachment = False
            raise RuntimeError("attachment service unavailable")
        return await super().upload_row_attachment(*args, **kwargs)


@pytest.mark.asyncio
async def test_attachment_failure_retries_only_attachment_without_rebilling():
    from uuid import uuid4

    org, cfg_id = uuid4(), uuid4()
    cfg = _identify_config(org, cfg_id)
    cfg.attach_to_row = True
    ss = FailFirstAttachmentSmartsheet()
    usage = FakeUsage()
    states = FakeRowStates()
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=states,
        usage=usage,
        smartsheet=StaticSmartsheetProvider(ss),
        billing=MockBilling(),
    )

    first = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=True
    )
    assert first.status == "error"
    assert first.reason and first.reason.startswith(
        "attachment_failed_after_cell_write:"
    )
    assert len(usage.rows) == 1
    assert ss.cell_uploads == 1

    second = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=False
    )
    assert second.status == "ok"
    assert second.reason and second.reason.endswith(":already_billed")
    assert len(usage.rows) == 1
    assert ss.cell_uploads == 1
    assert len(ss.attachments) == 1


@pytest.mark.asyncio
async def test_attachment_replacement_deletes_only_the_smartqr_owned_file():
    from uuid import uuid4

    org, cfg_id = uuid4(), uuid4()
    cfg = _identify_config(org, cfg_id)
    cfg.attach_to_row = True
    ss = MockSmartsheet()
    usage = FakeUsage()
    states = FakeRowStates()
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=states,
        usage=usage,
        smartsheet=StaticSmartsheetProvider(ss),
        billing=MockBilling(),
    )

    first = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=True
    )
    assert first.status == "ok"
    smartqr_attachment_id = next(iter(ss.attachments))[2]
    ss.attachments[(1, 100, 9999)] = ("user-manual.pdf", b"user-file")

    second = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=True
    )

    assert second.status == "ok"
    assert (1, 100, smartqr_attachment_id) not in ss.attachments
    assert ss.attachments[(1, 100, 9999)] == ("user-manual.pdf", b"user-file")
    assert len(usage.rows) == 2
    # The duplicate operation is observed but UsageRepository would return
    # False and avoid a second durable charge.
    assert len(usage.duplicate_keys) == 1


@pytest.mark.asyncio
async def test_style_invalidate_keeps_attachment_id_and_replaces_on_regen():
    """SH-233: logo/style invalidate must not orphan the prior printable file."""
    from uuid import uuid4

    org, cfg_id = uuid4(), uuid4()
    cfg = _identify_config(org, cfg_id)
    cfg.attach_to_row = True
    ss = MockSmartsheet()
    usage = FakeUsage()
    states = FakeRowStates()
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=states,
        usage=usage,
        smartsheet=StaticSmartsheetProvider(ss),
        billing=MockBilling(),
    )

    first = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=True
    )
    assert first.status == "ok"
    old_id = next(iter(ss.attachments))[2]
    assert states.attachments[(cfg_id, 100)][0] == old_id

    await states.clear_for_config(cfg_id)
    assert states.store[(cfg_id, 100)] == "invalidated"
    assert states.attachments[(cfg_id, 100)][0] == old_id

    # Style change also changes the payload hash (box size), like a real PATCH.
    cfg.qr_box_size = 6
    second = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=False
    )
    assert second.status == "ok"
    smartqr_files = [
        (aid, name)
        for (sid, rid, aid), (name, _b) in ss.attachments.items()
        if sid == 1 and rid == 100 and name.startswith("smartqr-")
    ]
    assert len(smartqr_files) == 1
    assert smartqr_files[0][0] != old_id
    assert (1, 100, old_id) not in ss.attachments


@pytest.mark.asyncio
async def test_orphan_smartqr_filename_deleted_when_attachment_id_missing():
    """Even without tracked ownership, same-name SmartQR files are replaced."""
    from uuid import uuid4

    org, cfg_id = uuid4(), uuid4()
    cfg = _identify_config(org, cfg_id)
    cfg.attach_to_row = True
    ss = MockSmartsheet()
    orphan_name = f"smartqr-{cfg.target_column_id}-100.png"
    ss.attachments[(1, 100, 4242)] = (orphan_name, b"old-png")
    ss.attachments[(1, 100, 9999)] = ("user-manual.pdf", b"user-file")
    states = FakeRowStates()
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=states,
        usage=FakeUsage(),
        smartsheet=StaticSmartsheetProvider(ss),
        billing=MockBilling(),
    )

    result = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=True
    )
    assert result.status == "ok"
    assert (1, 100, 4242) not in ss.attachments
    assert ss.attachments[(1, 100, 9999)] == ("user-manual.pdf", b"user-file")
    smartqr = [
        aid
        for (sid, rid, aid), (name, _) in ss.attachments.items()
        if sid == 1 and rid == 100 and name == orphan_name
    ]
    assert len(smartqr) == 1


@pytest.mark.asyncio
async def test_every_outcome_is_logged_with_org_and_duration(caplog):
    """A silent skip cannot be told apart from a bug when reading logs."""
    from datetime import datetime, timezone
    from uuid import uuid4

    from smart_qr.adapters.db.repositories import ConfigRecord

    org = uuid4()
    cfg_id = uuid4()
    cfg = ConfigRecord(
        id=cfg_id,
        organization_id=org,
        worksheet_id=1,
        name="t",
        content_mode="identify",
        form_base_url=None,
        content_column_ids=[11],
        content_column_titles=["Asset ID"],
        template=None,
        form_label_overrides={},
        watched_column_ids=[11],
        target_column_id=22,
        write_mode="replace",
        code_type="qr",
        qr_ecc="M",
        qr_box_size=4,
        qr_border=2,
        pause=True,
        runner_user_id=None,
        last_errors=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=FakeRowStates(),
        usage=FakeUsage(),
        smartsheet=StaticSmartsheetProvider(MockSmartsheet()),
        billing=MockBilling(),
    )
    with caplog.at_level("INFO"):
        result = await svc.process(organization_id=org, config_id=cfg_id, row_id=100)

    assert result.reason == "paused"
    logged = "\n".join(caplog.messages)
    assert "reason=paused" in logged
    assert str(org) in logged
    assert "duration_ms=" in logged


@pytest.mark.asyncio
async def test_hash_match_rewrites_when_target_image_missing():
    from datetime import datetime, timezone
    from uuid import uuid4

    from smart_qr.adapters.db.repositories import ConfigRecord

    org = uuid4()
    cfg_id = uuid4()
    cfg = ConfigRecord(
        id=cfg_id,
        organization_id=org,
        worksheet_id=1,
        name="t",
        content_mode="identify",
        form_base_url=None,
        content_column_ids=[11],
        content_column_titles=["Asset ID"],
        template=None,
        form_label_overrides={},
        watched_column_ids=[11],
        target_column_id=22,
        write_mode="replace",
        code_type="qr",
        qr_ecc="M",
        qr_box_size=4,
        qr_border=2,
        pause=False,
        runner_user_id=None,
        last_errors=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    ss = MockSmartsheet()
    billing = MockBilling()
    states = FakeRowStates()
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=states,
        usage=FakeUsage(),
        smartsheet=StaticSmartsheetProvider(ss),
        billing=billing,
    )
    r1 = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=True
    )
    assert r1.status == "ok"
    # Simulate user clearing the destination image while source text is unchanged
    del ss.images[(1, 100, 22)]
    r2 = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=False
    )
    assert r2.status == "ok"
    assert (1, 100, 22) in ss.images


@pytest.mark.asyncio
async def test_self_change_agent_skipped():
    from datetime import datetime, timezone
    from uuid import uuid4

    from smart_qr.adapters.db.repositories import ConfigRecord
    from smart_qr.config.constants import CHANGE_AGENT_ID

    org = uuid4()
    cfg_id = uuid4()
    cfg = ConfigRecord(
        id=cfg_id,
        organization_id=org,
        worksheet_id=1,
        name="t",
        content_mode="identify",
        form_base_url=None,
        content_column_ids=[11],
        content_column_titles=["Asset ID"],
        template=None,
        form_label_overrides={},
        watched_column_ids=[11],
        target_column_id=22,
        write_mode="replace",
        code_type="qr",
        qr_ecc="M",
        qr_box_size=4,
        qr_border=2,
        pause=False,
        runner_user_id=None,
        last_errors=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=FakeRowStates(),
        usage=FakeUsage(),
        smartsheet=StaticSmartsheetProvider(MockSmartsheet()),
        billing=MockBilling(),
    )
    r = await svc.process(
        organization_id=org,
        config_id=cfg_id,
        row_id=1,
        change_agent=CHANGE_AGENT_ID,
    )
    assert r.status == "skipped"
    assert r.reason == "self_change_agent"


@pytest.mark.asyncio
async def test_process_open_row_writes_permalink_qr():
    from datetime import datetime, timezone
    from uuid import uuid4

    from smart_qr.adapters.db.repositories import ConfigRecord

    org = uuid4()
    cfg_id = uuid4()
    cfg = ConfigRecord(
        id=cfg_id,
        organization_id=org,
        worksheet_id=1,
        name="open-row",
        content_mode="open_row",
        form_base_url=None,
        content_column_ids=[],
        content_column_titles=[],
        template=None,
        form_label_overrides={},
        watched_column_ids=[],
        target_column_id=22,
        write_mode="replace",
        code_type="qr",
        qr_ecc="M",
        qr_box_size=4,
        qr_border=2,
        pause=False,
        runner_user_id=None,
        last_errors=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    ss = MockSmartsheet()
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=FakeRowStates(),
        usage=FakeUsage(),
        smartsheet=StaticSmartsheetProvider(ss),
        billing=MockBilling(),
    )
    r = await svc.process(organization_id=org, config_id=cfg_id, row_id=100, force=True)
    assert r.status == "ok"
    assert (1, 100, 22) in ss.images


@pytest.mark.asyncio
async def test_process_open_row_skips_empty_row():
    from datetime import datetime, timezone
    from uuid import uuid4

    from smart_qr.adapters.db.repositories import ConfigRecord

    org = uuid4()
    cfg_id = uuid4()
    cfg = ConfigRecord(
        id=cfg_id,
        organization_id=org,
        worksheet_id=1,
        name="open-row-empty",
        content_mode="open_row",
        form_base_url=None,
        content_column_ids=[],
        content_column_titles=[],
        template=None,
        form_label_overrides={},
        watched_column_ids=[],
        target_column_id=22,
        write_mode="replace",
        code_type="qr",
        qr_ecc="M",
        qr_box_size=4,
        qr_border=2,
        pause=False,
        runner_user_id=None,
        last_errors=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    ss = MockSmartsheet()
    ss.empty_rows.add((1, 100))
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=FakeRowStates(),
        usage=FakeUsage(),
        smartsheet=StaticSmartsheetProvider(ss),
        billing=MockBilling(),
    )
    r = await svc.process(organization_id=org, config_id=cfg_id, row_id=100, force=True)
    assert r.status == "skipped"
    assert r.reason == "payload:row has no non-empty content"
    assert (1, 100, 22) not in ss.images


@pytest.mark.asyncio
async def test_process_advanced_writes_template_qr():
    from datetime import datetime, timezone
    from uuid import uuid4

    from smart_qr.adapters.db.repositories import ConfigRecord
    from smart_qr.config.constants import (
        DEFAULT_QR_BACK,
        DEFAULT_QR_BORDER,
        DEFAULT_QR_BOX,
        DEFAULT_QR_FILL,
        DEFAULT_QR_LOGO_SCALE,
    )

    org = uuid4()
    cfg_id = uuid4()
    cfg = ConfigRecord(
        id=cfg_id,
        organization_id=org,
        worksheet_id=1,
        name="custom-link",
        content_mode="advanced",
        form_base_url=None,
        content_column_ids=[11],
        content_column_titles=["Asset ID"],
        template="https://apps.example.com/asset/{Asset ID}",
        form_label_overrides={},
        watched_column_ids=[11],
        target_column_id=22,
        write_mode="replace",
        code_type="qr",
        qr_ecc="M",
        qr_box_size=4,
        qr_border=2,
        pause=False,
        runner_user_id=None,
        last_errors=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    ss = MockSmartsheet()
    ss.cells[(1, 100, "Asset ID")] = "A-42"
    states = FakeRowStates()
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=states,
        usage=FakeUsage(),
        smartsheet=StaticSmartsheetProvider(ss),
        billing=MockBilling(),
    )
    r = await svc.process(organization_id=org, config_id=cfg_id, row_id=100, force=True)
    assert r.status == "ok"
    assert (1, 100, 22) in ss.images
    expected_payload = "https://apps.example.com/asset/A-42"
    logo_token = payload_hash("nologo", "logo")
    style_token = (
        f"{DEFAULT_QR_FILL}|{DEFAULT_QR_BACK}|{DEFAULT_QR_BOX}|"
        f"{DEFAULT_QR_BORDER}|{DEFAULT_QR_LOGO_SCALE:.3f}"
    )
    expected_hash = payload_hash(f"{expected_payload}|{logo_token}|{style_token}", "qr")
    assert states.store[(cfg_id, 100)] == expected_hash


@pytest.mark.asyncio
async def test_process_identify_rejects_image_source_cell():
    from datetime import datetime, timezone
    from uuid import uuid4

    from smart_qr.adapters.db.repositories import ConfigRecord

    org = uuid4()
    cfg_id = uuid4()
    cfg = ConfigRecord(
        id=cfg_id,
        organization_id=org,
        worksheet_id=1,
        name="id-on-attachments",
        content_mode="identify",
        form_base_url=None,
        content_column_ids=[11],
        content_column_titles=["Photo"],
        template=None,
        form_label_overrides={},
        watched_column_ids=[11],
        target_column_id=22,
        write_mode="replace",
        code_type="qr",
        qr_ecc="M",
        qr_box_size=4,
        qr_border=2,
        pause=False,
        runner_user_id=None,
        last_errors=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    ss = MockSmartsheet()
    ss.image_cells.add((1, 100, "Photo"))
    ss.cells[(1, 100, "Photo")] = "scan-001.png"
    svc = ProcessRowService(
        configs=FakeConfigs(cfg),
        row_states=FakeRowStates(),
        usage=FakeUsage(),
        smartsheet=StaticSmartsheetProvider(ss),
        billing=MockBilling(),
    )
    r = await svc.process(organization_id=org, config_id=cfg_id, row_id=100, force=True)
    assert r.status == "error"
    assert "content_column_is_image" in (r.reason or "")
    assert (1, 100, 22) not in ss.images


def _identify_config(org, cfg_id, *, runner_user_id=None):
    from datetime import datetime, timezone

    from smart_qr.adapters.db.repositories import ConfigRecord

    now = datetime.now(timezone.utc)
    return ConfigRecord(
        id=cfg_id,
        organization_id=org,
        worksheet_id=1,
        name="t",
        content_mode="identify",
        form_base_url=None,
        content_column_ids=[11],
        content_column_titles=["Asset ID"],
        template=None,
        form_label_overrides={},
        watched_column_ids=[11],
        target_column_id=22,
        write_mode="replace",
        code_type="qr",
        qr_ecc="M",
        qr_box_size=4,
        qr_border=2,
        pause=False,
        runner_user_id=runner_user_id,
        last_errors=[],
        created_at=now,
        updated_at=now,
    )


class RaisingProvider:
    """A provider whose credentials cannot be resolved."""

    def __init__(self, error):
        self.error = error
        self.asked_for = []

    async def get(self, organization_id, user_id=None):
        self.asked_for.append((organization_id, user_id))
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected_reason",
    [
        (CredentialsMissing("not connected"), "connection_missing"),
        (
            CredentialsRevoked("revoked", oauth_state="revoked"),
            "reconnect_required:revoked",
        ),
        (
            CredentialsTemporarilyUnavailable("broker down"),
            "credentials_unavailable:broker down",
        ),
    ],
)
async def test_credential_failures_are_classified_not_swallowed(error, expected_reason):
    """A missing connection and a broker outage need different operator actions."""
    from uuid import uuid4

    org, cfg_id = uuid4(), uuid4()
    provider = RaisingProvider(error)
    svc = ProcessRowService(
        configs=FakeConfigs(_identify_config(org, cfg_id)),
        row_states=FakeRowStates(),
        usage=FakeUsage(),
        smartsheet=provider,
        billing=MockBilling(),
    )

    result = await svc.process(
        organization_id=org, config_id=cfg_id, row_id=100, force=True
    )

    assert result.status == "error"
    assert result.reason == expected_reason


@pytest.mark.asyncio
async def test_background_work_runs_as_the_config_runner():
    """Webhook and stream writes have no session, only the stored runner."""
    from uuid import uuid4

    org, cfg_id, runner = uuid4(), uuid4(), uuid4()
    provider = RaisingProvider(CredentialsMissing("not connected"))
    svc = ProcessRowService(
        configs=FakeConfigs(_identify_config(org, cfg_id, runner_user_id=runner)),
        row_states=FakeRowStates(),
        usage=FakeUsage(),
        smartsheet=provider,
        billing=MockBilling(),
    )

    await svc.process(organization_id=org, config_id=cfg_id, row_id=100, force=True)
    assert provider.asked_for == [(org, str(runner))]


@pytest.mark.asyncio
async def test_interactive_work_runs_as_the_signed_in_user():
    """Clicking Generate must write with the clicker's own connection."""
    from uuid import uuid4

    org, cfg_id, runner = uuid4(), uuid4(), uuid4()
    clicker = str(uuid4())
    provider = RaisingProvider(CredentialsMissing("not connected"))
    svc = ProcessRowService(
        configs=FakeConfigs(_identify_config(org, cfg_id, runner_user_id=runner)),
        row_states=FakeRowStates(),
        usage=FakeUsage(),
        smartsheet=provider,
        billing=MockBilling(),
    )

    await svc.process(
        organization_id=org,
        config_id=cfg_id,
        row_id=100,
        force=True,
        as_user_id=clicker,
    )
    assert provider.asked_for == [(org, clicker)]
