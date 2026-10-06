"""PostgreSQL engine for the coordinator's own data (SQLModel / SQLAlchemy).

`DATABASE_URL` unset -> `get_engine()` returns None and every store stays purely in-memory, so local
runs and the test-suite work without a database.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import ArgumentError, NoSuchModuleError
from sqlalchemy.engine.url import make_url
from sqlmodel import SQLModel, create_engine

from config import get_settings

log = logging.getLogger("db")

_engine: Optional[Engine] = None
_engine_url: Optional[str] = None


_URL_RE = re.compile(r"postgres(?:ql)?(?:\+\w+)?://\S+", re.I)


def mask_url(raw: str) -> str:
    """Hide the password so a URL can be shown in an error message."""
    return re.sub(r"(://[^:/@\s]*:)[^@\s]*(@)", r"\1***\2", raw.strip())[:200]


def clean_url(raw: str) -> str:
    """Forgive common paste mistakes: surrounding quotes/spaces, a `DATABASE_URL=` prefix, a whole
    `psql <url>` command, or a trailing `# comment`."""
    s = raw.strip().strip("'\"").strip()
    m = _URL_RE.search(s)
    return m.group(0).rstrip("'\"") if m else s


def normalize_url(url: str) -> str:
    """Hosts hand out postgres:// or postgresql://; SQLAlchemy needs the psycopg (v3) driver spelled out."""
    url = clean_url(url)
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def build_engine(url: str, *, pool: bool = True) -> Engine:
    try:
        norm = normalize_url(url)
        if re.search(r"[<>{}]", norm) or re.search(r"\[(?![0-9a-fA-F:.]+\])", norm):  # template placeholders left in
            raise ArgumentError("placeholder left in URL")
        backend = make_url(norm).get_backend_name()
    except (ArgumentError, NoSuchModuleError) as exc:
        raise RuntimeError(
            "DATABASE_URL is not a valid database URL.\n"
            "  It must look exactly like:  postgresql://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require\n"
            "  (no <angle brackets>, no [square brackets], no spaces, no extra text before 'postgresql://')\n"
            f"  What the backend read (password hidden):  {mask_url(url)!r}"
        ) from exc
    kwargs: dict = {"pool_pre_ping": True}
    if backend == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
    elif pool:
        kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
    return create_engine(norm, **kwargs)


def get_engine() -> Optional[Engine]:
    global _engine, _engine_url
    url = get_settings().database_url
    if not url:
        return None
    if _engine is None or _engine_url != url:
        _engine = build_engine(url)
        _engine_url = url
    return _engine


def reset_engine() -> None:
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None


def init_db() -> bool:
    """Create missing tables. Returns True when a database is configured."""
    engine = get_engine()
    if engine is None:
        log.warning("DATABASE_URL not set: runs, incidents and reports are kept in memory only")
        return False
    import db_models  # noqa: F401  (registers the tables on SQLModel.metadata)

    SQLModel.metadata.create_all(engine)
    log.info("database ready (%s)", engine.url.render_as_string(hide_password=True))
    return True


def ping() -> Optional[bool]:
    """None = no database configured, True/False = reachable or not."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        log.exception("database ping failed")
        return False
