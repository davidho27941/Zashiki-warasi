"""Logging bootstrap + helpers.

The formatter, adapter, and node_trace context manager back every INFO
line the operator sees and every DEBUG trace an incident-responder
searches. Bugs here silently degrade every other module's observability,
so the tests are heavy on structural asserts (handler count, level
values, exact context field presence) rather than message-string
matching.
"""

from __future__ import annotations

import logging

import pytest

from zashiki_warasi.core.config import LoggingSettings
from zashiki_warasi.core.logging import (
    ContextFormatter,
    _CHATTY_THIRD_PARTY_LOGGERS,
    _HANDLER_SENTINEL,
    _ZASHIKI_LOGGER_NAME,
    bind_message_context,
    configure_logging,
    node_trace,
)


@pytest.fixture(autouse=True)
def _reset_logging():
    """Every test starts + ends with a clean root logger + reset
    zashiki tree + reset third-party levels. Otherwise handlers from
    one test leak into the next (pytest imports everything once) and
    handler-count assertions become flaky."""

    def _snapshot():
        root = logging.getLogger()
        return {
            "root_level": root.level,
            "root_handlers": list(root.handlers),
            "level_zashiki": logging.getLogger(_ZASHIKI_LOGGER_NAME).level,
            "third_party": {
                name: logging.getLogger(name).level
                for name in _CHATTY_THIRD_PARTY_LOGGERS
            },
        }

    def _restore(snap):
        root = logging.getLogger()
        # Remove any handlers we added
        for h in list(root.handlers):
            if h not in snap["root_handlers"]:
                root.removeHandler(h)
        root.setLevel(snap["root_level"])
        logging.getLogger(_ZASHIKI_LOGGER_NAME).setLevel(snap["level_zashiki"])
        for name, lvl in snap["third_party"].items():
            logging.getLogger(name).setLevel(lvl)

    snap = _snapshot()
    # Also proactively strip any pre-existing zashiki handlers from
    # prior tests that might have leaked past their fixture.
    for h in list(logging.getLogger().handlers):
        if getattr(h, _HANDLER_SENTINEL, False):
            logging.getLogger().removeHandler(h)
    yield
    _restore(snap)


# --- configure_logging ---


class TestConfigureLogging:
    def test_first_call_attaches_exactly_one_handler(self):
        before = len(logging.getLogger().handlers)
        configure_logging(LoggingSettings())
        after = len(logging.getLogger().handlers)
        assert after == before + 1

    def test_second_call_does_not_duplicate_handler(self):
        """Idempotent — else `docker exec` re-invocations or a stray
        second call would double every log line."""
        configure_logging(LoggingSettings())
        count_after_first = len(logging.getLogger().handlers)
        configure_logging(LoggingSettings())
        count_after_second = len(logging.getLogger().handlers)
        assert count_after_first == count_after_second

    def test_handler_carries_our_sentinel(self):
        configure_logging(LoggingSettings())
        our_handlers = [
            h
            for h in logging.getLogger().handlers
            if getattr(h, _HANDLER_SENTINEL, False)
        ]
        assert len(our_handlers) == 1
        assert isinstance(our_handlers[0].formatter, ContextFormatter)

    def test_root_level_applied(self):
        configure_logging(LoggingSettings(level="WARNING"))
        assert logging.getLogger().level == logging.WARNING

    def test_level_zashiki_override_applied(self):
        configure_logging(
            LoggingSettings(level="INFO", level_zashiki="DEBUG")
        )
        assert (
            logging.getLogger(_ZASHIKI_LOGGER_NAME).level == logging.DEBUG
        )
        # And root stays at INFO so third-party defaults remain sane.
        assert logging.getLogger().level == logging.INFO

    def test_level_zashiki_unset_inherits_from_root(self):
        """With no explicit override, the zashiki logger must inherit
        (level = NOTSET) so a later `LOG_LEVEL=DEBUG` reconfigure
        actually takes effect on our tree too."""
        # Prime it to something non-NOTSET first
        logging.getLogger(_ZASHIKI_LOGGER_NAME).setLevel(logging.ERROR)
        configure_logging(LoggingSettings(level="INFO"))
        assert (
            logging.getLogger(_ZASHIKI_LOGGER_NAME).level == logging.NOTSET
        )

    def test_third_party_loggers_pinned_to_warning(self):
        configure_logging(LoggingSettings(level="DEBUG"))
        for name in _CHATTY_THIRD_PARTY_LOGGERS:
            assert (
                logging.getLogger(name).level == logging.WARNING
            ), f"{name} was not muted"

    def test_reconfigure_updates_root_level(self):
        """Second call updates the level — otherwise `docker exec` to
        change LOG_LEVEL wouldn't take effect."""
        configure_logging(LoggingSettings(level="INFO"))
        configure_logging(LoggingSettings(level="WARNING"))
        assert logging.getLogger().level == logging.WARNING

    def test_invalid_level_raises_before_handler_attach(self, monkeypatch):
        """Fail-fast on config errors — if we attached a handler first
        and then failed on level, a re-run would find our sentinel and
        skip re-attach even though the initial attempt was broken."""
        monkeypatch.setenv("LOG_LEVEL", "verbose")
        before = len(logging.getLogger().handlers)
        with pytest.raises(Exception, match="invalid log level"):
            configure_logging()  # constructs LoggingSettings() internally
        after = len(logging.getLogger().handlers)
        assert after == before

    def test_default_settings_used_when_none_passed(self, monkeypatch):
        for var in ("LOG_LEVEL", "LOG_LEVEL_ZASHIKI"):
            monkeypatch.delenv(var, raising=False)
        configure_logging()  # no arg
        assert logging.getLogger().level == logging.INFO


# --- ContextFormatter ---


class TestContextFormatter:
    def _record(self, **extra) -> logging.LogRecord:
        record = logging.LogRecord(
            name="zashiki_warasi.test",
            level=logging.INFO,
            pathname="/x.py",
            lineno=1,
            msg="hello",
            args=None,
            exc_info=None,
        )
        for key, value in extra.items():
            record.__dict__[key] = value
        return record

    def test_no_context_omits_brackets(self):
        out = ContextFormatter().format(self._record())
        # Between logger name and `: hello` there must be no `[...]`
        assert "zashiki_warasi.test: hello" in out
        assert "[" not in out.split("hello")[0]

    def test_message_id_rendered_in_brackets(self):
        out = ContextFormatter().format(self._record(message_id="msg-42"))
        assert "zashiki_warasi.test[message_id=msg-42]: hello" in out

    def test_multiple_fields_comma_separated_in_allowlist_order(self):
        """Order is the `_CONTEXT_FIELDS` tuple, not the dict
        iteration order — keeps output stable across Python versions
        and dict insertion history."""
        out = ContextFormatter().format(
            self._record(expense_id="e-2", message_id="msg-1")
        )
        # `_CONTEXT_FIELDS` = (message_id, thread_id, expense_id) so
        # message_id must come first, expense_id after.
        assert "[message_id=msg-1,expense_id=e-2]" in out

    def test_non_allowlisted_extras_are_ignored(self):
        """Prevents accidental leaking of arbitrary caller attrs and
        keeps the format grepped-for-a-key predictable."""
        out = ContextFormatter().format(
            self._record(secret_token="deadbeef", message_id="msg-1")
        )
        assert "secret_token" not in out
        assert "message_id=msg-1" in out

    def test_none_valued_field_is_omitted(self):
        out = ContextFormatter().format(
            self._record(message_id="msg-1", thread_id=None)
        )
        assert "thread_id" not in out
        assert "message_id=msg-1" in out


# --- bind_message_context ---


class TestBindMessageContext:
    def test_bound_field_reaches_record(self, caplog):
        logger = logging.getLogger("zashiki_warasi.test.bind1")
        logger.setLevel(logging.DEBUG)
        adapter = bind_message_context(logger, message_id="msg-a")
        with caplog.at_level(logging.INFO, logger=logger.name):
            adapter.info("hello")
        assert caplog.records[-1].message_id == "msg-a"

    def test_extra_kwargs_propagate(self, caplog):
        logger = logging.getLogger("zashiki_warasi.test.bind2")
        logger.setLevel(logging.DEBUG)
        adapter = bind_message_context(
            logger, message_id="m", expense_id="e-1"
        )
        with caplog.at_level(logging.INFO, logger=logger.name):
            adapter.info("hi")
        rec = caplog.records[-1]
        assert rec.message_id == "m"
        assert rec.expense_id == "e-1"

    def test_none_extra_is_dropped_not_bound(self, caplog):
        """None means 'no such context yet' — don't stamp None on the
        record and confuse the formatter."""
        logger = logging.getLogger("zashiki_warasi.test.bind3")
        logger.setLevel(logging.DEBUG)
        adapter = bind_message_context(
            logger, message_id="m", expense_id=None
        )
        with caplog.at_level(logging.INFO, logger=logger.name):
            adapter.info("hi")
        rec = caplog.records[-1]
        assert rec.message_id == "m"
        assert getattr(rec, "expense_id", None) is None

    def test_nested_bind_merges_with_precedence_to_new(self, caplog):
        """Wrapping an already-bound adapter must combine both sets of
        extras, with the outer (later) call overriding on conflict —
        so mid-flight refinements (e.g. add expense_id once known)
        work without losing message_id."""
        logger = logging.getLogger("zashiki_warasi.test.bind4")
        logger.setLevel(logging.DEBUG)
        outer = bind_message_context(logger, message_id="m")
        inner = bind_message_context(
            outer, message_id="m", expense_id="e-9"
        )
        with caplog.at_level(logging.INFO, logger=logger.name):
            inner.info("hi")
        rec = caplog.records[-1]
        assert rec.message_id == "m"
        assert rec.expense_id == "e-9"

    def test_per_call_extra_still_wins(self, caplog):
        """The adapter's stored extras merge with per-call `extra=`,
        with per-call taking precedence for one-off overrides."""
        logger = logging.getLogger("zashiki_warasi.test.bind5")
        logger.setLevel(logging.DEBUG)
        adapter = bind_message_context(logger, message_id="default")
        with caplog.at_level(logging.INFO, logger=logger.name):
            adapter.info("hi", extra={"message_id": "override"})
        assert caplog.records[-1].message_id == "override"


# --- node_trace ---


class TestNodeTrace:
    def test_normal_exit_emits_enter_and_exit(self, caplog):
        logger = logging.getLogger("zashiki_warasi.test.node1")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            with node_trace(logger, "analyze"):
                pass
        messages = [r.getMessage() for r in caplog.records]
        assert any("node=analyze enter" in m for m in messages)
        assert any(
            "node=analyze exit " in m and "elapsed_ms=" in m
            for m in messages
        )

    def test_elapsed_ms_is_non_negative_integer(self, caplog):
        logger = logging.getLogger("zashiki_warasi.test.node2")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            with node_trace(logger, "n"):
                pass
        exits = [
            r for r in caplog.records if "exit" in r.getMessage()
        ]
        assert exits, "no exit record captured"
        # The message contains `elapsed_ms=<int>`
        exit_msg = exits[-1].getMessage()
        elapsed = int(exit_msg.split("elapsed_ms=")[1].split()[0])
        assert elapsed >= 0

    def test_exception_re_raised_and_exit_error_logged(self, caplog):
        logger = logging.getLogger("zashiki_warasi.test.node3")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            with pytest.raises(RuntimeError, match="kaboom"):
                with node_trace(logger, "analyze"):
                    raise RuntimeError("kaboom")
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "node=analyze exit_error" in m
            and "exc=RuntimeError" in m
            and "elapsed_ms=" in m
            for m in messages
        )

    def test_traces_at_debug_level_only(self, caplog):
        """DEBUG only — an INFO-level log stream must not carry the
        per-node trace, otherwise a normal poll cycle would produce
        dozens of lines per message."""
        logger = logging.getLogger("zashiki_warasi.test.node4")
        with caplog.at_level(logging.INFO, logger=logger.name):
            with node_trace(logger, "n"):
                pass
        assert not [r for r in caplog.records if "node=n" in r.getMessage()]

    def test_works_with_message_context_adapter(self, caplog):
        """The whole point of node_trace + adapter — every trace line
        carries `message_id` so grep pulls the full node lifecycle
        for one message across log."""
        logger = logging.getLogger("zashiki_warasi.test.node5")
        adapter = bind_message_context(logger, message_id="mm")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            with node_trace(adapter, "analyze"):
                pass
        assert all(
            r.message_id == "mm"
            for r in caplog.records
            if "node=analyze" in r.getMessage()
        )
