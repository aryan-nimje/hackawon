"""Dynamic city loading: Overpass failover/retry, huge-area bbox cap, 'X District' names, bridges optional."""

import asyncio

import httpx
import pytest

from services import osm


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Client:
    """Fake httpx client: `script` maps a URL substring to a list of responses/exceptions served in order."""

    def __init__(self, script):
        self.script, self.calls = script, []

    async def post(self, url, **kw):
        self.calls.append(url)
        for key, seq in self.script.items():
            if key in url:
                item = seq.pop(0) if len(seq) > 1 else seq[0]
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(osm.asyncio, "sleep", no_sleep)


def test_clean_city_name():
    assert osm.clean_city_name("Mumbai City District") == "Mumbai City"
    assert osm.clean_city_name("Pune Division") == "Pune"
    assert osm.clean_city_name("Kansas City") == "Kansas City"
    assert osm.clean_city_name("District") == "District"  # never reduce to nothing
    assert osm.clean_city_name("Pune") == "Pune"


def test_bbox_is_capped_around_the_centre():
    # a district-sized box (about 1 degree wide) around Mumbai
    box = osm.clamp_bbox([18.5, 72.4, 19.6, 73.4], [19.07, 72.88])
    assert box == [18.82, 72.63, 19.32, 73.13]
    # a small city box is left alone
    assert osm.clamp_bbox([18.4, 73.7, 18.72, 74.0], [18.52, 73.86]) == [18.4, 73.7, 18.72, 74.0]


def test_overpass_fails_over_to_next_mirror_on_429_and_504():
    ok = _Resp(200, {"elements": [{"id": 1}]})
    client = _Client({"overpass-api.de": [_Resp(429)], "kumi": [_Resp(504)], "private.coffee": [ok]})
    out = asyncio.run(osm._overpass(client, "q"))
    assert out == [{"id": 1}]
    assert len(client.calls) == 3


def test_overpass_survives_timeout_and_html_200():
    ok = _Resp(200, {"elements": []})
    client = _Client({"overpass-api.de": [httpx.ReadTimeout("slow")], "kumi": [_Resp(200, None)], "private.coffee": [ok]})
    assert asyncio.run(osm._overpass(client, "q")) == []


def test_overpass_gives_a_specific_error_when_every_mirror_fails():
    client = _Client({"overpass-api.de": [_Resp(504)], "kumi": [_Resp(504)], "private.coffee": [_Resp(429)]})
    with pytest.raises(osm.OsmError) as e:
        asyncio.run(osm._overpass(client, "q"))
    assert "HTTP 429" in e.value.detail and "private.coffee" in e.value.detail
    assert len(client.calls) == osm.OVERPASS_ROUNDS * len(osm.OVERPASS_URLS)


def test_city_is_usable_when_only_the_bridge_query_fails(monkeypatch):
    city = {"name": "Testville (Simulated)", "slug": "testville", "center": [10.0, 20.0], "zoom": 12, "bbox": [9.9, 19.9, 10.1, 20.1]}
    hosp = {"type": "node", "id": 7, "lat": 10.0, "lon": 20.0, "tags": {"amenity": "hospital", "name": "City Hospital"}}
    queries = []

    async def fake_geocode(client, name):
        return city

    async def fake_overpass(client, query):
        queries.append(query)
        if "bridge" in query:
            raise osm.OsmError("overpass-api.de returned HTTP 504 (timed out)")
        return [hosp]

    monkeypatch.setattr(osm, "geocode_city", fake_geocode)
    monkeypatch.setattr(osm, "_overpass", fake_overpass)
    layers = asyncio.run(osm.fetch_city_layers("Testville"))
    assert [h["name"] for h in layers["hospitals"]] == ["City Hospital"]
    assert layers["bridges"] == [] and len(layers["flood_zones"]) == 3 and len(queries) == 2
