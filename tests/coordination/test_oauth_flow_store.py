"""Unit tests for the Postgres-backed OAuth flow store.

Mocks the ConnectionPool so tests run without a live DB. The put/pop
serialization roundtrip goes through the real google-auth-oauthlib
`Flow` object via `from_client_secrets_file`, which requires a tiny
fixture client-secrets JSON on disk (created per-test in tmp_path).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from zashiki_warasi.coordination.oauth_flow_store import (
    OAuthFlowStore,
    ensure_oauth_flows_table,
)


CLIENT_SECRETS = {
    "installed": {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "project_id": "test-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "test-client-secret",
        "redirect_uris": ["http://127.0.0.1:8080/auth/callback"],
    }
}


@pytest.fixture
def client_secrets_file(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(CLIENT_SECRETS))
    return path


def _make_pool_mock():
    """Pool whose cursor collects executed SQL + returns configurable
    rows via `set_row_sequence`."""
    cursor = MagicMock(name="cursor")
    cursor._row_sequence = []

    def _fetchone():
        if cursor._row_sequence:
            return cursor._row_sequence.pop(0)
        return None

    cursor.fetchone.side_effect = _fetchone
    cursor.set_row_sequence = lambda seq: cursor._row_sequence.extend(seq)

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

    return pool, cursor


class TestEnsureOauthFlowsTable:
    def test_executes_create_table_and_index_separately(self):
        """psycopg rejects multi-statement execute() calls, so the
        table + index must be sent as two separate statements."""
        pool, cursor = _make_pool_mock()

        ensure_oauth_flows_table(pool)

        calls = [args[0][0] for args in cursor.execute.call_args_list]
        assert len(calls) == 2
        table_sql, index_sql = calls
        assert "CREATE TABLE IF NOT EXISTS oauth_flows" in table_sql
        assert "state       TEXT PRIMARY KEY" in table_sql
        assert "flow_json   JSONB NOT NULL" in table_sql
        assert "created_at  TIMESTAMPTZ NOT NULL DEFAULT now()" in table_sql
        assert "CREATE INDEX IF NOT EXISTS oauth_flows_created_at_idx" in index_sql


class TestPutFlow:
    def test_inserts_serialized_flow_json(self, client_secrets_file):
        from google_auth_oauthlib.flow import Flow

        pool, cursor = _make_pool_mock()
        store = OAuthFlowStore(pool, client_secrets_file)

        flow = Flow.from_client_secrets_file(
            str(client_secrets_file),
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            state="csrf-abc",
            redirect_uri="http://127.0.0.1:8080/auth/callback",
        )

        store.put("csrf-abc", flow)

        # One INSERT with upsert semantics.
        sql, params = cursor.execute.call_args[0]
        assert "INSERT INTO oauth_flows" in sql
        assert "ON CONFLICT (state) DO UPDATE" in sql
        assert params[0] == "csrf-abc"
        payload = json.loads(params[1])
        assert payload["redirect_uri"] == "http://127.0.0.1:8080/auth/callback"
        assert "https://www.googleapis.com/auth/gmail.readonly" in payload["scopes"]


class TestPopFlow:
    def test_returns_none_for_missing_state(self, client_secrets_file):
        pool, cursor = _make_pool_mock()
        store = OAuthFlowStore(pool, client_secrets_file)
        # No rows queued → fetchone returns None → pop returns None.

        result = store.pop("does-not-exist")

        assert result is None

    def test_returns_reconstructed_flow_for_fresh_state(
        self, client_secrets_file
    ):
        pool, cursor = _make_pool_mock()
        store = OAuthFlowStore(pool, client_secrets_file)
        payload = {
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "redirect_uri": "http://127.0.0.1:8080/auth/callback",
        }
        # First fetchone is for the sweep DELETE (no RETURNING) — but
        # the sweep DELETE doesn't call fetchone in our implementation
        # (only the target DELETE ... RETURNING does). So queue exactly
        # one row for the target DELETE.
        cursor.set_row_sequence([{"flow_json": payload}])

        flow = store.pop("csrf-abc")

        assert flow is not None
        assert flow.redirect_uri == "http://127.0.0.1:8080/auth/callback"

    def test_runs_sweep_before_target_delete(self, client_secrets_file):
        """The sweep DELETE (stale rows) must fire before the target
        DELETE. Order matters: the sweep may delete our target if it's
        stale, which is the desired outcome."""
        pool, cursor = _make_pool_mock()
        store = OAuthFlowStore(pool, client_secrets_file)
        cursor.set_row_sequence([None])  # target DELETE → no row

        store.pop("csrf-abc")

        calls = [args[0][0] for args in cursor.execute.call_args_list]
        assert len(calls) == 2
        assert "DELETE FROM oauth_flows WHERE created_at" in calls[0]
        assert (
            "DELETE FROM oauth_flows " in calls[1]
            and "RETURNING flow_json" in calls[1]
        )

    def test_target_delete_binds_state_and_cutoff(self, client_secrets_file):
        pool, cursor = _make_pool_mock()
        store = OAuthFlowStore(
            pool, client_secrets_file, ttl_seconds=600
        )
        cursor.set_row_sequence([None])

        store.pop("csrf-abc")

        target_call = cursor.execute.call_args_list[1]
        _, params = target_call[0]
        # (state, cutoff)
        assert params[0] == "csrf-abc"
        # cutoff is a datetime; just assert it's present.
        assert params[1] is not None

    def test_handles_flow_json_as_str(self, client_secrets_file):
        """psycopg normally returns JSONB as a parsed dict; defend
        against a driver upgrade that returns str."""
        pool, cursor = _make_pool_mock()
        store = OAuthFlowStore(pool, client_secrets_file)
        payload = {
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "redirect_uri": "http://127.0.0.1:8080/auth/callback",
        }
        cursor.set_row_sequence([{"flow_json": json.dumps(payload)}])

        flow = store.pop("csrf-abc")

        assert flow is not None
        assert flow.redirect_uri == "http://127.0.0.1:8080/auth/callback"


class TestPutPopRoundtrip:
    """Full serialize → deserialize check against the real
    google-auth-oauthlib Flow object — pins that the private-ish
    fields we use don't drift with library versions."""

    def test_roundtrip_preserves_scopes_and_redirect(
        self, client_secrets_file
    ):
        from google_auth_oauthlib.flow import Flow

        pool, cursor = _make_pool_mock()
        store = OAuthFlowStore(pool, client_secrets_file)

        original = Flow.from_client_secrets_file(
            str(client_secrets_file),
            scopes=[
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
            ],
            state="csrf-round",
            redirect_uri="http://127.0.0.1:8080/auth/callback",
        )
        store.put("csrf-round", original)

        # The put's INSERT payload becomes what pop should see.
        _put_sql, put_params = cursor.execute.call_args[0]
        stored_payload = json.loads(put_params[1])
        cursor.set_row_sequence([{"flow_json": stored_payload}])

        popped = store.pop("csrf-round")

        assert popped is not None
        assert popped.redirect_uri == original.redirect_uri
        assert set(popped.oauth2session.scope or []) == set(
            original.oauth2session.scope or []
        )
