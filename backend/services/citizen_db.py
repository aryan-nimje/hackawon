"""Read-only access to the DEPLOYED citizen report app's PostgreSQL database.

That app (Express + pg) inserts into `reports(id, text, location, lat, lng, need_type, urgency, source,
"timestamp", metadata)` where `metadata` is JSON: {people_count, vulnerable[], here, phone?}. The table is
reflected at runtime, so extra columns are fine. Only SELECT statements are ever issued, and the reporter's
phone number is never copied into the coordinator.

Its vocabulary differs slightly from ours, so values are translated here:
  need_type  food_water -> supplies      urgency  moderate -> medium
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import MetaData, Table, select
from sqlalchemy.engine import Engine

from config import get_settings
from db import build_engine, get_engine as get_main_engine

log = logging.getLogger("citizen_db")

CANDIDATES: Dict[str, List[str]] = {
    "id": ["id", "report_id", "uuid", "token"],
    "text": ["text", "description", "message", "details", "report_text", "body"],
    "lat": ["lat", "latitude"],
    "lng": ["lng", "lon", "long", "longitude"],
    "location": ["location_text", "location", "address", "place", "area"],
    "need_type": ["need_type", "category", "type", "need"],
    "urgency": ["urgency", "priority", "severity"],
    "timestamp": ["created_at", "timestamp", "reported_at", "submitted_at", "createdat"],
    "metadata": ["metadata", "meta"],
    "vulnerable": ["vulnerable", "vulnerable_groups"],  # only if a deployment has a dedicated column
    "people_count": ["people_count", "people", "num_people"],
}
NEED_MAP = {"food_water": "supplies"}
URGENCY_MAP = {"moderate": "medium"}
REQUIRED = ("id", "text", "lat", "lng")

_lock = threading.Lock()
_engine: Optional[Engine] = None
_engine_url: Optional[str] = None
_table: Optional[Table] = None
_columns: Dict[str, str] = {}


def configured() -> bool:
    s = get_settings()
    return bool(s.citizen_database_url or s.database_url)


def reset() -> None:
    global _engine, _engine_url, _table, _columns
    if _engine is not None:  # only a separately configured engine is ours to dispose
        _engine.dispose()
    _engine = _engine_url = _table = None
    _columns = {}


def _get_engine() -> Optional[Engine]:
    """The citizens write `reports` into the SAME database the backend uses, so by default this is the main
    engine. CITIZEN_DATABASE_URL only exists for the case where the two are split."""
    global _engine, _engine_url
    s = get_settings()
    if s.citizen_database_url and s.citizen_database_url != s.database_url:
        if _engine is None or _engine_url != s.citizen_database_url:
            _engine = build_engine(s.citizen_database_url)
            _engine_url = s.citizen_database_url
        return _engine
    return get_main_engine()


def _resolve_columns(table: Table) -> Dict[str, str]:
    present = {c.name.lower(): c.name for c in table.columns}
    overrides: Dict[str, str] = {}
    raw = get_settings().citizen_column_map
    if raw:
        try:
            overrides = {k: str(v) for k, v in json.loads(raw).items()}
        except Exception:
            log.error("CITIZEN_COLUMN_MAP is not valid JSON: ignoring it")
    mapping: Dict[str, str] = {}
    for field, names in CANDIDATES.items():
        chosen = overrides.get(field)
        if chosen and chosen.lower() in present:
            mapping[field] = present[chosen.lower()]
            continue
        for name in names:
            if name in present:
                mapping[field] = present[name]
                break
    missing = [f for f in REQUIRED if f not in mapping]
    if missing:
        raise RuntimeError(
            f"citizen reports table '{table.name}' has no column for {missing}; columns are "
            f"{sorted(present.values())}. Set CITIZEN_COLUMN_MAP to map them."
        )
    return mapping


def _load_table(engine: Engine) -> Table:
    global _table, _columns
    with _lock:
        if _table is None:
            s = get_settings()
            table = Table(s.citizen_reports_table, MetaData(), autoload_with=engine, schema=s.citizen_reports_schema)
            _columns = _resolve_columns(table)
            _table = table
            log.info("citizen reports: table %s, columns %s", table.name, _columns)
        return _table


def _as_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    if isinstance(v, str):
        v = v.strip()
        if v.startswith("["):
            try:
                return [str(x) for x in json.loads(v)]
            except Exception:
                pass
        return [x.strip() for x in v.strip("{}").split(",") if x.strip()]
    return []


def _iso(v: Any) -> str:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)  # assume UTC for naive timestamps
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def row_to_report(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One citizen-app row -> the report dict detection already understands (see data/reports.json)."""
    text = str(row.get("text") or "").strip()
    lat, lng = _as_float(row.get("lat")), _as_float(row.get("lng"))
    if not text or lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    src = _as_dict(row.get("metadata"))
    metadata: Dict[str, Any] = {"origin": "citizen_db"}  # deliberately NOT copying src["phone"]
    vulnerable = _as_list(src.get("vulnerable") if "vulnerable" in src else row.get("vulnerable"))
    if vulnerable:
        metadata["vulnerable"] = vulnerable
    people = src.get("people_count", row.get("people_count"))
    if people is not None:
        metadata["people_count"] = people
        metadata["people"] = people
    if src.get("here") is False:
        metadata["reported_for_someone_else"] = True
    need = str(row.get("need_type") or "other").lower()
    urgency = str(row.get("urgency") or "medium").lower()
    return {
        "id": f"cr-{row['id']}",
        "text": text[:2000],
        "location": str(row.get("location") or "Reported location"),
        "lat": lat,
        "lng": lng,
        "need_type": NEED_MAP.get(need, need),
        "urgency": URGENCY_MAP.get(urgency, urgency),
        "source": "citizen",
        "timestamp": _iso(row.get("timestamp")),
        "metadata": metadata,
    }


FRONTEND_VULNERABLE = {"limited_mobility": "disabled"}  # the dashboard's VulnerableGroup uses "disabled"


def to_citizen_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """A row as returned by `fetch_recent` -> the CitizenReport the dashboard expects from GET /reports and
    the SSE `report.new` event. The token is the incident id (`cr-<uuid>`) so the dashboard can match the two."""
    meta = report.get("metadata") or {}
    return {
        "token": report["id"],
        "created_at": report["timestamp"],
        "urgency": report["urgency"],
        "run_id": None,  # real reports belong to no sim run, so every run shows them
        "submission": {
            "text": report["text"],
            "lat": report["lat"],
            "lng": report["lng"],
            "accuracy_m": None,
            "location_text": report["location"],
            "need_type": report["need_type"],
            "vulnerable": [FRONTEND_VULNERABLE.get(v, v) for v in meta.get("vulnerable", [])],
            "people_count": meta.get("people_count"),
            "is_own_location": not meta.get("reported_for_someone_else", False),
            "reporter_lat": None,
            "reporter_lng": None,
            "contact": None,  # never exposed
            "website": "",
        },
    }


def fetch_recent() -> List[Dict[str, Any]]:
    """Newest reports inside the configured window. Never raises: a failure is logged and returns []."""
    engine = _get_engine()
    if engine is None:
        return []
    s = get_settings()
    try:
        table = _load_table(engine)
        cols = [table.c[name].label(field) for field, name in _columns.items()]
        stmt = select(*cols)
        ts = _columns.get("timestamp")
        if ts:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=s.citizen_reports_window_hours)
            if not getattr(table.c[ts].type, "timezone", False):
                cutoff = cutoff.replace(tzinfo=None)  # `timestamp without time zone` column: compare in UTC
            stmt = stmt.where(table.c[ts] >= cutoff).order_by(table.c[ts].desc())
        stmt = stmt.limit(s.citizen_reports_limit)
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(stmt)]
        reports = [r for r in (row_to_report(x) for x in rows) if r]
        log.info("citizen reports: %d rows read, %d usable", len(rows), len(reports))
        return reports
    except Exception:
        log.exception("could not read citizen reports")
        return []


def ping() -> Optional[bool]:
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        log.exception("citizen database ping failed")
        return False
