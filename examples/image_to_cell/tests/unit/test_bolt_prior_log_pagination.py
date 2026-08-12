"""
Tests for B14 + B17 — chunking and pagination of `_fetch_prior_processed`.

PostgREST has two scale limits that bite this query:

    B14: response truncation — by default PostgREST returns at most 1000
         rows per query (Supabase config). Without pagination, queries
         that match >1000 entries silently lose data; rows whose entries
         fell off the cut-off look unprocessed and get re-billed.

    B17: URL length — a query with thousands of row_ids in `IN()` produces
         a multi-KB URL that ALBs and PostgREST may reject with 414 / 400.
         Splitting row_ids into chunks of ~500 keeps the URL bounded.

The fix issues:
    - One query per chunk of `row_ids` (B17)
    - Within each chunk, one query per `_PRIOR_LOG_PAGE_SIZE` block of
      response rows, until a partial page indicates we've drained that
      chunk (B14)

The mock_supabase fixture in conftest.py honours `range(start, end)` and
`in_("row_id", [...])` so we can verify both behaviours end-to-end.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def fetch_prior_processed():
    """Bound reference to the production helper, with retry knobs neutralised
    so tests never wait."""
    from src.services import i2c_bolt_gate

    return i2c_bolt_gate._fetch_prior_processed


def _build_log(row_ids, image_column_ids, names_per_row=1):
    """Generate `len(row_ids) * len(image_column_ids) * names_per_row` log
    rows. Each combination produces a distinct filename so signatures don't
    collide."""
    rows = []
    for rid in row_ids:
        for cid in image_column_ids:
            for k in range(names_per_row):
                rows.append({
                    "row_id": rid,
                    "image_column_id": cid,
                    "image_filename": f"r{rid}_c{cid}_n{k}.jpg",
                })
    return rows


# ---------------------------------------------------------------------------
# Small inputs — single chunk, single page
# ---------------------------------------------------------------------------


class TestSmallInput:
    def test_under_limits_runs_in_one_query(
        self, fetch_prior_processed, mock_supabase, monkeypatch
    ):
        """A workload that fits in one chunk and one page should issue
        exactly one DB query."""
        from src.services import i2c_bolt_gate
        monkeypatch.setattr(
            i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase
        )

        row_ids = list(range(1, 11))  # 10 rows
        col_ids = [1001, 1002]  # 2 cols
        mock_supabase.usage_log_rows = _build_log(row_ids, col_ids)  # 20 rows total

        result = fetch_prior_processed(
            organization_id="org",
            worksheet_id=1,
            target_column_ids=col_ids,
            row_ids=row_ids,
        )

        assert mock_supabase.execute_call_count == 1
        # Every row has 2 entries (one per col)
        assert len(result) == 10
        for rid, entries in result.items():
            assert len(entries) == 2


# ---------------------------------------------------------------------------
# B17 — row_id chunking when there are many rows
# ---------------------------------------------------------------------------


class TestB17RowIdChunking:
    def test_thousand_row_ids_split_into_two_chunks(
        self, fetch_prior_processed, mock_supabase, monkeypatch
    ):
        """1000 row_ids → 2 chunks of 500. Each chunk has 500 entries so
        the first page returns full (== page_size, loop continues) and
        a second page returns 0 (loop breaks). Total: 2 chunks × 2 queries = 4."""
        from src.services import i2c_bolt_gate
        monkeypatch.setattr(
            i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase
        )

        row_ids = list(range(1, 1001))  # 1000 rows
        col_ids = [1001]  # 1 col → 1 entry per row → 500 per chunk
        mock_supabase.usage_log_rows = _build_log(row_ids, col_ids)

        result = fetch_prior_processed(
            organization_id="org",
            worksheet_id=1,
            target_column_ids=col_ids,
            row_ids=row_ids,
        )

        assert mock_supabase.execute_call_count == 4
        # All 1000 rows present in result
        assert len(result) == 1000

    def test_2500_row_ids_split_into_five_chunks(
        self, fetch_prior_processed, mock_supabase, monkeypatch
    ):
        """5 chunks × 2 queries per chunk (full + empty-break) = 10 queries."""
        from src.services import i2c_bolt_gate
        monkeypatch.setattr(
            i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase
        )

        row_ids = list(range(1, 2501))
        col_ids = [1001]
        mock_supabase.usage_log_rows = _build_log(row_ids, col_ids)

        result = fetch_prior_processed(
            organization_id="org",
            worksheet_id=1,
            target_column_ids=col_ids,
            row_ids=row_ids,
        )

        assert mock_supabase.execute_call_count == 10
        assert len(result) == 2500


# ---------------------------------------------------------------------------
# B14 — response pagination within a chunk
# ---------------------------------------------------------------------------


class TestB14ResponsePagination:
    def test_chunk_with_more_than_page_size_entries_paginates(
        self, fetch_prior_processed, mock_supabase, monkeypatch
    ):
        """500 row_ids × 3 cols = 1500 entries, all in a single chunk.
        With page_size=500 we expect 4 queries: 3 full pages + 1 short
        page indicating end (1500 / 500 = 3 full pages + 1 empty 'tail'
        check, which the mock returns as 0 rows triggering the break)."""
        from src.services import i2c_bolt_gate
        monkeypatch.setattr(
            i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase
        )

        row_ids = list(range(1, 501))  # 500 rows = 1 chunk
        col_ids = [1001, 1002, 1003]  # 3 cols → 1500 entries
        mock_supabase.usage_log_rows = _build_log(row_ids, col_ids)

        result = fetch_prior_processed(
            organization_id="org",
            worksheet_id=1,
            target_column_ids=col_ids,
            row_ids=row_ids,
        )

        # 1500 / 500 = 3 full pages, then a 4th page returns 0 → break
        assert mock_supabase.execute_call_count == 4
        # All 500 rows captured, each with 3 entries
        assert len(result) == 500
        for entries in result.values():
            assert len(entries) == 3

    def test_chunk_with_exact_page_size_paginates_one_extra(
        self, fetch_prior_processed, mock_supabase, monkeypatch
    ):
        """When the result is exactly page_size, the loop runs an extra
        empty query to confirm there's nothing more (a partial page is
        the only break signal)."""
        from src.services import i2c_bolt_gate
        monkeypatch.setattr(
            i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase
        )

        # 500 rows × 1 col = exactly 500 entries
        row_ids = list(range(1, 501))
        col_ids = [1001]
        mock_supabase.usage_log_rows = _build_log(row_ids, col_ids)

        result = fetch_prior_processed(
            organization_id="org",
            worksheet_id=1,
            target_column_ids=col_ids,
            row_ids=row_ids,
        )

        # First page returns 500 (== page_size) → loop continues
        # Second page returns 0 → loop breaks
        # Total: 2 queries
        assert mock_supabase.execute_call_count == 2
        assert len(result) == 500


# ---------------------------------------------------------------------------
# Combined — chunking + pagination together
# ---------------------------------------------------------------------------


class TestB14B17Combined:
    def test_2000_rows_3_cols_chunks_and_paginates(
        self, fetch_prior_processed, mock_supabase, monkeypatch
    ):
        """2000 row_ids × 3 cols = 6000 entries.

        Chunks: 2000 / 500 = 4 chunks
        Per chunk: 500 row_ids × 3 cols = 1500 entries → 1500 / 500 = 3
        full pages + 1 short page break = 4 queries per chunk

        Total queries: 4 × 4 = 16"""
        from src.services import i2c_bolt_gate
        monkeypatch.setattr(
            i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase
        )

        row_ids = list(range(1, 2001))
        col_ids = [1001, 1002, 1003]
        mock_supabase.usage_log_rows = _build_log(row_ids, col_ids)

        result = fetch_prior_processed(
            organization_id="org",
            worksheet_id=1,
            target_column_ids=col_ids,
            row_ids=row_ids,
        )

        assert mock_supabase.execute_call_count == 16
        assert len(result) == 2000
        for entries in result.values():
            assert len(entries) == 3


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_row_ids_returns_empty_no_query(
        self, fetch_prior_processed, mock_supabase, monkeypatch
    ):
        from src.services import i2c_bolt_gate
        monkeypatch.setattr(
            i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase
        )

        result = fetch_prior_processed(
            organization_id="org",
            worksheet_id=1,
            target_column_ids=[1001],
            row_ids=[],
        )
        assert result == {}
        assert mock_supabase.execute_call_count == 0

    def test_no_matching_log_entries_returns_empty(
        self, fetch_prior_processed, mock_supabase, monkeypatch
    ):
        """Sheet with rows but nothing in the usage log → empty result,
        but at least one query happens."""
        from src.services import i2c_bolt_gate
        monkeypatch.setattr(
            i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase
        )

        mock_supabase.usage_log_rows = []
        result = fetch_prior_processed(
            organization_id="org",
            worksheet_id=1,
            target_column_ids=[1001],
            row_ids=[1, 2, 3],
        )
        assert result == {}
        # Empty page → break immediately, only 1 query
        assert mock_supabase.execute_call_count == 1
