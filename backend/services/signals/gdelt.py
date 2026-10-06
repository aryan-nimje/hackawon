"""GDELT (global news index) as a signal source: DOC 2.0 API, article list mode.

Flow: per city, one query ("<city>" AND any hazard term) -> JSON article list -> `normalize_article` -> common `Signal`s.

Design points:
  * News is the weakest evidence we hold. Trust tops out at 0.5 (SIGNAL_TRUST_GDELT), so a fresh article scores at most
    0.5; an article that reports actual impact lands around 0.4-0.5, a forecast-style or vague one lower (~0.25-0.35).
  * Severity / certainty / urgency come from the HEADLINE only (we never download article pages, so there is no SSRF
    surface and no copyrighted body text; the signal keeps the title and link). They are read with fixed keyword rules.
  * Certainty rises one rung when GDELT_CORROBORATION_DOMAINS different outlets cover the same hazard for the same
    city in one refresh. Syndicated copies are not detected, so this is approximate.
  * Location is coarse: the city that was searched (its box and centre), flagged `metadata.geo = "query_city"`.
  * Everything from the network is untrusted. GDELT answers errors, rate limits and empty results in plain text or `{}`:
    all of that is a counted failure / "no articles", never an exception. `fetch_gdelt` never raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from config import get_settings
from services.signals import common, scoring
from services.signals.common import CityRef, FetchError
from services.signals.models import Signal, SignalKind, SignalSeverity, SignalSource
from services.signals.sachet import hazards_for

log = logging.getLogger("signals.gdelt")

MAX_URL_LEN = 1000
MAX_TITLE_LEN = 300
CERTAINTY_RUNGS = ["unlikely", "possible", "likely", "observed"]
_TIMESPAN = re.compile(r"^\d{1,3}(min|h|hours?|d|days?|w|weeks?|m|months?)$", re.I)

# Headline rules. Order of use: severity ladder -> forecast-style cap -> certainty / urgency.
_EXTREME = re.compile(r"\b(dead|death|deaths|killed|dies|died|drown\w*|washed away|missing|casualt\w*|collaps\w*|toll)\b", re.I)
_SEVERE = re.compile(r"\b(flooded|inundat\w*|submerg\w*|stranded|rescue\w*|evacuat\w*|(?:dam|embankment|bund) breach|cloudburst|red alert|"
                     r"swept|trapped|marooned|overflow\w*)\b", re.I)
_FORECAST = re.compile(r"\b(forecast\w*|predict\w*|expected|likely|may|could|might|warns?|warning|alert|advisory|"
                       r"to lash|to hit|brace\w*|prepar\w*)\b", re.I)
_IMPACT = re.compile(r"\b(lash\w*|hit|hits|batter\w*|wreak\w*|cause[sd]?|claims?|killed|flooded|inundat\w*|submerg\w*|"
                     r"stranded|rescued|waterlogg\w*|swept|collaps\w*|dead|death)\b", re.I)
# Flood wording the shared SACHET hazard tagger does not know (it was built for CAP alerts, not headlines)
_FLOOD_WORDS = re.compile(r"\b(submerg\w*|marooned|(?:dam|embankment|bund) breach)", re.I)
# "flooded with applications" and friends are not weather
_FLOOD_METAPHOR = re.compile(r"\bflood(?:ed|s|ing)?\s+(?:of|with)\s+(?!water|rain|sewage|mud|slush|debris)", re.I)


class GdeltParseError(ValueError):
    """The response is not a usable article list."""


class ArticleDropped(Exception):
    """A well-formed article that is deliberately not ingested. `reason` is "irrelevant" or "stale"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class GdeltResult:
    signals: List[Signal] = field(default_factory=list)
    cities_ok: int = 0
    cities_failed: int = 0
    articles_seen: int = 0
    skipped_irrelevant: int = 0  # no hazard in the headline, or a "flood of ..." metaphor
    skipped_stale: int = 0  # already past its lifetime
    skipped_other: int = 0  # unsafe or duplicate link
    malformed: int = 0
    rate_limited: bool = False  # GDELT kept answering 429: the remaining cities were not asked
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.cities_ok > 0

    def err(self, msg: str) -> None:
        if len(self.errors) < common.MAX_ERRORS_KEPT:
            self.errors.append(msg[:300])

    def summary(self) -> Dict[str, Any]:
        return {"ok": self.ok, "cities_ok": self.cities_ok, "cities_failed": self.cities_failed,
                "articles_seen": self.articles_seen, "skipped_irrelevant": self.skipped_irrelevant,
                "skipped_stale": self.skipped_stale, "skipped_other": self.skipped_other,
                "malformed": self.malformed, "rate_limited": self.rate_limited, "signals": len(self.signals),
                "errors": list(self.errors)}


# -- configuration -----------------------------------------------------------------------------------------
def query_terms() -> List[str]:
    """Hazard terms from GDELT_QUERY_TERMS: >= 3 characters, no quotes / parentheses, de-duplicated; default list if
    nothing usable is left."""
    s = get_settings()
    terms: List[str] = []
    for raw in (s.gdelt_query_terms or "").split(","):
        t = re.sub(r"[\"()]", "", " ".join(raw.split())).strip().lower()
        if len(t) >= 3 and t not in terms:
            terms.append(t)
    if not terms:
        log.warning("GDELT_QUERY_TERMS has no usable term; using the default list")
        terms = [t for t in type(s).model_fields["gdelt_query_terms"].default.split(",")]
    return terms[:20]


def timespan() -> str:
    v = (get_settings().gdelt_timespan or "").strip()
    if _TIMESPAN.match(v):
        return v
    log.warning("GDELT_TIMESPAN=%r ignored; using 24h", v)
    return "24h"


def max_records() -> int:
    n = get_settings().gdelt_max_records
    return n if 1 <= n <= 250 else 50


def ttl_hours() -> float:
    return common.positive_or(get_settings().gdelt_ttl_hours, 24.0, "GDELT_TTL_HOURS")


def corroboration_domains() -> int:
    n = get_settings().gdelt_corroboration_domains
    return n if n >= 2 else 3


def settings_in_force() -> Dict[str, Any]:
    s = get_settings()
    return {"query_terms": query_terms(), "timespan": timespan(), "max_records": max_records(),
            "source_lang": s.gdelt_source_lang, "ttl_hours": ttl_hours(),
            "corroboration_domains": corroboration_domains(), "min_interval_seconds": max(0.0, s.gdelt_min_interval_seconds)}


def build_query(city_label: str, terms: List[str], source_lang: str = "", quote_city: bool = True) -> str:
    """`"Pune" ("flood" OR "heavy rain" ...) sourcelang:english`. Quotes and parentheses cannot leak in from a label."""
    label = re.sub(r"[\"()]", "", city_label).strip()
    q = (f'"{label}"' if quote_city else label) + " (" + " OR ".join(f'"{t}"' if " " in t else t for t in terms) + ")"
    lang = re.sub(r"[^a-z]", "", (source_lang or "").lower())
    return q + (f" sourcelang:{lang}" if lang else "")


# -- parsing -----------------------------------------------------------------------------------------------
def parse_seendate(value: Any) -> Optional[datetime]:
    """GDELT's `20261006T101500Z` (naive UTC); ISO-8601 is accepted too. None when unreadable."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    try:
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return common.parse_naive_utc(v)


def parse_articles(data: Any) -> List[Any]:
    """The `articles` list of a DOC API response. `{}` (no matches) is an empty list; anything that is not an object
    with a list of articles raises GdeltParseError."""
    if not isinstance(data, dict):
        raise GdeltParseError("response is not a JSON object")
    arts = data.get("articles")
    if arts is None:
        return []
    if not isinstance(arts, list):
        raise GdeltParseError("'articles' is not a list")
    return arts


def _safe_url(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    u = url.strip()
    if not u or len(u) > MAX_URL_LEN or any(c.isspace() or ord(c) < 32 for c in u):
        return ""
    try:
        p = urlparse(u)
    except ValueError:
        return ""
    return u if p.scheme in ("http", "https") and p.hostname else ""


def _title(raw: Any) -> str:
    return common.clean_text(html.unescape(raw), MAX_TITLE_LEN) if isinstance(raw, str) else ""


def _severity_and_tone(title: str):
    """(severity, certainty, urgency) from the headline."""
    impact = bool(_IMPACT.search(title))
    forecast_only = bool(_FORECAST.search(title)) and not impact
    if _EXTREME.search(title):
        sev = SignalSeverity.EXTREME
    elif _SEVERE.search(title):
        sev = SignalSeverity.SEVERE
    else:
        sev = SignalSeverity.MODERATE
    if forecast_only:  # "IMD warns of ..." / "red alert issued": evidence of a forecast, not of impact
        if common.SEVERITY_RANK[sev] > common.SEVERITY_RANK[SignalSeverity.MODERATE]:
            sev = SignalSeverity.MODERATE
        return sev, "possible", "expected"
    if impact:
        return sev, "likely", "immediate"
    return sev, "possible", "expected"


def normalize_article(art: Any, city: CityRef, now: datetime, fetched_at: Optional[datetime] = None) -> Signal:
    """One GDELT article -> a Signal (weight not yet final: see `fetch_gdelt`, which applies corroboration).

    Raises ArticleDropped for a well-formed article that is deliberately skipped (no hazard in the headline, already
    past its lifetime) and ValueError when it is malformed (not an object, no usable link or title)."""
    if not isinstance(art, dict):
        raise ValueError("article is not an object")
    url = _safe_url(art.get("url"))
    if not url:
        raise ValueError("article has no usable http(s) link")
    title = _title(art.get("title"))
    if not title:
        raise ValueError("article has no title")
    hazards = hazards_for(title)
    if _FLOOD_METAPHOR.search(title):
        hazards = [h for h in hazards if h != "flood"]
    if _FLOOD_WORDS.search(title) and "flood" not in hazards:
        hazards.append("flood")
    if not hazards:
        raise ArticleDropped("irrelevant")
    fetched_at = fetched_at or now
    seen = parse_seendate(art.get("seendate"))
    notes: Dict[str, Any] = {"geo": "query_city"}
    if seen is None or seen > now + timedelta(hours=1):  # missing, unreadable or from the future
        seen = fetched_at
        notes["issued_at_assumed"] = True
    expires = seen + timedelta(hours=ttl_hours())
    if expires <= now:
        raise ArticleDropped("stale")
    severity, certainty, urgency = _severity_and_tone(title)
    domain = common.clean_text(art.get("domain"), 100).lower() or (urlparse(url).hostname or "").lower()
    lang = common.clean_text(art.get("language"), 30)
    country = common.clean_text(art.get("sourcecountry"), 60)
    return Signal(
        id="gdelt:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
        source=SignalSource.GDELT, kind=SignalKind.NEWS, title=title, text=f"{domain}" + (f" ({country})" if country else ""),
        event="News report", hazards=hazards, severity=severity, urgency=urgency, certainty=certainty,
        area=city.label, lat=city.lat, lng=city.lng, bbox=list(city.bbox), sender=domain, source_url=url,
        language=lang, msg_type="alert", issued_at=seen, expires_at=expires, fetched_at=fetched_at,
        trust=scoring.source_trust(SignalSource.GDELT),
        metadata={**notes, "city": city.slug, "domain": domain, "source_country": country},
    )


def _rung_up(certainty: str) -> str:
    i = CERTAINTY_RUNGS.index(certainty) if certainty in CERTAINTY_RUNGS else CERTAINTY_RUNGS.index("possible")
    return CERTAINTY_RUNGS[min(i + 1, len(CERTAINTY_RUNGS) - 1)]


def finalize(signals: List[Signal]) -> List[Signal]:
    """Corroboration, then weights. Outlets covering the same hazard (one city, one refresh) raise certainty one rung
    once there are GDELT_CORROBORATION_DOMAINS of them. `metadata.corroborating_domains` records the count."""
    need = corroboration_domains()
    outlets: Dict[tuple, set] = {}
    for sig in signals:
        for hz in sig.hazards:
            outlets.setdefault((sig.metadata.get("city"), hz), set()).add(sig.metadata.get("domain"))
    for sig in signals:
        count = max((len(outlets[(sig.metadata.get("city"), hz)]) for hz in sig.hazards), default=1)
        base = sig.metadata.setdefault("base_certainty", sig.certainty)  # makes finalize safe to call again
        sig.metadata["corroborating_domains"] = count
        sig.certainty = _rung_up(base) if count >= need else base
        sig.weight = scoring.signal_weight(sig)
    return signals


# -- network -----------------------------------------------------------------------------------------------
async def _query(client: httpx.AsyncClient, params: Dict[str, Any]) -> Any:
    """One GDELT request. A 429 is retried GDELT_RETRIES times, waiting what GDELT asked (Retry-After) or an
    exponentially growing pause, at most two minutes; the last 429 is raised."""
    s = get_settings()
    attempt = 0
    while True:
        try:
            return await common.get_json_bounded(client, s.gdelt_api_url, params, s.gdelt_max_bytes)
        except FetchError as exc:
            if exc.status != 429 or attempt >= max(0, s.gdelt_retries):
                raise
            base = max(0.0, s.gdelt_min_interval_seconds)
            wait = min(120.0, max(exc.retry_after or 0.0, base * 2 ** (attempt + 1)))
            attempt += 1
            log.warning("GDELT answered 429; retry %d/%d in %.0fs", attempt, s.gdelt_retries, wait)
            await asyncio.sleep(wait)


async def fetch_gdelt(cities: List[CityRef], client: Optional[httpx.AsyncClient] = None,
                      now: Optional[datetime] = None) -> GdeltResult:
    """Query GDELT once per city (spaced out: GDELT allows one request per 5 s) and normalize the articles. Never
    raises; a city whose request fails is counted and skipped."""
    s = get_settings()
    result = GdeltResult()
    now = now or datetime.utcnow()
    own = client is None
    client = client or httpx.AsyncClient(
        timeout=s.gdelt_timeout_seconds, headers={"User-Agent": "disaster-relief-coordinator/1.0 (decision support)"})
    terms = query_terms()
    try:
        for n, city in enumerate(cities):
            if n:
                await asyncio.sleep(max(0.0, s.gdelt_min_interval_seconds))
            params = {"query": build_query(city.label, terms, s.gdelt_source_lang), "mode": "artlist", "format": "json",
                      "maxrecords": max_records(), "timespan": timespan(), "sort": "datedesc"}
            try:
                try:
                    articles = parse_articles(await _query(client, params))
                except FetchError as exc:
                    if "too short" not in str(exc):
                        raise
                    # GDELT rejected a quoted phrase as too short: ask again once with the city name unquoted.
                    await asyncio.sleep(max(0.0, s.gdelt_min_interval_seconds))
                    params = {**params, "query": build_query(city.label, terms, s.gdelt_source_lang, quote_city=False)}
                    articles = parse_articles(await _query(client, params))
            except FetchError as exc:
                if "empty response" in str(exc):  # GDELT sends an empty body for some zero-result queries
                    result.cities_ok += 1
                    continue
                result.cities_failed += 1
                result.err(f"{city.slug}: {exc}")
                log.warning("GDELT query for %s failed: %s", city.slug, exc)
                if exc.status == 429:  # still limited after the retries: asking the other cities would only make it worse
                    result.rate_limited = True
                    break
                continue
            except GdeltParseError as exc:
                result.cities_failed += 1
                result.err(f"{city.slug}: {exc}")
                continue
            result.cities_ok += 1
            result.articles_seen += len(articles)
            batch: Dict[str, Signal] = {}
            for art in articles:
                try:
                    sig = normalize_article(art, city, now, fetched_at=now)
                except ArticleDropped as dropped:
                    if dropped.reason == "stale":
                        result.skipped_stale += 1
                    else:
                        result.skipped_irrelevant += 1
                    continue
                except ValueError as exc:
                    result.malformed += 1
                    result.err(f"{city.slug}: {exc}")
                    continue
                except Exception as exc:  # one odd article must not cost the batch
                    result.malformed += 1
                    result.err(f"{city.slug}: unexpected {type(exc).__name__}: {exc}")
                    continue
                if sig.id in batch:
                    result.skipped_other += 1
                else:
                    batch[sig.id] = sig
            result.signals.extend(finalize(list(batch.values())))
    except Exception as exc:  # belt and braces: the contract is "never raises"
        result.err(f"unexpected: {type(exc).__name__}: {exc}")
        log.exception("GDELT fetch crashed")
    finally:
        if own:
            await client.aclose()
    return result
