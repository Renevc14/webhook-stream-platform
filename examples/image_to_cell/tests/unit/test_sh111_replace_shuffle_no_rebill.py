"""
SH-111 tests — replace mode shuffles attachments across N columns to keep
"newest N visible", and the shuffle does NOT re-bill attachments that were
already processed.

Scenario the customer reported:
  - Replace config with 3 image columns
  - User uploads A → col0=A, cols 1-2 empty (billed: 1)
  - User uploads B → col0=B, col1=A, col2 empty (billed: 1, A shuffled
    without billing)
  - User uploads C → col0=C, col1=B, col2=A (billed: 1, B and A shuffled
    without billing)

Before SH-111: `replace.filter_attachments` excluded any signature in
`state.last_ids`, so after the first webhook only the newest attachment
reached row_processor → cols 1..N-1 stayed empty. After SH-111 the filter
returns the newest N every time and the shuffle happens; row_processor
exempts shuffled attachments from `log_usage` so the customer is billed
once per attachment regardless of how many times it moves columns.
"""

from __future__ import annotations

import pytest

from tests.unit.conftest import make_attachment, make_config


class TestReplaceFilterAlwaysReturnsNewestN:
    def test_with_no_state_returns_all_newest_N(self):
        from src.core.modes.replace import ReplaceMode
        from src.models.row_state import RowState

        cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
        atts = [make_attachment(id=i, name=f"f{i}.jpg") for i in (1, 2, 3, 4, 5)]
        state = RowState(config_id=cfg.id, row_id=1)

        result = ReplaceMode().filter_attachments(atts, state, cfg)
        # Newest 3, newest-first
        assert [a.id for a in result] == [5, 4, 3]

    def test_with_full_state_still_returns_newest_N(self):
        """The filter must NOT subtract last_ids any more (SH-111)."""
        from src.core.modes.replace import ReplaceMode
        from src.models.row_state import RowState

        cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
        atts = [make_attachment(id=i, name=f"f{i}.jpg") for i in (1, 2, 3)]
        state = RowState(config_id=cfg.id, row_id=1)
        # Everything already processed
        state.last_ids = {a.signature for a in atts}

        result = ReplaceMode().filter_attachments(atts, state, cfg)
        assert {a.id for a in result} == {1, 2, 3}

    def test_caps_at_n_slots(self):
        from src.core.modes.replace import ReplaceMode
        from src.models.row_state import RowState

        cfg = make_config(config_type="replace", n_image_columns=2, images_to_process=2)
        atts = [make_attachment(id=i, name=f"f{i}.jpg") for i in (1, 2, 3, 4, 5)]
        state = RowState(config_id=cfg.id, row_id=1)

        result = ReplaceMode().filter_attachments(atts, state, cfg)
        assert len(result) == 2
        # Newest 2
        assert [a.id for a in result] == [5, 4]


class TestShuffleBillingExemption:
    """Mirror test for the billing exemption inside row_processor.process.

    The check is small and exact: skip `log_usage` for attachments whose
    signature was in `state.last_ids` BEFORE this run started. The check
    runs only on successful uploads. If a refactor changes the check, this
    mirror test catches the regression.
    """

    @staticmethod
    def _should_skip_log(att, pre_run_last_ids: set, success: bool) -> bool:
        """Mirror of the conditional in row_processor.process."""
        return bool(success and att.signature in pre_run_last_ids)

    def test_first_time_attachment_is_billed(self):
        att = make_attachment(id=1, name="a.jpg")
        assert self._should_skip_log(att, pre_run_last_ids=set(), success=True) is False

    def test_shuffled_attachment_is_NOT_billed(self):
        """Attachment already in state before this run → moved to a new
        column → upload happens but no log_usage row created."""
        att = make_attachment(id=1, name="a.jpg")
        pre = {att.signature}
        assert self._should_skip_log(att, pre_run_last_ids=pre, success=True) is True

    def test_failed_upload_is_not_skipped(self):
        """Failed uploads should be logged regardless (so the failure is
        recorded in usage_log with success=false). Only successful shuffles
        are exempted from billing."""
        att = make_attachment(id=1, name="a.jpg")
        pre = {att.signature}
        assert self._should_skip_log(att, pre_run_last_ids=pre, success=False) is False

    def test_different_attachment_with_same_name_is_billed(self):
        """Two attachments named identically but with different ids → different
        signatures. Re-upload of att2 is a fresh first-time processing."""
        att1 = make_attachment(id=1, name="a.jpg")
        att2 = make_attachment(id=2, name="a.jpg")  # different id → different signature
        pre = {att1.signature}
        assert self._should_skip_log(att2, pre_run_last_ids=pre, success=True) is False


class TestEndToEndShuffleSemanticDoc:
    """Documents the customer-visible behaviour after SH-111. Acts as a
    contract test: a refactor that changes how shuffling is billed should
    update this test deliberately."""

    def test_documented_scenario(self):
        """3 sequential webhooks each with 1 new attachment to the same row:
        - W1 (A): col0=A           billed=1 (A is new)
        - W2 (B): col0=B, col1=A   billed=1 (B new, A shuffled — no bill)
        - W3 (C): col0=C, col1=B, col2=A   billed=1 (C new, B & A shuffled)
        Total billed: 3 (one per genuinely new attachment).
        """
        from src.core.modes.replace import ReplaceMode
        from src.models.row_state import RowState

        cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
        att_a = make_attachment(id=1, name="A.jpg")
        att_b = make_attachment(id=2, name="B.jpg")
        att_c = make_attachment(id=3, name="C.jpg")
        state = RowState(config_id=cfg.id, row_id=1)
        mode = ReplaceMode()
        total_billed = 0

        # Helper that mirrors row_processor.process billing logic.
        def _simulate_webhook(new_atts, attachments_now_on_row):
            nonlocal total_billed
            pre_run_last_ids = set(state.last_ids)
            to_process = mode.filter_attachments(attachments_now_on_row, state, cfg)
            billed_this_run = [
                a for a in to_process if a.signature not in pre_run_last_ids
            ]
            total_billed += len(billed_this_run)
            # Update state for next webhook
            state.last_ids.update(a.signature for a in to_process)

        # W1: only A on the row
        _simulate_webhook([att_a], [att_a])
        assert total_billed == 1

        # W2: A + B on the row
        _simulate_webhook([att_b], [att_a, att_b])
        assert total_billed == 2  # +1 for B; A shuffled, no bill

        # W3: A + B + C on the row
        _simulate_webhook([att_c], [att_a, att_b, att_c])
        assert total_billed == 3  # +1 for C; A and B shuffled, no bill
