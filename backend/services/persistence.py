"""Write-through persistence for the in-memory stores.

The stores keep their in-memory dicts (fast reads, SSE needs them) and call these helpers on every
change. A database error is logged and never raised: a hiccup must not abort a running pipeline.
Everything is a no-op when DATABASE_URL is not set.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, TypeVar

from sqlalchemy import delete, update
from sqlmodel import Session, select

from db import get_engine
from db_models import IncidentRow, RunRow, SignalRow, SubmissionRow

log = logging.getLogger("persistence")
T = TypeVar("T")


def enabled() -> bool:
    return get_engine() is not None


def _safe(what: str, fn: Callable[[Session], T], default: T) -> T:
    engine = get_engine()
    if engine is None:
        return default
    try:
        with Session(engine) as session:
            result = fn(session)
            session.commit()
            return result
    except Exception:
        log.exception("database %s failed", what)
        return default


# -- runs ---------------------------------------------------------------------------------------------
def save_run(run: Any) -> None:
    data = run.model_dump(mode="json")

    def op(s: Session) -> None:
        existing = s.get(RunRow, run.id)
        if existing is None:
            s.add(RunRow(id=run.id, status=run.status.value, city=run.city, created_at=run.created_at,
                         updated_at=datetime.utcnow(), data=data))
        else:
            existing.status = run.status.value
            existing.city = run.city
            existing.updated_at = datetime.utcnow()
            existing.data = data

    _safe("save_run", op, None)


def set_active_run(run_id: str) -> None:
    def op(s: Session) -> None:
        s.exec(update(RunRow).values(is_active=False))
        s.exec(update(RunRow).where(RunRow.id == run_id).values(is_active=True))

    _safe("set_active_run", op, None)


def clear_active_run() -> None:
    _safe("clear_active_run", lambda s: s.exec(update(RunRow).values(is_active=False)), None)


def delete_run(run_id: str) -> None:
    _safe("delete_run", lambda s: s.exec(delete(RunRow).where(RunRow.id == run_id)), None)


def load_runs() -> tuple[List[Dict[str, Any]], Optional[str]]:
    def op(s: Session):
        rows = s.exec(select(RunRow).order_by(RunRow.created_at)).all()
        return [r.data for r in rows], next((r.id for r in rows if r.is_active), None)

    return _safe("load_runs", op, ([], None))


# -- incidents ----------------------------------------------------------------------------------------
def save_incident(incident: Any) -> None:
    data = incident.model_dump(mode="json")

    def op(s: Session) -> None:
        ts = incident.timestamp.replace(tzinfo=None) if incident.timestamp.tzinfo else incident.timestamp
        existing = s.get(IncidentRow, incident.id)
        if existing is None:
            s.add(IncidentRow(id=incident.id, source=incident.source.value, created_at=ts, data=data))
        else:
            existing.source = incident.source.value
            existing.created_at = ts
            existing.data = data

    _safe("save_incident", op, None)


def delete_incidents(ids: List[str]) -> None:
    if not ids:
        return
    _safe("delete_incidents", lambda s: s.exec(delete(IncidentRow).where(IncidentRow.id.in_(ids))), None)


def load_incidents() -> List[Dict[str, Any]]:
    return _safe("load_incidents", lambda s: [r.data for r in s.exec(select(IncidentRow)).all()], [])


# -- report submissions -------------------------------------------------------------------------------
def save_submission(rec: Dict[str, Any]) -> None:
    def op(s: Session) -> None:
        s.merge(SubmissionRow(token=rec["token"], created_at=rec["created_at"], run_id=rec.get("run_id"),
                              submission=rec["submission"]))

    _safe("save_submission", op, None)


def load_submissions(limit: int) -> List[Dict[str, Any]]:
    def op(s: Session):
        rows = s.exec(select(SubmissionRow).order_by(SubmissionRow.created_at.desc()).limit(limit)).all()
        return [{"token": r.token, "created_at": r.created_at, "submission": r.submission, "run_id": r.run_id}
                for r in reversed(rows)]

    return _safe("load_submissions", op, [])


# -- signals (external evidence) ----------------------------------------------------------------------
def save_signals(signals: List[Any]) -> None:
    """Upsert a batch in one transaction."""
    if not signals:
        return

    def op(s: Session) -> None:
        for sig in signals:
            s.merge(SignalRow(id=sig.id, source=sig.source.value, kind=sig.kind.value, status=sig.status.value,
                              issued_at=sig.issued_at, expires_at=sig.expires_at, fetched_at=sig.fetched_at,
                              data=sig.model_dump(mode="json")))

    _safe("save_signals", op, None)


def load_signals() -> List[Dict[str, Any]]:
    return _safe("load_signals", lambda s: [r.data for r in s.exec(select(SignalRow)).all()], [])


def delete_signals(ids: List[str]) -> None:
    if not ids:
        return
    _safe("delete_signals", lambda s: s.exec(delete(SignalRow).where(SignalRow.id.in_(ids))), None)


# -- strict loaders for re-syncing memory with the database ---------------------------------------------
# Unlike the load_* helpers above (which answer "empty" when the database is down), these answer None: a
# re-sync must never mistake "database unreachable" for "database is empty" and wipe memory because of it.
def load_runs_strict() -> Optional[tuple[List[Dict[str, Any]], Optional[str]]]:
    def op(s: Session):
        rows = s.exec(select(RunRow).order_by(RunRow.created_at)).all()
        return [r.data for r in rows], next((r.id for r in rows if r.is_active), None)

    return _safe("load_runs", op, None)


def load_incidents_strict() -> Optional[List[Dict[str, Any]]]:
    return _safe("load_incidents", lambda s: [r.data for r in s.exec(select(IncidentRow)).all()], None)


def load_submissions_strict(limit: int) -> Optional[List[Dict[str, Any]]]:
    def op(s: Session):
        rows = s.exec(select(SubmissionRow).order_by(SubmissionRow.created_at.desc()).limit(limit)).all()
        return [{"token": r.token, "created_at": r.created_at, "submission": r.submission, "run_id": r.run_id}
                for r in reversed(rows)]

    return _safe("load_submissions", op, None)


def load_signals_strict() -> Optional[List[Dict[str, Any]]]:
    return _safe("load_signals", lambda s: [r.data for r in s.exec(select(SignalRow)).all()], None)
