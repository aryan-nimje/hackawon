"""Common signals store + SACHET normalization. No network: the feed is served by httpx.MockTransport."""

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

import db
from config import get_settings
from main import app
from services.signals import ingest, sachet, scoring
from services.signals.models import Signal, SignalKind, SignalSeverity, SignalSource, SignalStatus
from services.signals.store import SignalStore, signal_store

FEED = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml"
NOW = datetime(2026, 10, 2, 10, 0)
PUNE_POLY = "18.40,73.70 18.40,74.00 18.65,74.00 18.65,73.70 18.40,73.70"


def cap(identifier="IMD-1", *, msg_type="Alert", severity="Severe", urgency="Immediate", certainty="Likely",
        sent="2026-10-02T15:30:00+05:30", expires="2026-10-03T15:30:00+05:30", polygon=PUNE_POLY,
        event="Heavy Rain", headline="Heavy rainfall and flooding likely in Pune", extra_info="", references="",
        status="Actual", scope="Public", language="en-IN"):
    refs = f"<references>{references}</references>" if references else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
 <identifier>{identifier}</identifier><sender>imd@example.gov.in</sender><sent>{sent}</sent>
 <status>{status}</status><msgType>{msg_type}</msgType><scope>{scope}</scope>{refs}
 <info><language>{language}</language><category>Met</category><event>{event}</event>
  <urgency>{urgency}</urgency><severity>{severity}</severity><certainty>{certainty}</certainty>
  <effective>{sent}</effective><expires>{expires}</expires><senderName>IMD Pune</senderName>
  <headline>{headline}</headline><description>Very heavy rain, waterlogging expected.</description>
  <instruction>Avoid low-lying areas.</instruction>
  <area><areaDesc>Pune district</areaDesc><polygon>{polygon}</polygon></area></info>{extra_info}
</alert>""".encode()


def rss(*links):
    items = "".join(f"<item><title>t{i}</title><link>{l}</link><pubDate>Fri, 02 Oct 2026 10:{i:02d}:00 GMT</pubDate></item>"
                    for i, l in enumerate(links))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{items}</channel></rss>'.encode()


def transport(routes):
    """routes: url -> bytes | int status | Exception"""
    def handler(request):
        r = routes.get(str(request.url))
        if isinstance(r, Exception):
            raise r
        if isinstance(r, int):
            return httpx.Response(r)
        if r is None:
            return httpx.Response(404)
        return httpx.Response(200, content=r)
    return httpx.MockTransport(handler)


# -- normalization -----------------------------------------------------------------------------------------
def test_normalizes_a_cap_alert_into_the_common_format():
    [sig] = sachet.normalize_cap(cap(), source_url="https://sachet.ndma.gov.in/x?identifier=IMD-1", fetched_at=NOW)
    assert sig.id == "sachet:IMD-1" and sig.source == SignalSource.SACHET and sig.kind == SignalKind.OFFICIAL_ALERT
    assert sig.severity == SignalSeverity.SEVERE and sig.urgency == "immediate" and sig.certainty == "likely"
    assert sig.issued_at == datetime(2026, 10, 2, 10, 0)  # +05:30 -> naive UTC
    assert sig.expires_at == datetime(2026, 10, 3, 10, 0)
    assert {"flood", "rain"} <= set(sig.hazards)
    assert sig.area == "Pune district" and sig.title.startswith("Heavy rainfall")
    assert sig.polygons and len(sig.polygons[0]) == 4  # closing point dropped, [lat, lng]
    assert 18.4 < sig.lat < 18.65 and 73.7 < sig.lng < 74.0
    assert sig.bbox == [18.4, 73.7, 18.65, 74.0]
    assert 0 < sig.weight <= 1 and sig.trust == scoring.source_trust(SignalSource.SACHET)
    assert sig.status == SignalStatus.ACTIVE


def test_no_citizen_or_reporter_fields_exist_on_a_signal():
    assert not {k for k in Signal.model_fields if "reporter" in k or "phone" in k or "contact" in k}


def test_prefers_english_info_and_ignores_other_languages():
    hindi = cap(language="hi-IN", headline="भारी बारिश").decode().split("<info>", 1)[1].rsplit("</info>", 1)[0]
    data = cap(extra_info=f"<info>{hindi}</info>")
    sigs = sachet.normalize_cap(data)
    assert len(sigs) == 1 and sigs[0].language == "en-IN"


def test_cancel_and_update_carry_references():
    [c] = sachet.normalize_cap(b'<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>C1</identifier>'
                               b'<sender>s</sender><sent>2026-10-02T10:00:00Z</sent><status>Actual</status>'
                               b'<msgType>Cancel</msgType><scope>Public</scope>'
                               b'<references>imd@x,IMD-1,2026-10-02T09:00:00Z</references></alert>')
    assert c.msg_type == "cancel" and c.references == ["sachet:IMD-1"] and c.status == SignalStatus.CANCELLED


@pytest.mark.parametrize("kwargs", [{"status": "Test"}, {"status": "Exercise"}, {"scope": "Restricted"}, {"msg_type": "Ack"}])
def test_non_public_or_non_actual_alerts_are_not_ingested(kwargs):
    assert sachet.normalize_cap(cap(**kwargs)) == []


def test_test_alerts_can_be_enabled(monkeypatch):
    monkeypatch.setenv("SACHET_INGEST_NON_ACTUAL", "true")
    get_settings.cache_clear()
    [sig] = sachet.normalize_cap(cap(status="Test"))
    assert sig.metadata["cap_status"] == "test"


def test_missing_optional_data_is_handled():
    data = cap(severity="Bogus", urgency="", certainty="???", expires="", polygon="not,a polygon at all")
    [sig] = sachet.normalize_cap(data, fetched_at=NOW)
    assert sig.severity == SignalSeverity.UNKNOWN and sig.urgency == "unknown" and sig.certainty == "unknown"
    assert sig.polygons == [] and sig.lat is None and sig.bbox is None and sig.metadata["geometry_dropped"] == 1
    assert sig.expires_at == datetime(2026, 10, 2, 10, 0) + timedelta(hours=get_settings().sachet_default_ttl_hours)
    assert sig.metadata["expires_assumed"] is True


def test_missing_sent_falls_back_to_rss_time_then_fetch_time():
    [a] = sachet.normalize_cap(cap(sent=""), fetched_at=NOW, fallback_time=datetime(2026, 10, 1, 8, 0))
    assert a.issued_at == datetime(2026, 10, 1, 8, 0) and a.metadata["issued_at_assumed"]
    [b] = sachet.normalize_cap(cap(sent=""), fetched_at=NOW)
    assert b.issued_at == NOW


@pytest.mark.parametrize("data", [
    b"", b"   ", b"not xml", b"<alert><identifier>x", b"<html><body>Service unavailable</body></html>",
    b"<alert xmlns='urn:oasis:names:tc:emergency:cap:1.2'></alert>",  # no identifier
    b"<alert><identifier>x</identifier><msgType>Alert</msgType></alert>",  # no info
    b"<alert><identifier>x</identifier><info><severity>Severe</severity></info></alert>",  # no title
    b"\xff\xfe\x00bad encoding",
])
def test_malformed_cap_raises_a_parse_error_not_something_else(data):
    with pytest.raises(sachet.SachetParseError):
        sachet.normalize_cap(data)


def test_xml_entity_bombs_and_external_entities_are_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "aaaa"><!ENTITY y "&x;&x;&x;&x;">]><alert><identifier>&y;</identifier></alert>'
    xxe = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/passwd">]><alert><identifier>&e;</identifier></alert>'
    for data in (bomb, xxe):
        with pytest.raises(sachet.SachetParseError):
            sachet.normalize_cap(data)


def test_text_is_cleaned_and_bounded():
    # raw control characters are not legal XML 1.0 at all, so a document carrying them is refused outright
    with pytest.raises(sachet.SachetParseError):
        sachet.normalize_cap(cap(headline="Rain\x00alert"))
    [sig] = sachet.normalize_cap(cap(headline="Rain   alert  \n warning " + "x" * 1000))
    assert len(sig.title) <= 300 and "  " not in sig.title and "\n" not in sig.title
    assert sachet._clean("a\x07b\x00c", 10) == "abc"


@pytest.mark.parametrize("text,ok", [
    ("18.4,73.7 18.4,74.0 18.6,74.0 18.4,73.7", True),
    ("18.4,73.7 18.4,74.0", False), ("95,73 18,74 18,75", False), ("a,b c,d e,f", False), ("18.4;73.7 1,2 3,4", False),
    ("nan,1 2,3 4,5", False), ("", False),
])
def test_polygon_parsing(text, ok):
    from services.signals import geo
    assert (geo.parse_polygon(text) is not None) is ok


# -- feed parsing / safety ---------------------------------------------------------------------------------
def test_parse_feed_rss_and_atom():
    items = sachet.parse_feed(rss("https://sachet.ndma.gov.in/a", "https://sachet.ndma.gov.in/b"))
    assert [i.link for i in items] == ["https://sachet.ndma.gov.in/a", "https://sachet.ndma.gov.in/b"]
    assert items[0].published == datetime(2026, 10, 2, 10, 0)
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>t</title><link href="https://sachet.ndma.gov.in/z"/></entry><entry><title>no link</title></entry></feed>'
    assert [i.link for i in sachet.parse_feed(atom)] == ["https://sachet.ndma.gov.in/z"]


@pytest.mark.parametrize("data", [b"", b"nope", b"<html/>", b"<alert/>"])
def test_parse_feed_rejects_non_feeds(data):
    with pytest.raises(sachet.SachetParseError):
        sachet.parse_feed(data)


@pytest.mark.parametrize("link,ok", [
    ("https://sachet.ndma.gov.in/cap?identifier=1", True), ("https://mausam.imd.gov.in/x", True),
    ("http://169.254.169.254/latest/meta-data", False), ("http://localhost:8742/health", False),
    ("file:///etc/passwd", False), ("https://evil.example.com/cap", False), ("https://gov.in.evil.com/x", False),
    ("javascript:alert(1)", False), ("", False),
])
def test_link_allowlist_blocks_ssrf(link, ok):
    assert sachet.link_allowed(link, FEED) is ok


# -- fetching ----------------------------------------------------------------------------------------------
async def _fetch(routes, **kw):
    async with httpx.AsyncClient(transport=transport(routes)) as client:
        return await sachet.fetch_sachet(client=client, **kw)


async def test_fetch_end_to_end_with_bad_items_mixed_in():
    good, bad_xml, gone, evil = (f"https://sachet.ndma.gov.in/cap?identifier={n}" for n in ("G", "B", "X", "E"))
    res = await _fetch({
        FEED: rss(good, bad_xml, gone, evil, "http://169.254.169.254/x"),
        good: cap("G"), bad_xml: b"<alert><oops", gone: 500,
        evil: b'<!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/passwd">]><alert><identifier>&e;</identifier></alert>',
    })
    assert res.ok and [s.id for s in res.signals] == ["sachet:G"]
    assert res.malformed == 3 and res.skipped_other == 1 and res.items_seen == 5
    assert all(len(e) <= 300 for e in res.errors)


@pytest.mark.parametrize("feed_response", [None, 500, b"", b"<html>maintenance</html>", httpx.ConnectTimeout("slow"), b"x" * 2_000_000])
async def test_feed_down_or_garbage_never_raises(feed_response):
    res = await _fetch({FEED: feed_response})
    assert not res.ok and res.feeds_failed == 1 and res.signals == [] and res.errors


async def test_oversized_cap_document_is_rejected(monkeypatch):
    monkeypatch.setenv("SACHET_MAX_BYTES", "5000")
    get_settings.cache_clear()
    link = "https://sachet.ndma.gov.in/cap?identifier=BIG"
    res = await _fetch({FEED: rss(link), link: cap(headline="x") + b" " * 6000})
    assert res.signals == [] and res.malformed == 1


async def test_known_urls_are_not_downloaded_again_and_max_items_applies(monkeypatch):
    monkeypatch.setenv("SACHET_MAX_ITEMS", "2")
    get_settings.cache_clear()
    links = [f"https://sachet.ndma.gov.in/cap?identifier={i}" for i in range(4)]  # newest = last
    routes = {FEED: rss(*links), **{l: cap(f"A{i}") for i, l in enumerate(links)}}
    res = await _fetch(routes, known_urls={links[3]})
    assert res.skipped_known == 1 and [s.id for s in res.signals] == ["sachet:A2"]


# -- store -------------------------------------------------------------------------------------------------
def _sig(i, **kw):
    base = dict(id=f"sachet:{i}", source=SignalSource.SACHET, kind=SignalKind.OFFICIAL_ALERT, title="t",
                issued_at=NOW, expires_at=NOW + timedelta(hours=6), fetched_at=NOW)
    return Signal(**{**base, **kw})


def test_upsert_is_idempotent():
    store = SignalStore()
    assert store.upsert_many([_sig("1"), _sig("2")]) == (2, 0)
    assert store.upsert_many([_sig("1", title="new")]) == (0, 1)
    assert store.get("sachet:1").title == "new" and len(store.list()) == 2


def test_cancel_and_update_withdraw_the_referenced_alert_even_out_of_order():
    store = SignalStore()
    store.upsert_many([_sig("1"), _sig("2")])
    store.upsert_many([_sig("c", msg_type="cancel", references=["sachet:1"], status=SignalStatus.CANCELLED),
                       _sig("3", msg_type="update", references=["sachet:2"])])
    assert store.get("sachet:1").status == SignalStatus.CANCELLED
    assert store.get("sachet:2").status == SignalStatus.SUPERSEDED and store.get("sachet:3").status == SignalStatus.ACTIVE
    store.upsert_many([_sig("1")])  # re-ingesting must not resurrect it
    assert store.get("sachet:1").status == SignalStatus.CANCELLED
    late = SignalStore()  # the cancel arrives before the alert it cancels
    late.upsert_many([_sig("c", msg_type="cancel", references=["sachet:9"], status=SignalStatus.CANCELLED)])
    late.upsert_many([_sig("9")])
    assert late.get("sachet:9").status == SignalStatus.CANCELLED


def test_prune_drops_only_long_expired(monkeypatch):
    store = SignalStore()
    store.upsert_many([_sig("old", expires_at=NOW - timedelta(days=10)), _sig("recent", expires_at=NOW - timedelta(hours=1))])
    assert store.prune(now=NOW) == 1 and store.get("sachet:recent") and not store.get("sachet:old")


def test_signals_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 's.db'}")
    get_settings.cache_clear()
    db.reset_engine()
    assert db.init_db()
    SignalStore().upsert_many([_sig("1", polygons=[[[1.0, 2.0], [1.0, 3.0], [2.0, 3.0]]], bbox=[1, 2, 2, 3])])
    again = SignalStore()
    assert again.load_from_db() == 1 and again.get("sachet:1").polygons[0][0] == [1.0, 2.0]
    db.reset_engine()


# -- scoring (configurable) --------------------------------------------------------------------------------
def test_weight_follows_severity_and_trust():
    hi, lo = _sig("h", severity=SignalSeverity.EXTREME, urgency="immediate", certainty="observed", trust=0.9), \
        _sig("l", severity=SignalSeverity.MINOR, urgency="future", certainty="possible", trust=0.9)
    assert scoring.signal_weight(hi) > scoring.signal_weight(lo) > 0
    assert scoring.signal_weight(hi) == pytest.approx(0.9)


def test_scoring_constants_are_overridable_from_env(monkeypatch):
    sig = _sig("1", severity=SignalSeverity.SEVERE, urgency="expected", certainty="likely")
    base = scoring.signal_weight(sig)
    monkeypatch.setenv("SIGNAL_TRUST_SACHET", "0.45")
    monkeypatch.setenv("SIGNAL_SEVERITY_WEIGHTS", '{"severe": 0.1}')
    monkeypatch.setenv("SIGNAL_FRESHNESS_HALF_LIFE_HOURS", "2")
    get_settings.cache_clear()
    assert scoring.source_trust(SignalSource.SACHET) == 0.45
    assert scoring.signal_weight(sig) < base / 2
    assert scoring.severity_weights()["extreme"] == scoring.DEFAULT_SEVERITY_WEIGHTS["extreme"]  # others keep defaults
    assert scoring.freshness_half_life_hours() == 2


@pytest.mark.parametrize("env,val", [
    ("SIGNAL_SEVERITY_WEIGHTS", "{not json"), ("SIGNAL_SEVERITY_WEIGHTS", "[1,2]"), ("SIGNAL_SEVERITY_WEIGHTS", '{"severe": 7}'),
    ("SIGNAL_TRUST_SACHET", "5"), ("SIGNAL_FRESHNESS_HALF_LIFE_HOURS", "0"), ("SIGNAL_FRESHNESS_HALF_LIFE_HOURS", "-3"),
    ("SIGNAL_FRESHNESS_FLOOR", "2"), ("SIGNAL_COMPONENT_WEIGHTS", '{"severity":0,"certainty":0,"urgency":0}'),
])
def test_bad_scoring_config_falls_back_to_defaults(monkeypatch, env, val):
    monkeypatch.setenv(env, val)
    get_settings.cache_clear()
    assert scoring.severity_weights()["severe"] == scoring.DEFAULT_SEVERITY_WEIGHTS["severe"]
    assert scoring.source_trust(SignalSource.SACHET) == scoring.DEFAULT_SOURCE_TRUST["sachet"]
    assert scoring.freshness_half_life_hours() == scoring.DEFAULT_FRESHNESS_HALF_LIFE_HOURS
    assert scoring.freshness_floor() == scoring.DEFAULT_FRESHNESS_FLOOR
    assert sum(scoring.component_weights().values()) == pytest.approx(1.0)


def test_freshness_decays_and_stops_at_expiry_or_withdrawal():
    sig = _sig("1", weight=0.8, expires_at=NOW + timedelta(hours=48))
    assert scoring.freshness(sig, NOW) == 1.0
    assert scoring.freshness(sig, NOW + timedelta(hours=12)) == pytest.approx(0.5)  # default half-life 12h
    assert scoring.freshness(sig, NOW + timedelta(hours=6)) > scoring.freshness(sig, NOW + timedelta(hours=24))
    assert scoring.evidence_score(sig, NOW + timedelta(hours=12)) == pytest.approx(0.4)
    short = _sig("s", weight=0.8)  # expires after 6h
    assert scoring.freshness(short, NOW + timedelta(hours=7)) == 0.0
    assert scoring.evidence_score(short, NOW + timedelta(hours=7)) == 0.0
    assert scoring.evidence_score(_sig("2", weight=0.8, status=SignalStatus.CANCELLED), NOW) == 0.0
    assert scoring.freshness(_sig("3", expires_at=None, issued_at=NOW - timedelta(days=30)), NOW) == scoring.freshness_floor()


# -- ingest + API ------------------------------------------------------------------------------------------
async def test_refresh_stores_signals_and_is_safe_when_feed_is_down():
    link = "https://sachet.ndma.gov.in/cap?identifier=R1"
    async with httpx.AsyncClient(transport=transport({FEED: rss(link), link: cap("R1")})) as c:
        s1 = await ingest.refresh_sachet(client=c)
    assert s1["ok"] and s1["added"] == 1 and signal_store.get("sachet:R1")
    async with httpx.AsyncClient(transport=transport({FEED: rss(link), link: cap("R1")})) as c:
        s2 = await ingest.refresh_sachet(client=c)  # same link again: not downloaded, not duplicated
    assert s2["skipped_known"] == 1 and s2["added"] == 0 and len(signal_store.list()) == 1
    async with httpx.AsyncClient(transport=transport({FEED: 503})) as c:
        s3 = await ingest.refresh_sachet(client=c)
    assert not s3["ok"] and len(signal_store.list()) == 1  # what we had is kept


async def test_refresh_disabled(monkeypatch):
    monkeypatch.setenv("SACHET_ENABLED", "false")
    get_settings.cache_clear()
    assert (await ingest.refresh_sachet())["disabled"] is True


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_api_lists_scores_and_filters_by_city():
    sent, exp = _iso(datetime.utcnow() - timedelta(hours=1)), _iso(datetime.utcnow() + timedelta(hours=10))
    [pune] = sachet.normalize_cap(cap("P", sent=sent, expires=exp))
    [far] = sachet.normalize_cap(cap("F", sent=sent, expires=exp, polygon="28.4,77.0 28.4,77.3 28.7,77.3 28.4,77.0"))
    nogeo = sachet.normalize_cap(cap("N", sent=sent, expires=exp, polygon=""))[0]
    gone = sachet.normalize_cap(cap("E", expires="2020-01-01T00:00:00Z", sent="2020-01-01T00:00:00Z"))[0]
    signal_store.upsert_many([pune, far, nogeo, gone])
    client = TestClient(app)
    for_sure = client.get("/signals", params={"active_only": False}).json()
    assert {s["id"] for s in for_sure} == {"sachet:P", "sachet:F", "sachet:N", "sachet:E"}
    active = {s["id"] for s in client.get("/signals").json()}
    assert active == {"sachet:P", "sachet:F", "sachet:N"}  # expired one hidden
    rows = client.get("/signals", params={"city": "pune", "source": "sachet"}).json()
    assert [s["id"] for s in rows] == ["sachet:P"] and rows[0]["score"] > 0 and rows[0]["active"]
    assert client.get("/signals", params={"city": "atlantis"}).status_code == 404
    assert client.get("/signals", params={"source": "bogus"}).status_code == 422


def test_api_status_scoring_and_refresh_endpoints(monkeypatch):
    async def fake_refresh():
        return {"ok": False, "feeds_failed": 1}
    monkeypatch.setattr(ingest, "refresh_sachet", fake_refresh)
    client = TestClient(app)
    assert client.post("/signals/sachet/refresh").json() == {"ok": False, "feeds_failed": 1}
    sc = client.get("/signals/scoring").json()
    assert sc["source_trust"]["sachet"] == 0.9 and set(sc["severity_weights"]) >= {"extreme", "unknown"}
    st = client.get("/signals/status").json()
    assert st["sachet"]["enabled"] is True and st["counts"] == {}
