"""In-memory citizen report store with a time-based status timeline."""

from __future__ import annotations

import secrets
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

MAX_REPORTS = 5000

# (status, seconds after creation, citizen-facing summary). A time-based timeline for now;
# swap `timeline_for` for real run/plan state later.
STEPS: List[Tuple[str, int, str]] = [
    ("received", 0, "Your report has been received."),
    ("verifying", 6, "We are checking the details of your report."),
    ("prioritized", 15, "Your report has been prioritized by urgency."),
    ("assigned", 30, "A response team has been assigned to your report."),
    ("resolved", 75, "This report has been marked as resolved."),
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ReportStore:
    def __init__(self) -> None:
        self._reports: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def add(self, submission: Dict[str, Any]) -> Dict[str, Any]:
        token = secrets.token_urlsafe(12)
        rec = {"token": token, "created_at": iso(now_utc()), "submission": submission}
        self._reports[token] = rec
        while len(self._reports) > MAX_REPORTS:
            self._reports.popitem(last=False)
        return rec

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        return self._reports.get(token)

    def list(self) -> List[Dict[str, Any]]:
        return list(self._reports.values())

    @staticmethod
    def status_view(rec: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or now_utc()
        created = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
        elapsed = (now - created).total_seconds()
        history = [
            {"status": s, "at": iso(created + timedelta(seconds=off))}
            for s, off, _ in STEPS
            if elapsed >= off
        ]
        current = history[-1]
        summary = next(text for s, _, text in STEPS if s == current["status"])
        return {
            "token": rec["token"],
            "status": current["status"],
            "updated_at": current["at"],
            "history": history,
            "summary": summary,
        }


report_store = ReportStore()
