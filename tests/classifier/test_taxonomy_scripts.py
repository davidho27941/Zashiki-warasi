"""Taxonomy-v2 operator scripts (adopt-email-taxonomy-v2, tasks 4.x)."""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.classifier.classification_store import SEED_V1_HISTORY_SQL
from zashiki_warasi.core.models import Base, EmailClassification
from zashiki_warasi.core.models import EmailAnalysis as EmailAnalysisORM
from zashiki_warasi.core.schemas import EmailAnalysis

_ROOT = Path(__file__).resolve().parents[2]


def _load(relpath: str):
    path = _ROOT / relpath
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _squash(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_seed_sql_matches_migration_copy():
    migration = _load("alembic/versions/0009_email_classifications.py")
    assert _squash(migration.SEED_SQL) == _squash(SEED_V1_HISTORY_SQL)


# ---------- import_human_labels ----------


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        for mid in ("m1", "m2", "m3"):
            s.add(EmailAnalysisORM(
                message_id=mid, importance=3, urgency="normal",
                category="廣告", taxonomy_version="v1", summary="s",
                keywords=[],
                analyzed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            ))
        s.flush()
        s.execute(text(SEED_V1_HISTORY_SQL))
        s.commit()
        yield s


def _verdict(mid, verified, at, blind=True):
    return json.dumps({
        "message_id": mid, "llm_original": "廣告", "verified": verified,
        "verified_at": at, "blind": blind,
    }, ensure_ascii=False)


def test_import_last_wins_and_rejects(session):
    script = _load("scripts/import_human_labels.py")
    verdicts = script.load_verdicts([
        _verdict("m1", "促銷", "2026-10-05T00:00:00+00:00"),
        _verdict("m1", "講座資訊", "2026-10-05T01:00:00+00:00"),  # undo
        _verdict("m2", "廣告", "2026-10-05T02:00:00+00:00", blind=False),
        _verdict("m3", "行銷", "2026-10-05T03:00:00+00:00"),  # v2 name
        _verdict("ghost", "廣告", "2026-10-05T04:00:00+00:00"),
    ])
    counts, rejected = script.import_verdicts(session, verdicts, "v1")
    assert counts["versioned"] == 1 and counts["rejected"] == 3, counts
    assert any("m2: not a blind" in r for r in rejected)
    assert any("m3: '行銷' not in taxonomy v1" in r for r in rejected)
    assert any("ghost: no email_analyses row" in r for r in rejected)
    current = session.scalar(select(EmailClassification).where(
        EmailClassification.message_id == "m1",
        EmailClassification.valid_to.is_(None),
    ))
    assert (current.category, current.source, current.recorded_by) == (
        "講座資訊", "human", "import")


def test_import_is_rerunnable(session):
    script = _load("scripts/import_human_labels.py")
    verdicts = script.load_verdicts([
        _verdict("m1", "促銷", "2026-10-05T00:00:00+00:00"),
    ])
    script.import_verdicts(session, verdicts, "v1")
    counts, _ = script.import_verdicts(session, verdicts, "v1")
    assert counts["unchanged"] == 1


# ---------- check_pending_threads ----------


class _FakeSaver:
    def __init__(self, states):
        self._states = states

    def get_tuple(self, config):
        values = self._states.get(config["configurable"]["thread_id"])
        if values is None:
            return None
        return SimpleNamespace(checkpoint={"channel_values": values})


def test_find_pending_lists_only_threads_holding_an_analysis():
    script = _load("scripts/check_pending_threads.py")
    v1 = EmailAnalysis(
        importance=3, urgency="normal", category="消費支出", summary="s",
    )
    saver = _FakeSaver({
        "t-pending": {"analysis": v1},
        "t-failed": {"analysis": None},
        "t-fresh": {},
    })
    pending = script.find_pending(
        saver, ["t-pending", "t-failed", "t-fresh", "t-gone"],
    )
    assert pending == [("t-pending", "消費支出", "v1")]


def test_import_rejects_record_from_another_taxonomy(session):
    script = _load("scripts/import_human_labels.py")
    line = json.dumps({
        "message_id": "m1", "verified": "其他", "blind": True,
        "verified_at": "2026-10-05T00:00:00+00:00", "taxonomy_version": "v2",
    }, ensure_ascii=False)
    counts, rejected = script.import_verdicts(
        session, script.load_verdicts([line]), "v1",
    )
    assert counts["rejected"] == 1
    assert "labeled under v2, declared v1" in rejected[0]
