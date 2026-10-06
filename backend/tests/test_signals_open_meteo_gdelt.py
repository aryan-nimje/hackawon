"""Open-Meteo + GDELT into the common signals store. No network: every request is served by httpx.MockTransport."""

import json
import math
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from config import get_settings
from main import app
from services.signals import common, gdelt, ingest, open_meteo, scoring
from services.signals.models import Signal, SignalKind, SignalSeverity, SignalSource, SignalStatus
from services.signals.store import signal_store

NOW = datetime(2026, 10, 6, 10, 20)
NOW_H = datetime(2026, 10, 6, 10, 0)
PUNE = common.city_ref("pune")
FORECAST_HOST, FLOOD_HOST, GDELT_HOST = "api.open-meteo.com", "flood-api.open-meteo.com", "api.gdeltproject.org"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


# -- fixtures: documents shaped like the real APIs ---------------------------------------------------------
def forecast_doc(precip=None, gust=None, temp=None, code=None, hours=96, start=None):
    """Hourly document: 24 h of past + 72 h of forecast around NOW_H. Each argument is {offset_hours: value}."""
    start = start or NOW_H - timedelta(hours=24)
    times = [start + timedelta(hours=i) for i in range(hours)]

    def col(spec, base):
        spec = spec or {}
        return [spec.get(int((t - NOW_H).total_seconds() // 3600), base) for t in times]
    return {"hourly": {"time": [iso(t) for t in times], "precipitation": col(precip, 0.0),
                       "wind_gusts_10m": col(gust, 12.0), "temperature_2m": col(temp, 28.0),
                       "weather_code": col(code, 1)}}


def flood_doc(past=10.0, peak=None, days_past=30, days_future=6, median_peak=None):
    today = NOW.replace(hour=0, minute=0)
    days = [today + timedelta(days=i) for i in range(-days_past, days_future)]
    ctrl, med = [], []
    for d in days:
        off = (d - today).days
        ctrl.append(peak.get(off, past) if peak and off >= 0 else past)
        med.append(median_peak.get(off, past) if median_peak and off >= 0 else past)
    return {"daily": {"time": [d.strftime("%Y-%m-%d") for d in days], "river_discharge": ctrl,
                      "river_discharge_median": med}}


HEAVY = {h: 6.0 for h in range(-2, 22)}  # 24 x 6 mm = 144 mm in 24 h, 6 mm/h


def mock(handlers):
    """handlers: host -> bytes/dict/list (200 JSON) | int status | Exception | callable(request) -> Response."""
    seen = []

    def handler(request):
        seen.append(request)
        r = handlers.get(request.url.host)
        if callable(r):
            return r(request)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, int):
            return httpx.Response(r)
        if r is None:
            return httpx.Response(404)
        return httpx.Response(200, content=r if isinstance(r, bytes) else json.dumps(r).encode())
    return httpx.MockTransport(handler), seen


# =====================================================================================================
# Open-Meteo: analysis
# =====================================================================================================
def test_quiet_weather_makes_no_signal():
    assert open_meteo.analyze_forecast(forecast_doc(), PUNE, NOW) == []
    assert open_meteo.analyze_flood(flood_doc(), PUNE, NOW) == []


def test_heavy_rain_becomes_a_common_format_weather_signal_with_modest_score():
    [sig] = open_meteo.analyze_forecast(forecast_doc(precip=HEAVY), PUNE, NOW)
    assert sig.id == "open_meteo:pune:rain" and sig.source == SignalSource.OPEN_METEO and sig.kind == SignalKind.WEATHER
    assert sig.severity == SignalSeverity.SEVERE and "rain" in sig.hazards  # 144 mm / 24 h >= 115.6
    assert sig.bbox == PUNE.bbox and sig.lat == PUNE.lat and sig.area == "Pune"
    assert sig.issued_at == NOW and sig.expires_at == NOW + timedelta(hours=open_meteo.ttl_hours())
    assert sig.certainty in ("likely", "possible") and sig.certainty != "observed"  # model output is never "observed"
    assert sig.metadata["city"] == "pune" and sig.metadata["endpoint"] == "forecast" and sig.metadata["total_24h_mm"] >= 115
    assert 0 < sig.weight <= sig.trust == 0.7
    assert scoring.evidence_score(sig, NOW) < 0.95  # weather does NOT start at 0.95


def test_even_the_most_extreme_weather_cannot_outscore_the_source_trust():
    doc = forecast_doc(precip={h: 40.0 for h in range(-2, 22)}, gust={1: 200.0}, temp={5: 50.0}, code={2: 99})
    sigs = open_meteo.analyze_forecast(doc, PUNE, NOW)
    assert {s.id.split(":")[-1] for s in sigs} == {"rain", "wind", "heat", "storm"}
    for s in sigs:
        assert s.severity in (SignalSeverity.EXTREME, SignalSeverity.SEVERE)
        assert scoring.evidence_score(s, NOW) <= scoring.source_trust(SignalSource.OPEN_METEO) < 0.95


def test_weather_weight_depends_on_conditions_not_a_fixed_start():
    light = open_meteo.analyze_forecast(forecast_doc(precip={h: 1.6 for h in range(-2, 22)}), PUNE, NOW)[0]  # 38 mm
    heavy = open_meteo.analyze_forecast(forecast_doc(precip=HEAVY), PUNE, NOW)[0]
    assert light.severity == SignalSeverity.MINOR and heavy.weight > light.weight > 0.1
    far = open_meteo.analyze_forecast(forecast_doc(precip={h: 6.0 for h in range(40, 64)}), PUNE, NOW)
    assert far and far[0].urgency == "future" and far[0].certainty == "possible" and far[0].weight < heavy.weight


def test_single_cloudburst_hour_counts_through_the_1h_ladder():
    [sig] = open_meteo.analyze_forecast(forecast_doc(precip={3: 55.0}), PUNE, NOW)
    assert sig.severity == SignalSeverity.EXTREME and sig.metadata["peak_1h_mm"] == 55.0


def test_wind_heat_and_thunderstorm():
    [wind] = open_meteo.analyze_forecast(forecast_doc(gust={4: 95.0}), PUNE, NOW)
    assert wind.id.endswith(":wind") and wind.severity == SignalSeverity.SEVERE and wind.hazards == ["wind"]
    [heat] = open_meteo.analyze_forecast(forecast_doc(temp={20: 44.0}), PUNE, NOW)
    assert heat.id.endswith(":heat") and heat.severity == SignalSeverity.MODERATE
    [storm] = open_meteo.analyze_forecast(forecast_doc(code={2: 96}), PUNE, NOW)
    assert storm.id.endswith(":storm") and storm.severity == SignalSeverity.SEVERE and storm.hazards == ["lightning"]
    [plain] = open_meteo.analyze_forecast(forecast_doc(code={2: 95.0}), PUNE, NOW)  # float codes are fine
    assert plain.severity == SignalSeverity.MODERATE


def test_old_rain_is_marked_past():
    [sig] = open_meteo.analyze_forecast(forecast_doc(precip={h: 6.0 for h in range(-24, -9)}), PUNE, NOW)
    assert sig.urgency == "past"


def test_river_flood_compares_forecast_peak_with_recent_median():
    [sig] = open_meteo.analyze_flood(flood_doc(past=10.0, peak={2: 62.0}, median_peak={2: 55.0}), PUNE, NOW)
    assert sig.id == "open_meteo:pune:flood" and sig.hazards == ["flood"] and sig.metadata["endpoint"] == "flood"
    assert sig.severity == SignalSeverity.SEVERE  # 6.2x >= 5
    assert sig.certainty == "likely" and sig.metadata["ensemble_median_agrees"] is True
    [solo] = open_meteo.analyze_flood(flood_doc(past=10.0, peak={2: 62.0}), PUNE, NOW)  # ensemble median disagrees
    assert solo.certainty == "possible" and solo.weight < sig.weight
    assert scoring.evidence_score(sig, NOW) <= 0.7


def test_flood_needs_a_baseline_and_a_real_river():
    assert open_meteo.analyze_flood(flood_doc(past=10.0, peak={2: 90.0}, days_past=3), PUNE, NOW) == []  # no baseline
    assert open_meteo.analyze_flood(flood_doc(past=0.5, peak={2: 3.0}), PUNE, NOW) == []  # trickle: below min discharge
    assert open_meteo.analyze_flood(flood_doc(past=0.0, peak={2: 90.0}), PUNE, NOW) == []  # zero baseline: no ratio


# -- malformed / missing data ------------------------------------------------------------------------------
@pytest.mark.parametrize("doc", [
    None, [], "text", 42, {}, {"hourly": None}, {"hourly": []}, {"hourly": {}}, {"hourly": {"time": []}},
    {"hourly": {"time": "nope"}}, {"hourly": {"time": ["garbage", None, 5]}},
    {"error": True, "reason": "Latitude must be in range of -90 to 90"},
])
def test_unusable_forecast_documents_raise_parse_error_only(doc):
    with pytest.raises(open_meteo.OpenMeteoParseError):
        open_meteo.analyze_forecast(doc, PUNE, NOW)


@pytest.mark.parametrize("doc", [None, {}, {"daily": {"time": []}}, {"daily": {"time": ["x"]}}, {"error": True}])
def test_unusable_flood_documents_raise_parse_error_only(doc):
    with pytest.raises(open_meteo.OpenMeteoParseError):
        open_meteo.analyze_flood(doc, PUNE, NOW)


def test_bad_values_inside_a_good_document_are_ignored_not_fatal():
    doc = forecast_doc(precip=HEAVY)
    h = doc["hourly"]
    h["precipitation"][30] = None
    h["precipitation"][31] = "NaN"
    h["precipitation"][32] = float("nan")
    h["precipitation"][33] = float("inf")
    h["precipitation"][34] = True
    h["precipitation"][35] = {"x": 1}
    h["wind_gusts_10m"] = "not a list"
    del h["temperature_2m"]
    h["weather_code"] = h["weather_code"][:5]  # short series
    sigs = open_meteo.analyze_forecast(doc, PUNE, NOW)
    assert [s.id for s in sigs] == ["open_meteo:pune:rain"]
    assert all(math.isfinite(s.weight) for s in sigs)


def test_rain_with_too_many_gaps_is_not_guessed():
    doc = forecast_doc(precip=HEAVY)
    doc["hourly"]["precipitation"] = [None if i % 2 else v for i, v in enumerate(doc["hourly"]["precipitation"])]
    assert open_meteo.analyze_forecast(doc, PUNE, NOW) == []  # < 18 of 24 values in every window


def test_flood_values_missing_or_malformed():
    doc = flood_doc(past=10.0, peak={2: 62.0})
    doc["daily"]["river_discharge"][5] = None
    doc["daily"]["river_discharge"][6] = "x"
    del doc["daily"]["river_discharge_median"]
    [sig] = open_meteo.analyze_flood(doc, PUNE, NOW)
    assert sig.certainty == "possible"


# -- configurable thresholds -------------------------------------------------------------------------------
def test_thresholds_are_overridable_and_partial(monkeypatch):
    monkeypatch.setenv("OPEN_METEO_RAIN_24H_MM", '{"severe": 200, "extreme": 300}')
    monkeypatch.setenv("OPEN_METEO_HEAT_C", '{"minor": 30}')
    get_settings.cache_clear()
    th = open_meteo.thresholds()
    assert th["rain_24h_mm"]["severe"] == 200 and th["rain_24h_mm"]["minor"] == 35.5  # untouched keys keep defaults
    [sig] = open_meteo.analyze_forecast(forecast_doc(precip=HEAVY), PUNE, NOW)
    assert sig.severity == SignalSeverity.MODERATE  # 144 mm is no longer "severe"
    [hot] = open_meteo.analyze_forecast(forecast_doc(temp={5: 31.0}), PUNE, NOW)
    assert hot.severity == SignalSeverity.MINOR


@pytest.mark.parametrize("val", ["{not json", "[1,2]", '{"severe": -1}', '{"bogus": 5}', '{"minor": "a"}',
                                 '{"minor": 100, "moderate": 10}', '{"severe": null}'])
def test_bad_threshold_overrides_fall_back_to_defaults(monkeypatch, val):
    monkeypatch.setenv("OPEN_METEO_RAIN_24H_MM", val)
    get_settings.cache_clear()
    assert open_meteo.thresholds()["rain_24h_mm"] == open_meteo.DEFAULT_RAIN_24H_MM


def test_trust_is_configurable_per_source_and_bad_values_fall_back(monkeypatch):
    assert scoring.source_trust(SignalSource.OPEN_METEO) == 0.7 and scoring.source_trust(SignalSource.GDELT) == 0.5
    monkeypatch.setenv("SIGNAL_TRUST_OPEN_METEO", "0.4")
    monkeypatch.setenv("SIGNAL_TRUST_GDELT", "0.3")
    get_settings.cache_clear()
    assert scoring.source_trust(SignalSource.OPEN_METEO) == 0.4 and scoring.source_trust(SignalSource.GDELT) == 0.3
    monkeypatch.setenv("SIGNAL_TRUST_OPEN_METEO", "7")
    monkeypatch.setenv("SIGNAL_TRUST_GDELT", "-1")
    get_settings.cache_clear()
    assert scoring.source_trust(SignalSource.OPEN_METEO) == 0.7 and scoring.source_trust(SignalSource.GDELT) == 0.5


def test_ttl_and_other_numbers_are_sanitised(monkeypatch):
    monkeypatch.setenv("OPEN_METEO_TTL_HOURS", "0")
    monkeypatch.setenv("OPEN_METEO_FLOOD_PAST_DAYS", "9999")
    monkeypatch.setenv("OPEN_METEO_FLOOD_HORIZON_DAYS", "-2")
    get_settings.cache_clear()
    assert open_meteo.ttl_hours() == 3.0 and open_meteo.flood_past_days() == 30 and open_meteo.flood_horizon_days() == 5


# -- fetching ----------------------------------------------------------------------------------------------
async def _om(handlers, cities=None):
    transport, seen = mock(handlers)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await open_meteo.fetch_open_meteo(cities or [PUNE], client=client, now=NOW)
    return res, seen


async def test_fetch_asks_both_apis_with_the_city_centre_and_utc_times():
    res, seen = await _om({FORECAST_HOST: forecast_doc(precip=HEAVY), FLOOD_HOST: flood_doc(past=10, peak={2: 62.0})})
    assert res.ok and res.requests_ok == 2 and res.scopes_ok == {("pune", "forecast"), ("pune", "flood")}
    assert {s.id for s in res.signals} == {"open_meteo:pune:rain", "open_meteo:pune:flood"}
    q = {r.url.host: dict(r.url.params) for r in seen}
    assert q[FORECAST_HOST]["timezone"] == "UTC" and float(q[FORECAST_HOST]["latitude"]) == round(PUNE.lat, 4)
    assert "river_discharge" in q[FLOOD_HOST]["daily"]


async def test_one_api_down_does_not_cost_the_other():
    res, _ = await _om({FORECAST_HOST: 500, FLOOD_HOST: flood_doc(past=10, peak={2: 62.0})})
    assert res.ok and res.requests_failed == 1 and [s.id for s in res.signals] == ["open_meteo:pune:flood"]
    assert res.scopes_ok == {("pune", "flood")}  # the failed scope is NOT "ok": ingest must not expire anything for it


@pytest.mark.parametrize("answer", [
    None, 500, 429, b"", b"<html>maintenance</html>", b"null", b"[]", b'{"hourly": 5}', b"\xff\xfe\x00",
    httpx.ConnectTimeout("slow"), httpx.ReadError("reset"), b"x" * 3_000_000,
    {"error": True, "reason": "bad"},
])
async def test_garbage_from_either_api_never_raises(answer):
    res, _ = await _om({FORECAST_HOST: answer, FLOOD_HOST: answer})
    assert not res.ok and res.signals == [] and res.scopes_ok == set() and res.errors
    assert res.requests_failed + res.malformed == 2 and all(len(e) <= 300 for e in res.errors)


async def test_400_reason_is_reported_and_redirects_are_not_followed():
    res, seen = await _om({
        FORECAST_HOST: lambda r: httpx.Response(400, json={"error": True, "reason": "Latitude out of range"}),
        FLOOD_HOST: lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"}),
    })
    assert any("Latitude out of range" in e for e in res.errors) and any("HTTP 302" in e for e in res.errors)
    assert {r.url.host for r in seen} == {FORECAST_HOST, FLOOD_HOST}  # nothing else was requested


async def test_oversized_response_is_rejected(monkeypatch):
    monkeypatch.setenv("OPEN_METEO_MAX_BYTES", "1000")
    get_settings.cache_clear()
    res, _ = await _om({FORECAST_HOST: forecast_doc(precip=HEAVY), FLOOD_HOST: flood_doc()})
    assert not res.ok and res.signals == []


async def test_flood_api_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("OPEN_METEO_FLOOD_ENABLED", "false")
    get_settings.cache_clear()
    res, seen = await _om({FORECAST_HOST: forecast_doc(), FLOOD_HOST: flood_doc()})
    assert {r.url.host for r in seen} == {FORECAST_HOST}


# -- ingest into the common store --------------------------------------------------------------------------
async def _refresh_om(handlers, **kw):
    transport, _ = mock(handlers)
    async with httpx.AsyncClient(transport=transport) as client:
        return await ingest.refresh_open_meteo(client=client, **kw)


async def test_refresh_writes_into_the_same_store_as_sachet_and_is_idempotent():
    signal_store.upsert_many([Signal(id="sachet:X", source=SignalSource.SACHET, kind=SignalKind.OFFICIAL_ALERT, title="t",
                                     issued_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(hours=5))])
    h = {FORECAST_HOST: lambda r: httpx.Response(200, json=forecast_doc(precip=HEAVY, start=_now_h() - timedelta(hours=24))),
         FLOOD_HOST: lambda r: httpx.Response(200, json=flood_doc())}
    s1 = await _refresh_om(h)
    assert s1["ok"] and s1["added"] == 1 and signal_store.get("open_meteo:pune:rain")
    s2 = await _refresh_om(h)
    assert s2["added"] == 0 and s2["updated"] == 1
    assert signal_store.counts() == {"sachet": 1, "open_meteo": 1}
    assert ingest.last_refresh["open_meteo"]["ok"]


def _now_h():
    return datetime.utcnow().replace(minute=0, second=0, microsecond=0)


def _heavy_now():
    return {h: 6.0 for h in range(-2, 22)}


def _live_forecast(precip=None):
    """Same as forecast_doc but anchored on the real clock (ingest uses utcnow)."""
    now_h = _now_h()
    doc = forecast_doc(start=now_h - timedelta(hours=24))
    times = [now_h - timedelta(hours=24) + timedelta(hours=i) for i in range(96)]
    doc["hourly"]["precipitation"] = [(precip or {}).get(i - 24, 0.0) for i in range(96)]
    doc["hourly"]["time"] = [iso(t) for t in times]
    return doc


async def test_a_hazard_that_has_passed_is_expired_early_but_a_failed_fetch_keeps_it():
    stormy = {FORECAST_HOST: lambda r: httpx.Response(200, json=_live_forecast(_heavy_now())),
              FLOOD_HOST: lambda r: httpx.Response(200, json=flood_doc())}
    await _refresh_om(stormy)
    sig = signal_store.get("open_meteo:pune:rain")
    assert scoring.is_in_force(sig, datetime.utcnow())

    await _refresh_om({FORECAST_HOST: 503, FLOOD_HOST: 503})  # outage: keep what we know
    assert scoring.is_in_force(signal_store.get("open_meteo:pune:rain"), datetime.utcnow())

    calm = {FORECAST_HOST: lambda r: httpx.Response(200, json=_live_forecast()),
            FLOOD_HOST: lambda r: httpx.Response(200, json=flood_doc())}
    s = await _refresh_om(calm)  # a good answer with no hazard in it
    assert s["expired"] == 1 and not scoring.is_in_force(signal_store.get("open_meteo:pune:rain"), datetime.utcnow())
    assert signal_store.get("open_meteo:pune:rain").status == SignalStatus.ACTIVE  # expired, not withdrawn

    await _refresh_om(stormy)  # and it can come back under the same id
    assert scoring.is_in_force(signal_store.get("open_meteo:pune:rain"), datetime.utcnow())


async def test_expiry_only_touches_the_scope_that_was_read_successfully():
    stormy = {FORECAST_HOST: lambda r: httpx.Response(200, json=_live_forecast(_heavy_now())),
              FLOOD_HOST: lambda r: httpx.Response(200, json=flood_doc(past=10, peak={2: 62.0}))}
    await _refresh_om(stormy)
    assert signal_store.get("open_meteo:pune:flood")
    s = await _refresh_om({FORECAST_HOST: lambda r: httpx.Response(200, json=_live_forecast()), FLOOD_HOST: 500})
    now = datetime.utcnow()
    assert not scoring.is_in_force(signal_store.get("open_meteo:pune:rain"), now)  # forecast read fine: rain is gone
    assert scoring.is_in_force(signal_store.get("open_meteo:pune:flood"), now)  # flood read failed: untouched


async def test_refresh_disabled_and_unplaceable_city(monkeypatch):
    assert (await _refresh_om({}, city="atlantis"))["errors"][0].startswith("city 'atlantis'")
    monkeypatch.setenv("OPEN_METEO_ENABLED", "false")
    get_settings.cache_clear()
    assert (await ingest.refresh_open_meteo())["disabled"] is True


# =====================================================================================================
# GDELT
# =====================================================================================================
def art(title, url=None, seen="20261006T091500Z", domain="example.in", **kw):
    return {"url": url or f"https://{domain}/{abs(hash(title)) % 10**8}", "title": title, "seendate": seen,
            "domain": domain, "language": "English", "sourcecountry": "India", **kw}


def test_gdelt_article_becomes_a_low_trust_news_signal_at_the_city():
    sig = gdelt.normalize_article(art("Pune flooded after heavy rain, residents stranded"), PUNE, NOW)
    assert sig.id.startswith("gdelt:") and sig.source == SignalSource.GDELT and sig.kind == SignalKind.NEWS
    assert {"flood", "rain"} <= set(sig.hazards) and sig.area == "Pune" and sig.bbox == PUNE.bbox
    assert sig.issued_at == datetime(2026, 10, 6, 9, 15) and sig.expires_at == sig.issued_at + timedelta(hours=24)
    assert sig.source_url.startswith("https://example.in/") and sig.sender == "example.in"
    assert sig.metadata["geo"] == "query_city" and sig.trust == 0.5


def test_gdelt_starts_around_0_4_to_0_5_for_reported_impact_and_never_near_0_85():
    [sig] = gdelt.finalize([gdelt.normalize_article(art("Pune flooded after heavy rain; residents stranded"), PUNE, NOW)])
    assert sig.severity == SignalSeverity.SEVERE and sig.certainty == "likely"
    assert 0.4 <= scoring.evidence_score(sig, sig.issued_at) <= 0.5
    [dead] = gdelt.finalize([gdelt.normalize_article(art("Three killed as Pune floods wash away vehicles"), PUNE, NOW)])
    assert dead.severity == SignalSeverity.EXTREME and 0.4 <= scoring.evidence_score(dead, dead.issued_at) <= 0.5
    [vague] = gdelt.finalize([gdelt.normalize_article(art("Heavy rain in Pune today"), PUNE, NOW)])
    [fcst] = gdelt.finalize([gdelt.normalize_article(art("IMD warns of heavy rain in Pune, red alert issued"), PUNE, NOW)])
    for s in (vague, fcst):
        assert 0.2 <= scoring.evidence_score(s, s.issued_at) < 0.4  # forecasts / vague mentions start lower
    assert fcst.severity == SignalSeverity.MODERATE and fcst.certainty == "possible"  # "red alert" is capped: it is a forecast


def test_no_gdelt_signal_can_exceed_the_source_trust_even_with_corroboration():
    arts = [gdelt.normalize_article(art("Pune dam breach: 5 dead, town submerged", domain=f"d{i}.in"), PUNE, NOW)
            for i in range(6)]
    for s in gdelt.finalize(arts):
        assert s.certainty == "observed" and s.metadata["corroborating_domains"] == 6
        assert scoring.evidence_score(s, s.issued_at) <= 0.5 < 0.85


def test_corroboration_needs_distinct_outlets(monkeypatch):
    same = [gdelt.normalize_article(art(f"Pune flooded again {i}", domain="one.in"), PUNE, NOW) for i in range(5)]
    assert {s.certainty for s in gdelt.finalize(same)} == {"likely"}  # five articles, one outlet: no boost
    many = [gdelt.normalize_article(art("Pune flooded again", domain=f"o{i}.in"), PUNE, NOW) for i in range(3)]
    assert {s.certainty for s in gdelt.finalize(many)} == {"observed"}
    monkeypatch.setenv("GDELT_CORROBORATION_DOMAINS", "5")
    get_settings.cache_clear()
    assert {s.certainty for s in gdelt.finalize(many)} == {"likely"}


def test_gdelt_trust_override_moves_every_score(monkeypatch):
    monkeypatch.setenv("SIGNAL_TRUST_GDELT", "0.2")
    get_settings.cache_clear()
    [sig] = gdelt.finalize([gdelt.normalize_article(art("Pune flooded, residents stranded"), PUNE, NOW)])
    assert scoring.evidence_score(sig, sig.issued_at) <= 0.2


@pytest.mark.parametrize("bad", [
    None, "string", 5, [], {}, {"title": "Pune flood"}, {"url": "https://a.in/x"}, {"url": "https://a.in/x", "title": "  "},
    {"url": "javascript:alert(1)", "title": "Pune flood"}, {"url": "file:///etc/passwd", "title": "Pune flood"},
    {"url": "ftp://a.in/x", "title": "Pune flood"}, {"url": "https://", "title": "Pune flood"},
    {"url": "https://a.in/" + "x" * 2000, "title": "Pune flood"}, {"url": "https://a.in/a b", "title": "Pune flood"},
    {"url": 12, "title": "Pune flood"}, {"url": "https://a.in/x", "title": 12},
])
def test_malformed_articles_raise_value_error_only(bad):
    with pytest.raises(ValueError):
        gdelt.normalize_article(bad, PUNE, NOW)


def test_dropped_articles_have_a_reason():
    for title in ("Pune wins the cricket match", "Pune flooded with applications for new jobs"):
        with pytest.raises(gdelt.ArticleDropped) as e:
            gdelt.normalize_article(art(title), PUNE, NOW)
        assert e.value.reason == "irrelevant"
    with pytest.raises(gdelt.ArticleDropped) as e:
        gdelt.normalize_article(art("Pune flooded", seen="20261001T000000Z"), PUNE, NOW)
    assert e.value.reason == "stale"
    assert gdelt.normalize_article(art("Pune flooded with rain water"), PUNE, NOW)  # a real flood still counts


def test_missing_bad_or_future_seendate_falls_back_to_fetch_time():
    for seen in (None, "", "garbage", "20269999T999999Z", 12345, "20271231T000000Z"):
        sig = gdelt.normalize_article(art("Pune flooded", seen=seen), PUNE, NOW)
        assert sig.issued_at == NOW and sig.metadata["issued_at_assumed"] is True
    assert gdelt.parse_seendate("2026-10-06T09:15:00Z") == datetime(2026, 10, 6, 9, 15)


def test_html_entities_and_control_characters_in_titles_are_cleaned():
    sig = gdelt.normalize_article(art("Pune&nbsp;flooded &amp; \x00stranded\n  " + "x" * 600), PUNE, NOW)
    assert "\x00" not in sig.title and "\n" not in sig.title and "&amp;" not in sig.title and len(sig.title) <= 300


def test_ids_are_stable_per_link():
    a = gdelt.normalize_article(art("Pune flooded", url="https://a.in/1"), PUNE, NOW)
    b = gdelt.normalize_article(art("Pune flooded (updated)", url="https://a.in/1"), PUNE, NOW)
    c = gdelt.normalize_article(art("Pune flooded", url="https://a.in/2"), PUNE, NOW)
    assert a.id == b.id != c.id


@pytest.mark.parametrize("data,expected", [({}, 0), ({"articles": None}, 0), ({"articles": []}, 0), ({"articles": [1, 2]}, 2)])
def test_parse_articles_accepts_empty_results(data, expected):
    assert len(gdelt.parse_articles(data)) == expected


@pytest.mark.parametrize("data", [None, [], "x", 3, {"articles": "nope"}, {"articles": {"a": 1}}])
def test_parse_articles_rejects_wrong_shapes(data):
    with pytest.raises(gdelt.GdeltParseError):
        gdelt.parse_articles(data)


def test_query_building_is_safe_and_configurable(monkeypatch):
    q = gdelt.build_query('Pu"ne (x)', ["flood", "heavy rain"], "english")
    assert q == '"Pune x" (flood OR "heavy rain") sourcelang:english'
    assert "sourcelang" not in gdelt.build_query("Pune", ["flood"], "")
    monkeypatch.setenv("GDELT_QUERY_TERMS", ' Flood , ,ab, heavy  rain,"quoted",flood,(x)')
    get_settings.cache_clear()
    assert gdelt.query_terms() == ["flood", "heavy rain", "quoted"]  # too-short terms dropped, de-duplicated
    monkeypatch.setenv("GDELT_QUERY_TERMS", ",,")
    get_settings.cache_clear()
    assert "flood" in gdelt.query_terms()  # nothing usable: defaults


def test_gdelt_numbers_are_sanitised(monkeypatch):
    monkeypatch.setenv("GDELT_TIMESPAN", "yesterday; DROP")
    monkeypatch.setenv("GDELT_MAX_RECORDS", "100000")
    monkeypatch.setenv("GDELT_TTL_HOURS", "-4")
    monkeypatch.setenv("GDELT_CORROBORATION_DOMAINS", "0")
    get_settings.cache_clear()
    assert (gdelt.timespan(), gdelt.max_records(), gdelt.ttl_hours(), gdelt.corroboration_domains()) == ("24h", 50, 24.0, 3)


# -- fetching ----------------------------------------------------------------------------------------------
async def _gd(answer, cities=None):
    transport, seen = mock({GDELT_HOST: answer})
    async with httpx.AsyncClient(transport=transport) as client:
        res = await gdelt.fetch_gdelt(cities or [PUNE], client=client, now=NOW)
    return res, seen


async def test_fetch_normalizes_good_articles_and_survives_bad_ones_mixed_in():
    res, seen = await _gd({"articles": [
        art("Pune flooded after heavy rain", url="https://a.in/1"), art("Pune flooded after heavy rain", url="https://a.in/1"),
        art("Pune wins cricket"), art("Old flood in Pune", seen="20260901T000000Z"), "junk", None, {"url": "x"},
        art("Pune landslide kills two", url="https://b.in/2", domain="b.in"),
    ]})
    assert res.ok and res.articles_seen == 8
    assert sorted(s.source_url for s in res.signals) == ["https://a.in/1", "https://b.in/2"]
    assert (res.skipped_other, res.skipped_irrelevant, res.skipped_stale, res.malformed) == (1, 1, 1, 3)
    assert all(s.weight <= 0.5 for s in res.signals)
    assert len(seen) == 1 and seen[0].url.host == GDELT_HOST  # one query, and no article page was ever requested
    p = dict(seen[0].url.params)
    assert p["mode"] == "artlist" and p["format"] == "json" and '"Pune"' in p["query"] and "flood" in p["query"]


@pytest.mark.parametrize("answer", [
    None, 500, 429, b"Please limit requests to one every 5 seconds.", b"<html>error</html>", b"The specified phrase is too short.",
    b"[]", b'{"articles": "x"}', b"\xff\xfe", httpx.ConnectTimeout("slow"), b"x" * 3_000_000,
])
async def test_gdelt_failures_are_counted_never_raised(answer):
    res, _ = await _gd(answer)
    assert not res.ok and res.cities_failed == 1 and res.signals == [] and res.errors and len(res.errors[0]) <= 300


@pytest.mark.parametrize("answer", [b"", b"{}", {"articles": []}, {"articles": None}])
async def test_empty_results_are_a_success_with_no_signals(answer):
    res, _ = await _gd(answer)
    assert res.ok and res.signals == [] and res.cities_failed == 0


async def test_each_city_is_queried_separately_and_one_failure_does_not_stop_the_rest():
    other = common.CityRef(slug="x", label="Xville", lat=1.0, lng=2.0, bbox=[0, 1, 2, 3])
    calls = []

    def answer(request):
        calls.append(request.url.params["query"])
        return httpx.Response(500) if "Pune" in calls[-1] else httpx.Response(200, json={"articles": [art("Xville flooded")]})
    res, _ = await _gd(answer, cities=[PUNE, other])
    assert len(calls) == 2 and res.cities_ok == 1 and res.cities_failed == 1
    assert [s.area for s in res.signals] == ["Xville"] and res.signals[0].bbox == [0, 1, 2, 3]


async def test_refresh_gdelt_writes_to_the_common_store_idempotently_and_keeps_data_on_failure():
    good = {GDELT_HOST: {"articles": [art("Pune flooded after heavy rain", seen=_seen_now(), url="https://a.in/1")]}}
    transport, _ = mock(good)
    async with httpx.AsyncClient(transport=transport) as c:
        s1 = await ingest.refresh_gdelt(client=c)
    assert s1["ok"] and s1["added"] == 1 and signal_store.counts() == {"gdelt": 1}
    transport, _ = mock(good)
    async with httpx.AsyncClient(transport=transport) as c:
        s2 = await ingest.refresh_gdelt(client=c)
    assert s2["added"] == 0 and s2["updated"] == 1 and len(signal_store.list()) == 1
    transport, _ = mock({GDELT_HOST: 429})
    async with httpx.AsyncClient(transport=transport) as c:
        s3 = await ingest.refresh_gdelt(client=c)
    assert not s3["ok"] and len(signal_store.list()) == 1
    assert ingest.last_refresh["gdelt"] is s3


def _seen_now():
    return (datetime.utcnow() - timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")


async def test_refresh_gdelt_disabled(monkeypatch):
    monkeypatch.setenv("GDELT_ENABLED", "false")
    get_settings.cache_clear()
    assert (await ingest.refresh_gdelt())["disabled"] is True


# =====================================================================================================
# API + shared guarantees
# =====================================================================================================
def test_signals_are_listed_scored_and_filtered_across_all_three_sources():
    now = datetime.utcnow()
    [rain] = open_meteo.analyze_forecast(_live_forecast(_heavy_now()), PUNE, now)
    news = gdelt.finalize([gdelt.normalize_article(art("Pune flooded, residents stranded", seen=_seen_now()), PUNE, now)])
    signal_store.upsert_many([rain, *news])
    client = TestClient(app)
    rows = client.get("/signals", params={"city": "pune"}).json()
    assert {r["source"] for r in rows} == {"open_meteo", "gdelt"}
    by = {r["source"]: r for r in rows}
    assert by["open_meteo"]["kind"] == "weather" and by["gdelt"]["kind"] == "news"
    assert 0 < by["gdelt"]["score"] <= 0.5 and 0 < by["open_meteo"]["score"] <= 0.7
    assert [r["id"] for r in client.get("/signals", params={"source": "gdelt"}).json()] == [news[0].id]
    assert client.get("/signals", params={"kind": "weather"}).json()[0]["id"] == "open_meteo:pune:rain"


def test_status_scoring_and_refresh_endpoints(monkeypatch):
    calls = []

    async def fake_om(client=None, city=None):
        calls.append(("om", city))
        return {"ok": True, "signals": 0}

    async def fake_gd(client=None, city=None):
        calls.append(("gd", city))
        return {"ok": False, "cities_failed": 1}
    monkeypatch.setattr(ingest, "refresh_open_meteo", fake_om)
    monkeypatch.setattr(ingest, "refresh_gdelt", fake_gd)
    client = TestClient(app)
    assert client.post("/signals/open-meteo/refresh").json() == {"ok": True, "signals": 0}
    assert client.post("/signals/gdelt/refresh", params={"city": "Pune"}).json() == {"ok": False, "cities_failed": 1}
    assert calls == [("om", None), ("gd", "Pune")]
    assert client.post("/signals/gdelt/refresh", params={"city": "atlantis"}).status_code == 404
    assert client.post("/signals/open-meteo/refresh", params={"city": "atlantis"}).status_code == 404

    st = client.get("/signals/status").json()
    assert st["open_meteo"]["enabled"] and st["gdelt"]["cities"] == ["Pune"] and st["sachet"]["enabled"]
    sc = client.get("/signals/scoring").json()
    assert sc["source_trust"] == {"sachet": 0.9, "gdelt": 0.5, "open_meteo": 0.7}
    assert sc["open_meteo"]["thresholds"]["rain_24h_mm"]["severe"] == 115.6 and sc["gdelt"]["corroboration_domains"] == 3
    assert "severity_weights" in sc and "freshness_half_life_hours" in sc  # earlier keys unchanged


def test_nothing_citizen_related_exists_on_the_new_signals():
    assert not {k for k in Signal.model_fields if any(w in k for w in ("reporter", "phone", "contact", "user", "device"))}
    [rain] = open_meteo.analyze_forecast(forecast_doc(precip=HEAVY), PUNE, NOW)
    news = gdelt.normalize_article(art("Pune flooded"), PUNE, NOW)
    for sig in (rain, news):
        blob = json.dumps(sig.model_dump(mode="json")).lower()
        assert "reporter" not in blob and "phone" not in blob


def test_new_signals_survive_a_restart(tmp_path, monkeypatch):
    import db
    from services.signals.store import SignalStore
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 's.db'}")
    get_settings.cache_clear()
    db.reset_engine()
    assert db.init_db()
    [rain] = open_meteo.analyze_forecast(forecast_doc(precip=HEAVY), PUNE, NOW)
    news = gdelt.finalize([gdelt.normalize_article(art("Pune flooded"), PUNE, NOW)])
    SignalStore().upsert_many([rain, *news])
    again = SignalStore()
    assert again.load_from_db() == 2
    assert again.get("open_meteo:pune:rain").metadata["endpoint"] == "forecast" and again.get(news[0].id).kind == SignalKind.NEWS
    db.reset_engine()


def test_build_query_can_leave_the_city_unquoted():
    assert gdelt.build_query("Pune", ["flood"], "english", quote_city=False).startswith("Pune (flood)")
    assert gdelt.build_query("Pune", ["flood"], "english").startswith('"Pune" (flood)')
