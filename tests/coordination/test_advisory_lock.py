"""Unit tests for the Postgres advisory-lock helper.

Unit-level: mocks the ConnectionPool + Connection + Cursor so we can
assert the SQL shape and control return values without a live Postgres.
Live-DB verification (two concurrent sessions actually contending)
lives in Group 17.6's cross-replica live smoke.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from zashiki_warasi.coordination.advisory_lock import (
    TICK_LOCK_KEY,
    advisory_lock,
)


def _make_pool(lock_returns: bool):
    """Fake pool whose cursor returns the given bool from
    `pg_try_advisory_lock`."""
    cursor = MagicMock(name="cursor")

    # `dict_row` is what the real pool uses; mirror the row shape.
    def _fetchone_side_effect():
        # Only the pg_try_advisory_lock call fetches; the unlock does not.
        return {"ok": lock_returns}

    cursor.fetchone.side_effect = _fetchone_side_effect

    cursor_cm = MagicMock()
    cursor_cm.__enter__.return_value = cursor
    cursor_cm.__exit__.return_value = False

    conn = MagicMock(name="conn")
    conn.cursor.return_value = cursor_cm

    conn_cm = MagicMock()
    conn_cm.__enter__.return_value = conn
    conn_cm.__exit__.return_value = False

    pool = MagicMock(name="pool")
    pool.connection.return_value = conn_cm

    return pool, conn, cursor


class TestTickLockKey:
    def test_key_is_stable_int(self):
        """Value must never change silently; pg_locks entries key off it."""
        assert isinstance(TICK_LOCK_KEY, int)
        # Regression pin: if this ever needs to change, it's a
        # deliberate migration event, not an accident.
        assert TICK_LOCK_KEY == -6178253175476858907


class TestAdvisoryLock:
    def test_acquires_and_releases_on_success(self):
        pool, conn, cursor = _make_pool(lock_returns=True)

        with advisory_lock(pool, TICK_LOCK_KEY) as (acquired, held_conn):
            assert acquired is True
            assert held_conn is conn

        # Both queries fired: try_lock + unlock.
        calls = [args[0][0] for args in cursor.execute.call_args_list]
        assert calls[0] == "SELECT pg_try_advisory_lock(%s) AS ok"
        assert calls[-1] == "SELECT pg_advisory_unlock(%s)"

    def test_key_is_bound_parameter_not_string_interpolation(self):
        """Guard against a future refactor that formats the key into the
        SQL string — always parameterized to keep the query plan cached
        and to avoid any accidental SQL-injection surface (the key is
        internal, but hygiene matters)."""
        pool, _, cursor = _make_pool(lock_returns=True)

        with advisory_lock(pool, 42):
            pass

        try_call = cursor.execute.call_args_list[0]
        assert try_call[0][1] == (42,)
        unlock_call = cursor.execute.call_args_list[-1]
        assert unlock_call[0][1] == (42,)

    def test_does_not_unlock_when_acquire_failed(self):
        pool, _, cursor = _make_pool(lock_returns=False)

        with advisory_lock(pool, TICK_LOCK_KEY) as (acquired, _conn):
            assert acquired is False

        # Only the try_lock query — no unlock (nothing to release).
        calls = [args[0][0] for args in cursor.execute.call_args_list]
        assert calls == ["SELECT pg_try_advisory_lock(%s) AS ok"]

    def test_releases_lock_even_when_body_raises(self):
        pool, _, cursor = _make_pool(lock_returns=True)

        with pytest.raises(RuntimeError, match="boom"):
            with advisory_lock(pool, TICK_LOCK_KEY) as (acquired, _c):
                assert acquired is True
                raise RuntimeError("boom")

        # Unlock still fired.
        calls = [args[0][0] for args in cursor.execute.call_args_list]
        assert calls[-1] == "SELECT pg_advisory_unlock(%s)"

    def test_unlock_failure_is_swallowed(self, caplog):
        """Unlock is best-effort — the session-close release is our
        real safety net. Raising from the unlock would mask whatever
        exception (if any) came out of the body."""
        import logging

        pool, _, cursor = _make_pool(lock_returns=True)

        def _execute_side_effect(sql, *_args):
            if "unlock" in sql:
                raise RuntimeError("db went away during unlock")
            # try_lock returns None (fetchone gets the fixture value)
            return None

        cursor.execute.side_effect = _execute_side_effect

        with caplog.at_level(logging.WARNING):
            with advisory_lock(pool, TICK_LOCK_KEY) as (acquired, _c):
                assert acquired is True
                # do work — successful

        assert any(
            "session-close release" in r.getMessage() for r in caplog.records
        )

    def test_tuple_row_factory_still_works(self):
        """The pool uses dict_row today (services.py wires it that way),
        but a future change or a test-configured pool might use
        tuple_row. Verify the extractor handles both."""
        cursor = MagicMock(name="cursor")
        cursor.fetchone.return_value = (True,)  # tuple shape

        cursor_cm = MagicMock()
        cursor_cm.__enter__.return_value = cursor
        cursor_cm.__exit__.return_value = False

        conn = MagicMock()
        conn.cursor.return_value = cursor_cm

        conn_cm = MagicMock()
        conn_cm.__enter__.return_value = conn
        conn_cm.__exit__.return_value = False

        pool = MagicMock()
        pool.connection.return_value = conn_cm

        with advisory_lock(pool, TICK_LOCK_KEY) as (acquired, _c):
            assert acquired is True
