"""Fire-and-forget laya shadow classifier (v1.6.0 Phase 0).

Non-interference contract (spec `classifier-shadow`): the pipeline's
behavior is byte-identical whether this client is enabled, disabled,
crashed, or slow. Every public method swallows every exception; the
only externally visible effects are metrics and rows in
`laya_shadow_predictions`.

Execution model: bounded two-worker pool with drop-not-queue —
a `BoundedSemaphore(2)` guards submission; when both workers are busy
the sample is DROPPED (metric `dropped_saturated`), never queued, so
a laya outage can't grow memory. Each worker takes its own SQLAlchemy
session; duplicate `(message_id, laya_model_ver)` inserts are ignored
(IntegrityError → rollback), which makes poller retries idempotent.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.classifier.question import (
    is_agreement,
    load_questions_file,
)
from zashiki_warasi.classifier.truncation import build_classifier_input
from zashiki_warasi.core.config import LayaSettings
from zashiki_warasi.core.models import LayaShadowPrediction
from zashiki_warasi.core.schemas import EmailMessage
from zashiki_warasi.observability import (
    classifier_shadow_agreement_total,
    classifier_shadow_duration_seconds,
    classifier_shadow_error_total,
)

logger = logging.getLogger(__name__)

_MAX_WORKERS = 2


class LayaShadowClient:
    """Inert unless `LAYA_SHADOW_ENABLED=1` AND `LAYA_BASE_URL` set."""

    def __init__(
        self,
        *,
        settings: LayaSettings | None = None,
        session_factory: sessionmaker | None = None,
    ) -> None:
        self._settings = settings or LayaSettings()
        self._session_factory = session_factory
        self.enabled = bool(
            self._settings.shadow_enabled and self._settings.base_url
        )
        if not self.enabled:
            return

        # Fail LOUDLY here (boot time), per spec — a broken dict must
        # abort startup, not silently produce unmappable predictions.
        question, dict_hash8, zh_by_en, _ = load_questions_file(
            self._settings.questions_path
        )
        self._question = question
        self._zh_by_en = zh_by_en
        self.model_ver = f"{self._settings.semantic_ver}-{dict_hash8}"
        self._endpoint = (
            self._settings.base_url.rstrip("/") + "/v1/systemone"
        )
        self._executor = ThreadPoolExecutor(
            max_workers=_MAX_WORKERS, thread_name_prefix="laya-shadow"
        )
        self._slots = threading.BoundedSemaphore(_MAX_WORKERS)
        logger.info(
            f"laya shadow enabled: endpoint={self._endpoint} "
            f"model_ver={self.model_ver}"
        )

    def classify_async(
        self, *, email: EmailMessage, llm_category: str
    ) -> Future | None:
        """Submit one shadow classification. Returns the Future (tests
        use it for determinism) or None when disabled / dropped. NEVER
        raises."""
        if not self.enabled:
            return None
        try:
            if not self._slots.acquire(blocking=False):
                classifier_shadow_error_total.labels(
                    reason="dropped_saturated"
                ).inc()
                return None
            try:
                return self._executor.submit(
                    self._worker, email, llm_category
                )
            except BaseException:
                self._slots.release()
                raise
        except Exception:  # noqa: BLE001 — non-interference contract
            logger.exception("laya shadow: submit failed (swallowed)")
            return None

    def shutdown(self) -> None:
        if self.enabled:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # ---- worker (background thread) ----------------------------------

    def _worker(self, email: EmailMessage, llm_category: str) -> None:
        try:
            self._classify_and_persist(email, llm_category)
        except Exception:  # noqa: BLE001 — never propagate anywhere
            logger.exception("laya shadow: worker failed (swallowed)")
        finally:
            self._slots.release()

    def _classify_and_persist(
        self, email: EmailMessage, llm_category: str
    ) -> None:
        input_text = build_classifier_input(email)
        started = time.monotonic()
        laya_category = confidence = alternates = None
        error: str | None = None
        try:
            resp = httpx.post(
                self._endpoint,
                json={
                    "state": input_text,
                    "questions": {"category": self._question},
                    "model": "multilingual",
                },
                timeout=self._settings.timeout_seconds,
            )
            if resp.status_code >= 500:
                error = "http_5xx"
            else:
                answer = resp.json()["answers"]["category"]
                en_choice = answer["choice"]
                zh = self._zh_by_en.get(en_choice)
                if zh is None:
                    error = "bad_response"
                    logger.warning(
                        f"laya shadow: unmapped label {en_choice!r}"
                    )
                else:
                    laya_category = zh
                    confidence = float(answer["confidence"])
                    alternates = sorted(
                        (
                            {"cat": self._zh_by_en.get(k, k),
                             "prob": round(float(v), 4)}
                            for k, v in answer["probabilities"].items()
                        ),
                        key=lambda a: -a["prob"],
                    )
        except httpx.TimeoutException:
            error = "timeout"
        except httpx.ConnectError:
            error = "connection_refused"
        except Exception:  # noqa: BLE001 — malformed JSON, missing keys…
            error = "bad_response"
        latency_ms = int((time.monotonic() - started) * 1000)

        classifier_shadow_duration_seconds.labels(engine="laya").observe(
            latency_ms / 1000.0
        )
        if error is not None:
            classifier_shadow_error_total.labels(reason=error).inc()
        else:
            agreed = is_agreement(laya_category, llm_category)
            classifier_shadow_agreement_total.labels(
                llm_category=llm_category,
                laya_category=laya_category,
                agreed="true" if agreed else "false",
            ).inc()

        if self._session_factory is None:
            return
        with self._session_factory() as session:
            session.add(LayaShadowPrediction(
                message_id=email.id,
                input_text=input_text,
                llm_category=llm_category,
                laya_category=laya_category,
                laya_confidence=confidence,
                laya_alternates=alternates,
                laya_latency_ms=latency_ms,
                laya_error=error,
                laya_model_ver=self.model_ver,
            ))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()  # duplicate (message_id, ver) — fine
