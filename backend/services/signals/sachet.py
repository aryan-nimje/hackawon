"""SACHET (NDMA's national CAP alert hub): the official-alert source.

Flow: CAP RSS feed (country or state level) -> each item links to a CAP 1.2 XML alert -> `normalize_cap` -> common
`Signal`s. Everything from the network is untrusted: XML is parsed with defusedxml (no entity / DTD tricks), every
download is size-capped, CAP links must stay on the feed's host or a *.gov.in host, redirects are not followed, and a
bad item is counted and skipped, never raised. `fetch_sachet` never raises.

Notes on the data: SACHET alerts are district level (CAP polygons, "lat,lng" pairs in WGS84, converted here to
[lat, lng] rings with a centroid and bbox); one alert may carry several <info> blocks, one per language, and we keep
the preferred language. Alert text is free text from outside: treat it as untrusted if it is ever shown to an LLM.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from config import get_settings
from services.signals import geo, scoring
from services.signals.models import Signal, SignalKind, SignalSeverity, SignalSource, SignalStatus

log = logging.getLogger("signals.sachet")

MAX_AREAS = 60  # per <info>
MAX_POLYGON_POINTS = 5000
MAX_GEOMETRIES = 80
MAX_ERRORS_KEPT = 10
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SEVERITY_MAP = {s.value: s for s in SignalSeverity}
URGENCY_VALUES = {"immediate", "expected", "future", "past", "unknown"}
CERTAINTY_VALUES = {"observed", "likely", "possible", "unlikely", "unknown"}
# Normalized hazard tags from free text (event + headline + description). First column = tag.
HAZARD_KEYWORDS = {
    "flood": ("flood", "inundat", "waterlog", "water log", "overflow", "dam release"),
    "rain": ("rain", "downpour", "cloudburst", "cloud burst", "precipitation", "thunderstorm"),
    "cyclone": ("cyclone", "deep depression", "depression", "storm surge"),
    "landslide": ("landslide", "land slide", "mudslide", "rockfall", "avalanche"),
    "wind": ("gale", "squall", "strong wind", "gusty", "high wind"),
    "lightning": ("lightning", "thunderstorm"),
    "heat": ("heat wave", "heatwave", "hot weather"),
    "cold": ("cold wave", "coldwave", "fog", "frost"),
    "coastal": ("high wave", "swell", "tsunami", "sea condition", "rough sea"),
    "earthquake": ("earthquake", "seismic", "tremor"),
    "fire": ("forest fire", "wildfire", "fire"),
}


class SachetParseError(ValueError):
    """The document is not a usable CAP alert / RSS feed."""


@dataclass
class FeedItem:
    title: str = ""
    link: str = ""
    guid: str = ""
    published: Optional[datetime] = None
    author: str = ""


@dataclass
class SachetResult:
    signals: List[Signal] = field(default_factory=list)
    feeds_ok: int = 0
    feeds_failed: int = 0
    items_seen: int = 0
    fetched: int = 0
    skipped_known: int = 0
    skipped_other: int = 0  # unsafe link, non-public scope, test/exercise, ack/error
    malformed: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.feeds_ok > 0

    def err(self, msg: str) -> None:
        if len(self.errors) < MAX_ERRORS_KEPT:
            self.errors.append(msg[:300])

    def summary(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "feeds_ok": self.feeds_ok, "feeds_failed": self.feeds_failed,
            "items_seen": self.items_seen, "fetched": self.fetched, "skipped_known": self.skipped_known,
            "skipped_other": self.skipped_other, "malformed": self.malformed,
            "signals": len(self.signals), "errors": list(self.errors),
        }


# -- small helpers -----------------------------------------------------------------------------------------
def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _clean(text: Optional[str], limit: int) -> str:
    return _CTRL.sub("", " ".join((text or "").split()))[:limit]


def _children(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _child(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el, name: str, limit: int = 500) -> str:
    c = _child(el, name)
    return _clean(c.text, limit) if c is not None else ""


def parse_time(value: Optional[str]) -> Optional[datetime]:
    """ISO-8601 (CAP) or RFC-822 (RSS) -> naive UTC. None when it cannot be read."""
    v = (value or "").strip()
    if not v:
        return None
    dt: Optional[datetime] = None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(v)
        except (TypeError, ValueError, IndexError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    if not 1990 <= dt.year <= 2100:
        return None
    return dt


def _parse_xml(data: bytes, what: str):
    if not data or not data.strip():
        raise SachetParseError(f"{what}: empty document")
    try:
        return SafeET.fromstring(data)
    except (SafeET.ParseError, DefusedXmlException, ValueError) as exc:
        raise SachetParseError(f"{what}: not valid XML ({type(exc).__name__})") from exc


def cap_identifier_from_url(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    return (q.get("identifier") or [""])[0]


_HAZARD_RE = {tag: re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + ")") for tag, words in HAZARD_KEYWORDS.items()}


def hazards_for(*texts: str) -> List[str]:
    """Hazard tags whose keywords start a word in the text ("rain" matches "rainfall", not "train")."""
    blob = " ".join(texts).lower()
    return [tag for tag, rx in _HAZARD_RE.items() if rx.search(blob)]


# -- RSS ---------------------------------------------------------------------------------------------------
def parse_feed(data: bytes) -> List[FeedItem]:
    """RSS 2.0 (what SACHET serves) or Atom. Items with no usable link are dropped. Raises SachetParseError when the
    document is not a feed at all."""
    root = _parse_xml(data, "feed")
    if _local(root.tag) not in ("rss", "feed", "rdf"):
        raise SachetParseError("feed: not an RSS/Atom document")
    items: List[FeedItem] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        link = _text(el, "link", 1000)
        if not link:  # Atom: <link href="..."/>
            for l in _children(el, "link"):
                if l.get("href"):
                    link = _clean(l.get("href"), 1000)
                    break
        if not link:
            continue
        items.append(FeedItem(
            title=_text(el, "title", 300), link=link, guid=_text(el, "guid", 300) or _text(el, "id", 300),
            published=parse_time(_text(el, "pubdate", 100) or _text(el, "updated", 100) or _text(el, "published", 100)),
            author=_text(el, "author", 200),
        ))
    return items


def link_allowed(link: str, feed_url: str) -> bool:
    """CAP links must be http(s) on the feed's own host or a *.gov.in host: a hostile feed cannot aim us elsewhere."""
    try:
        u, f = urlparse(link), urlparse(feed_url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        return False
    return host == (f.hostname or "").lower() or host == "gov.in" or host.endswith(".gov.in")


# -- CAP -> Signal -----------------------------------------------------------------------------------------
def _refs(text: str) -> List[str]:
    """CAP <references>: space-separated "sender,identifier,sent" -> ["sachet:<identifier>", ...]."""
    out = []
    for token in (text or "").split():
        parts = token.split(",")
        if len(parts) >= 2 and parts[1]:
            out.append(f"sachet:{parts[1][:200]}")
    return out[:50]


def _area_geometry(infos_areas: list, notes: Dict[str, Any]):
    polygons: List[geo.Ring] = []
    circles: List[List[float]] = []
    names: List[str] = []
    dropped = 0
    for area in infos_areas[:MAX_AREAS]:
        name = _text(area, "areadesc", 200)
        if name:
            names.append(name)
        for p in _children(area, "polygon"):
            raw = (p.text or "")
            ring = geo.parse_polygon(raw) if len(raw) < MAX_POLYGON_POINTS * 24 else None
            if ring is None or len(ring) > MAX_POLYGON_POINTS or len(polygons) >= MAX_GEOMETRIES:
                dropped += 1
            else:
                polygons.append(ring)
        for c in _children(area, "circle"):
            circ = geo.parse_circle(c.text or "")
            if circ is None or len(circles) >= MAX_GEOMETRIES:
                dropped += 1
            else:
                circles.append(circ)
    if dropped:
        notes["geometry_dropped"] = dropped
    return polygons, circles, names


def _choose_infos(infos: list) -> list:
    """Infos in the preferred language (all of them), else those of the first info's language."""
    if not infos:
        return []
    pref = get_settings().sachet_preferred_language.lower()

    def lang(i) -> str:
        return _text(i, "language", 20).lower()

    wanted = [i for i in infos if lang(i).startswith(pref)] if pref else []
    if wanted:
        return wanted
    first = lang(infos[0])
    return [i for i in infos if lang(i) == first]


def normalize_cap(data: bytes, *, source_url: str = "", fetched_at: Optional[datetime] = None,
                  fallback_time: Optional[datetime] = None) -> List[Signal]:
    """One CAP document -> 0..n Signals. Raises SachetParseError when it is not a usable CAP alert; returns [] for a
    valid alert that is deliberately not ingested (non-public scope, test/exercise unless enabled, ack/error)."""
    s = get_settings()
    fetched_at = fetched_at or datetime.utcnow()
    root = _parse_xml(data, "cap")
    if _local(root.tag) != "alert":
        raise SachetParseError("cap: root element is not <alert>")

    identifier = _text(root, "identifier", 200)
    if not identifier:
        raise SachetParseError("cap: missing <identifier>")
    msg_type = (_text(root, "msgtype", 20) or "alert").lower()
    if msg_type in ("ack", "error"):
        return []
    if msg_type not in ("alert", "update", "cancel"):
        msg_type = "alert"
    if (_text(root, "scope", 20) or "public").lower() != "public":
        return []
    cap_status = (_text(root, "status", 20) or "actual").lower()
    if cap_status != "actual" and not s.sachet_ingest_non_actual:
        return []

    sent = parse_time(_text(root, "sent", 60))
    notes: Dict[str, Any] = {}
    if sent is None:
        sent = fallback_time or fetched_at
        notes["issued_at_assumed"] = True
    if cap_status != "actual":
        notes["cap_status"] = cap_status
    sender = _text(root, "sender", 200)
    refs = _refs(_text(root, "references", 2000))
    infos = _children(root, "info")

    def finish(sig: Signal) -> Signal:
        sig.weight = scoring.signal_weight(sig)
        return sig

    common = dict(source=SignalSource.SACHET, kind=SignalKind.OFFICIAL_ALERT, sender=sender, source_url=source_url,
                  msg_type=msg_type, references=refs, issued_at=sent, fetched_at=fetched_at,
                  trust=scoring.source_trust(SignalSource.SACHET))

    if msg_type == "cancel":
        note = _clean(_text(root, "note", 500), 500)
        return [finish(Signal(id=f"sachet:{identifier}", title="Alert cancelled", text=note, status=SignalStatus.CANCELLED,
                              expires_at=sent + timedelta(hours=s.sachet_default_ttl_hours), metadata=notes, **common))]

    chosen = _choose_infos(infos)
    if not chosen:
        raise SachetParseError("cap: no <info> block")
    out: List[Signal] = []
    for n, info in enumerate(chosen):
        inotes = dict(notes)
        event = _text(info, "event", 200)
        headline = _text(info, "headline", 300)
        description = _clean(_text(info, "description", 4000), 2000)
        title = headline or event
        if not title:
            raise SachetParseError("cap: <info> has neither headline nor event")
        sev_raw = _text(info, "severity", 20).lower()
        urg_raw = _text(info, "urgency", 20).lower()
        cert_raw = _text(info, "certainty", 20).lower()
        severity = SEVERITY_MAP.get(sev_raw, SignalSeverity.UNKNOWN)
        effective = parse_time(_text(info, "effective", 60)) or parse_time(_text(info, "onset", 60))
        expires = parse_time(_text(info, "expires", 60))
        if expires is None:
            expires = (effective or sent) + timedelta(hours=s.sachet_default_ttl_hours)
            inotes["expires_assumed"] = True
        polygons, circles, names = _area_geometry(_children(info, "area"), inotes)
        centre = geo.centroid_of(polygons, circles)
        categories = [_clean(c.text, 40).lower() for c in _children(info, "category") if c.text]
        sig = Signal(
            id=f"sachet:{identifier}" + (f"#{n + 1}" if len(chosen) > 1 else ""),
            title=title, text=description, event=event,
            hazards=hazards_for(event, headline, description),
            severity=severity,
            urgency=urg_raw if urg_raw in URGENCY_VALUES else "unknown",
            certainty=cert_raw if cert_raw in CERTAINTY_VALUES else "unknown",
            instruction=_clean(_text(info, "instruction", 2000), 1000),
            area="; ".join(names)[:500], lat=centre[0] if centre else None, lng=centre[1] if centre else None,
            bbox=geo.bbox_of(polygons, circles), polygons=polygons, circles=circles,
            language=_text(info, "language", 20), effective_at=effective, expires_at=expires,
            metadata={**inotes, "category": categories[:5], "sender_name": _text(info, "sendername", 200),
                      "response_type": [_clean(r.text, 30) for r in _children(info, "responsetype") if r.text][:5]},
            **common,
        )
        out.append(finish(sig))
    return out


# -- network -----------------------------------------------------------------------------------------------
async def _get_bounded(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes:
    """GET with a hard size cap and no redirects. Raises on any failure."""
    async with client.stream("GET", url, follow_redirects=False) as resp:
        resp.raise_for_status()
        if int(resp.headers.get("content-length") or 0) > max_bytes:
            raise ValueError("document too large")
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) > max_bytes:
                raise ValueError("document too large")
        return bytes(buf)


async def fetch_sachet(known_urls: Optional[set] = None, client: Optional[httpx.AsyncClient] = None) -> SachetResult:
    """Fetch every configured feed and normalize new alerts. Never raises. `known_urls`: CAP links already stored
    (CAP messages are immutable: an update has a new identifier), so they are not downloaded again."""
    s = get_settings()
    result = SachetResult()
    known_urls = known_urls or set()
    own = client is None
    client = client or httpx.AsyncClient(
        timeout=s.sachet_timeout_seconds, headers={"User-Agent": "disaster-relief-coordinator/1.0 (decision support)"})
    sem = asyncio.Semaphore(max(1, s.sachet_concurrency))
    try:
        for feed_url in s.sachet_feeds:
            try:
                items = parse_feed(await _get_bounded(client, feed_url, s.sachet_max_bytes))
            except Exception as exc:  # network, status, size, XML: all the same to the caller
                result.feeds_failed += 1
                result.err(f"feed {feed_url}: {type(exc).__name__}: {exc}")
                log.warning("SACHET feed %s failed: %s", feed_url, exc)
                continue
            result.feeds_ok += 1
            result.items_seen += len(items)
            items.sort(key=lambda i: i.published or datetime.min, reverse=True)
            todo: List[FeedItem] = []
            for it in items[: max(0, s.sachet_max_items)]:
                if not link_allowed(it.link, feed_url):
                    result.skipped_other += 1
                elif it.link in known_urls:
                    result.skipped_known += 1
                else:
                    todo.append(it)

            async def one(it: FeedItem) -> None:
                async with sem:
                    try:
                        data = await _get_bounded(client, it.link, s.sachet_max_bytes)
                        sigs = normalize_cap(data, source_url=it.link, fetched_at=datetime.utcnow(),
                                             fallback_time=it.published)
                    except SachetParseError as exc:
                        result.malformed += 1
                        result.err(f"{it.link}: {exc}")
                        return
                    except Exception as exc:
                        result.malformed += 1
                        result.err(f"{it.link}: {type(exc).__name__}: {exc}")
                        return
                    result.fetched += 1
                    if sigs:
                        result.signals.extend(sigs)
                    else:
                        result.skipped_other += 1

            await asyncio.gather(*(one(it) for it in todo))
    except Exception as exc:  # belt and braces: the contract is "never raises"
        result.err(f"unexpected: {type(exc).__name__}: {exc}")
        log.exception("SACHET fetch crashed")
    finally:
        if own:
            await client.aclose()
    return result
