"""OTel BatchSpanProcessor subclass with drop-counting + rate-limited
error logging.

This module is DELIBERATELY imported lazily — only inside
`configure_tracing()`'s enabled branch. That preserves the OTEL_ENABLED=0
zero-SDK-import contract: importing `zashiki_warasi.observability.tracing`
under `OTEL_ENABLED=0` does NOT reach this module, so the top-level
`from opentelemetry.sdk.trace.export import BatchSpanProcessor` below
never executes and the SDK stays out of memory.

The underscore prefix on the module name signals "internal implementation
of the observability package, not part of the public API." Callers should
route through `configure_tracing()`.

Design decision D24 in the add-observability-stack change explains why
this lives as a top-level class in its own submodule rather than as a
factory-with-inner-class (Group 4a's shipped shape) or a top-level class
in `tracing.py` (would break lazy import).
"""

from __future__ import annotations

import logging
import time

from opentelemetry.sdk.trace.export import BatchSpanProcessor

from zashiki_warasi.observability import traces_dropped_total

# One rate-limit window per (reason) key — a downed collector produces
# ONE WARNING per minute per drop category instead of one per batch.
_WINDOW_SECONDS = 60.0

logger = logging.getLogger(__name__)


class RateLimitedBSP(BatchSpanProcessor):
    """Subclass of `BatchSpanProcessor` that:

    - Counts drops on `zashiki_traces_dropped_total{reason=...}` for the
      three failure modes the SDK exposes: `queue_full` (SDK dropped an
      incoming span because the batch queue is at max), `export_failed`
      (the exporter raised on a batch), `shutdown` (in-flight spans at
      process shutdown).
    - Rate-limits WARNING lines about drops to at most one per minute
      per reason category, so a sustained collector outage does not
      spam thousands of identical lines into the log stream.

    Why subclass rather than wrap-and-delegate: the SDK's SpanProcessor
    protocol has grown methods like `_on_ending` (called from inside
    `span.end()` in newer SDK versions). Wrap-and-delegate has to
    manually forward each such method or crash with AttributeError; the
    first draft of this code shipped with wrap-and-delegate and got
    bitten by exactly this on the first `OTEL_ENABLED=1` request in
    tests. Subclassing inherits every method the SDK adds automatically.
    """

    def __init__(
        self, exporter, *, log: logging.Logger | None = None
    ) -> None:
        super().__init__(exporter)
        # Keep our own reference to the exporter so the export wrapper
        # can find it after super().__init__ (BSP stores it privately).
        self._exporter_ref = exporter
        self._log = log or logger
        self._last_warned: dict[str, float] = {}
        self._install_export_wrapper()

    def _install_export_wrapper(self) -> None:
        """Wrap `exporter.export()` to count export failures.

        Called from `__init__` so every span batch export goes through
        our counter regardless of the caller path (BSP worker thread,
        `force_flush`, `shutdown` drain).
        """
        original_export = self._exporter_ref.export

        def _export(spans):
            try:
                return original_export(spans)
            except Exception:
                traces_dropped_total.labels(reason="export_failed").inc(
                    len(spans)
                )
                self._warn_rate_limited("export_failed")
                # Re-raise so BSP's internal bookkeeping (its own
                # failed-batch counter) still fires. BSP catches this
                # itself and doesn't propagate to the app path.
                raise

        self._exporter_ref.export = _export

    def on_end(self, span) -> None:
        # Sample the queue length BEFORE calling super so we can detect
        # the "queue was already full" case (which means the SDK
        # dropped this span rather than enqueuing it). Post-call
        # sampling wouldn't work — the SDK might have made room.
        queue_size_before = len(getattr(self, "queue", []))
        super().on_end(span)
        max_size = getattr(self, "max_queue_size", None)
        if max_size is not None and queue_size_before >= max_size:
            traces_dropped_total.labels(reason="queue_full").inc()
            self._warn_rate_limited("queue_full")

    def shutdown(self) -> None:
        # Count in-flight spans as shutdown drops (best effort — some
        # may still export cleanly if `force_flush` was already called
        # before shutdown).
        remaining = len(getattr(self, "queue", []))
        if remaining > 0:
            traces_dropped_total.labels(reason="shutdown").inc(remaining)
        super().shutdown()

    def _warn_rate_limited(self, reason: str) -> None:
        """Emit a WARNING for this drop reason at most once per
        `_WINDOW_SECONDS`. Uses `time.monotonic()` so wall-clock
        adjustments (NTP sync) don't break the window."""
        now = time.monotonic()
        prior = self._last_warned.get(reason, 0.0)
        if now - prior >= _WINDOW_SECONDS:
            self._last_warned[reason] = now
            endpoint = getattr(
                self._exporter_ref, "_endpoint", "<unknown>"
            )
            self._log.warning(
                f"OTel span export dropping traces (reason={reason}). "
                f"Check collector reachability at {endpoint}."
            )
