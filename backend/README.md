# Backend

FastAPI + an in-process SSE bus. Run with **one worker**:

    pip install -r requirements.txt
    cp .env.example .env
    python main.py            # 0.0.0.0:8742 (set PORT to change; the frontend expects 8742)
    pytest

## City data (Pune by default)

`data/layers_pune.json` is the single source for the map and the agents (hospitals + beds, relief depots,
flood zones, bridges, fire and police stations). `GET /layers?city=Pune` serves it, so the demo never
depends on live Overpass. The shipped file is a hand-built seed with approximate coordinates; refresh it from
OpenStreetMap once (needs internet):

    python scripts/fetch_city_data.py --city Pune

Nominatim gives the bounding box, Overpass the facilities; unnamed entries and duplicates are dropped and
bridges are capped to major roads. Hospital beds use OSM's `beds` tag when present, otherwise a number by
hospital size. Depots and flood zones are always generated (OSM has neither). Another city, e.g.
`/layers?city=Mumbai`, is fetched on first request and cached as `data/layers_mumbai.json` (disable with
`LIVE_OSM=false`). A cached file is always used first, so each city hits Nominatim/Overpass at most once
(`&refresh=true` forces a re-fetch; if that fails the cached copy is kept). With no cache and no data from
OSM, `/layers` returns a clear error: 404 (city not found, no hospitals, or `LIVE_OSM=false`) or 502 (OSM unreachable). `DEFAULT_CITY` picks the city used by weather, detection, routing and verification.

## Weather

Detection calls Open-Meteo for the default city's centre. In mock mode, or with `SIMULATE_WEATHER=true`, a
simulated heavy-rain alert is injected when real weather is clear, so the demo always has one.

## Active run

Starting a scenario makes its run the active one: `run.active` is broadcast on `/bus/stream` (and replayed on
connect), `GET /runs/active` returns it, and `world` / `plan.*` payloads carry `run_id`. `GET /reports` and
`GET /incidents` show the active run only (plus untagged real reports); add `?all=true` for everything.

`POST /incidents` creates a manual incident (`text, lat, lng, need_type, urgency`, optional `location, people, vulnerable, run_id`). It joins `run_id` (default: the active run; a new run starts if there is none), is visible at once, and the supervisor plans it into the existing plan through the same agents and failure handling as scenario incidents: new plan items arrive as `pending` over `plan.updated`, nothing already in the plan is reset, and the plan stops being final until the new items are approved.
Old runs are hidden, not deleted.

## Hospital capacity

The medical agent sends each patient to the nearest operational hospital with free beds. If that hospital is
full, the patient is diverted to the next nearest and `patient_diverted` / `hospital_full` events go to the
bus. If every hospital is full the nearest one is used and flagged `overflow`.

**Live occupancy.** The simulator owns the clock and the patient ledger (see the frontend README) and publishes
a per-hospital census with every `POST /sim/world` (`hospital_load`, `admitted_incident_ids`). The backend keeps
the latest one (`services/hospital_state.py`) and the medical agent plans against it: free beds are the beds free
*now* (a bed returns when its patient's stay ends), a patient assigned but not yet admitted reserves a bed, and a
patient already in a bed is not counted twice. Rejected hospital items release their reservation. The census applies
only to the run it is tagged with; without one (no simulator, another run) the agent uses the static `beds_available`
exactly as before. Each assignment carries `expected_stay_min` (by urgency, longer for vulnerable patients), the time
the simulator keeps the bed taken. `GET /hospitals/capacity` returns what the agent sees: capacity, occupied, free,
status and, when live, demand, diversions and overflow per hospital.

## Per-run city

`POST /scenario/start` takes an optional `city` (name or slug). Its layers are loaded first (cache, else fetched from OSM,
else a clear 404/502), the run stores `city`, and every agent in that run (hospitals, depots, flood zones, weather,
routing, verification) uses that city instead of `DEFAULT_CITY` (`services/city.py: city_context`). `run.active`,
`/bus/stream` replay and `GET /runs/active` carry the run's `city`. The shipped scenario, reports and news are Pune's, so
for any other city nothing is replayed: the run starts from the weather alert and incidents come from the simulator
(`POST /incidents`; `city` there only matters if it has to start the run). Without `city` everything behaves as before.

## External evidence: common signals (SACHET alerts, Open-Meteo weather, GDELT news)

External evidence (official alerts, weather, news) lives in one **common signals store**, separate
from citizen reports. Citizen reports, their table and the deployed citizen page are untouched, and a signal never
carries reporter ids, history or tracking.

- **Common format** (`services/signals/models.py: Signal`): `id` (`sachet:<cap identifier>`, `open_meteo:<city>:<type>`,
  `gdelt:<hash of link>`), `source` (`sachet | open_meteo | gdelt`), `kind` (`official_alert | weather | news`),
  `title/text/event`, `hazards` (flood, rain, cyclone, ...), `severity`, `urgency`, `certainty`, area (`area`,
  `polygons` as `[lat, lng]` rings, `lat/lng` centroid, `bbox`), `issued_at/effective_at/expires_at`, `status`
  (`active | cancelled | superseded`), and the scoring inputs `trust` and `weight`. All times are naive UTC.
- **Store** (`services/signals/store.py`): in memory with write-through to the `signals` table when `DATABASE_URL`
  is set (created on startup, restored on restart). Upserts by id are idempotent. A CAP *cancel* / *update*
  withdraws the alert it references (also if it arrives first) and a withdrawn alert is never resurrected. Expired
  signals are pruned after `SIGNALS_RETENTION_HOURS`.
- **SACHET** (`services/signals/sachet.py`): reads the NDMA CAP RSS feed(s) in `SACHET_FEED_URLS` (country or state
  level), downloads each CAP 1.2 alert and normalizes it. Only public, `Actual` alerts are kept (`SACHET_INGEST_NON_ACTUAL=true`
  also keeps Test / Exercise). One language is kept (`SACHET_PREFERRED_LANGUAGE`, default `en`). Alerts are
  district-level polygons; there is no point-level feed. Polled every `SACHET_POLL_SECONDS` (0 = off) and on
  `POST /signals/sachet/refresh`; CAP links already stored are not downloaded again.
- **Safe with bad data**: XML goes through `defusedxml` (no entity / DTD tricks); downloads are size-capped
  (`SACHET_MAX_BYTES`), time-limited, never follow redirects, and a CAP link must be on the feed's host or `*.gov.in`.
  A feed that is down, empty or not XML, and an item that is malformed, missing fields, has bad polygons, odd
  enum values or no `<expires>`, is counted in the refresh summary and skipped or defaulted; nothing raises, and
  what is already stored is kept. Text is whitespace-cleaned and length-bounded; it is still outside text, so treat
  it as untrusted if it is ever put in an LLM prompt.
- **Scoring constants are configurable** (`services/signals/scoring.py`, env in `.env.example`):
  `weight = trust x (w_sev*severity + w_cert*certainty + w_urg*urgency)` and
  `score = weight x freshness`, with `freshness = 0.5 ** (age_h / half_life)` floored at `SIGNAL_FRESHNESS_FLOOR`
  and 0 once expired / withdrawn. Trust, the three weight tables, the component mix, half-life and floor can all be
  overridden; an invalid value is logged and the default is used. `GET /signals/scoring` shows what is in force.
- **API**: `GET /signals` (`source`, `kind`, `active_only=true`, `city=<cached slug>`, `limit`; each row adds
  `active`, `freshness`, `score`), `GET /signals/status`, `GET /signals/scoring`, `POST /signals/sachet/refresh`.
  `city` matches signals whose area overlaps the city's box; signals without geometry are left out of that filter.

### Open-Meteo (weather, rain, river flood)

`services/signals/open_meteo.py`. Per configured city (`OPEN_METEO_CITIES`, cached cities only, default `DEFAULT_CITY`) it
asks two key-less APIs: the **forecast** API (hourly rain, wind gusts, temperature, weather code) and the **flood** API
(GloFAS daily river discharge: the last `OPEN_METEO_FLOOD_PAST_DAYS` days plus the forecast).

- **Signals** (`open_meteo:<city>:<type>`, kind `weather`): `rain` (worst rolling 24 h total and worst 1 h intensity),
  `flood` (forecast peak discharge / median of recent days), `wind` (gusts), `storm` (WMO thunderstorm codes 95/96/99),
  `heat`. **A signal exists only while a threshold is crossed**: calm weather leaves nothing in the store, so weather
  never sits at a fixed score. Default thresholds follow IMD rainfall classes (35.5 / 64.5 / 115.6 / 204.5 mm per 24 h).
- **Lifetime**: each signal lives `OPEN_METEO_TTL_HOURS` from its last refresh. A *successful* refresh that no longer
  shows a hazard expires it at once (status stays `active`, so the same id can come back); a failed or malformed
  response never expires or changes anything.
- **Score**: `trust` is 0.7 (model output, no issuing authority), so a weather signal can never exceed 0.7. Certainty is
  `likely` when the peak is within `OPEN_METEO_CERTAIN_WITHIN_HOURS`, else `possible`, never `observed`; the flood signal is
  `likely` only when the ensemble median agrees with the control run. Urgency comes from how far away the peak is.
- **Limits**: GloFAS is a ~5 km grid cell and may be a different river than the one you care about; every signal carries
  the city's box (where it was asked for), not a hazard footprint. Open-Meteo's free tier is for non-commercial use
  (attribution: CC BY 4.0).
- **Configurable**: trust (`SIGNAL_TRUST_OPEN_METEO`), the five threshold ladders, TTL, flood baseline / horizon / minimum
  discharge, certainty horizon. See `.env.example`; effective values at `GET /signals/scoring` under `open_meteo`.

### GDELT (news)

`services/signals/gdelt.py`. One DOC 2.0 API query per configured city (`GDELT_CITIES`): `"<city>"` AND any of
`GDELT_QUERY_TERMS`, last `GDELT_TIMESPAN`, newest first, spaced `GDELT_MIN_INTERVAL_SECONDS` apart (GDELT allows about one
request per 5 s).

- **Only the headline and link are used.** Article pages are never fetched (no SSRF surface, no copied body text).
  Hazard tags, severity, certainty and urgency are read from the headline with fixed keyword rules: deaths / "washed away"
  -> `extreme`, flooded / stranded / rescue / evacuation -> `severe`, otherwise `moderate`; forecast-style wording
  ("warns", "may", "red alert issued") caps severity at `moderate`, certainty `possible`. "Flooded with applications" style
  metaphors and headlines with no hazard word are dropped (counted as `skipped_irrelevant`).
- **Score**: `trust` is 0.5, so a fresh article can never score above 0.5. With the default tables an article that reports
  actual impact starts around **0.42-0.47**, a generic mention about 0.34, a forecast-style one about 0.27. Certainty rises
  one rung when `GDELT_CORROBORATION_DOMAINS` different outlets cover the same hazard for the same city in one refresh
  (`metadata.corroborating_domains`; syndicated copies are not detected). Freshness then halves every 12 h as for every source.
- **Location is coarse**: the searched city's centre and box, flagged `metadata.geo = "query_city"`.
- **Safe with bad data**: GDELT answers errors, rate limits and some empty results in plain text, `{}` or an empty body:
  a failed query is counted in the summary, an empty one is "no articles", nothing raises, stored articles are kept.
  Articles with no usable http(s) link or title, an unreadable or future `seendate`, or an already-expired lifetime are
  skipped or defaulted; titles are HTML-unescaped, control-character-free and length-bounded. Titles are outside text:
  treat them as untrusted if they are ever put in an LLM prompt.
- **Configurable**: trust (`SIGNAL_TRUST_GDELT`), query terms, timespan, language, max records, TTL, corroboration count.

### Shared

- **API** additions: `POST /signals/open-meteo/refresh` and `POST /signals/gdelt/refresh` (optional `?city=<cached city>`,
  404 otherwise; always 200 with a summary), `GET /signals/status` now also reports `open_meteo` and `gdelt`
  (`enabled`, `cities`, `poll_seconds`, `last_refresh`), and `GET /signals/scoring` adds `open_meteo` and `gdelt`
  settings next to the shared tables. Pollers: `OPEN_METEO_POLL_SECONDS`, `GDELT_POLL_SECONDS` (default 1800, 0 = off).
- Neither source touches citizen reports, their table or the deployed citizen page, and no signal carries reporter ids,
  history or tracking. The source-trust numbers are the *ceiling* of a signal's weight; the old fixed 0.85 (news) / 0.95
  (weather) in `agents/verification.py` apply only to the legacy incident path and are untouched.

Not wired yet: the verification agent does not read signals.

## Verification: external-evidence support and crowd corroboration

`agents/verification.py` scores each incident (base 0.65 for a citizen report) and may **raise** it with two rule-based
boosts, implemented in `services/corroboration.py`. Missing evidence or a small crowd costs nothing: there is no penalty
for "no match". The old near-duplicate penalty is gone. Credibility never exceeds **0.95**.

- **Evidence support.** A signal from the common store supports an incident when all three hold: *hazard-compatible*
  (incident text tagged with the same tagger as alerts; a rain / flood alert supports a flood report, a fire alert only
  a fire report, and a flood or rain alert **never** supports a fire incident; text with no hazard word matches nothing),
  *covering the place* (inside the alert polygon / circle / box; for point evidence such as a river gauge, within
  `evidence_local_radius_km`) and *overlapping in time* (incident time inside the signal's lifetime, with a configurable
  margin before and after). Cancelled / superseded and weak signals (`evidence_min_signal_weight`) are ignored.
  **Broad** evidence (district- or city-wide alert areas, city-level news and weather) gives the smaller boost;
  **local** evidence (a small alert area, a river gauge, or news whose headline names the incident's locality) the
  larger one. A second *different* source adds a small bonus, up to a cap. The reason reads
  `Supported by <source> "<title>" (<scope> evidence): <link>`, never "verified". No river-gauge source exists yet; the
  rule is ready for one (`metadata.scope == "local"` or `metadata.type == "river_gauge"` plus a point).
- **Crowd corroboration.** Other citizen reports nearby (`crowd_radius_km`, `crowd_window_hours`) about the same kind of
  event. Only independent reports count, judged by text diversity, spatial spread and time spread alone: copy-pasted
  text (similarity >= `crowd_copy_similarity`), near-identical text from the same spot or moment, and different text from
  the same spot at the same moment count once. No reporter ids, history or tracking are read. Boost by independent count:
  +0.10 at 2, +0.15 at 3, +0.20 at 5, +0.25 at 10, +0.35 at 25+ (counts above `crowd_cap` = 25 are treated as 25).
  Flagged-as-suspicious and implausible reports are never boosted, and only citizen reports corroborate.
- **`VerificationResult`** gained `crowd_size` (independent reports, *not* capped, for severity / priority later) and
  `supported_by` (signal ids). Both are optional additions; nothing else about the contract changed.
- **One configurable block**: `CorroborationConfig` in `services/corroboration.py` holds every new number (boosts, tiers,
  radii, windows, thresholds, ceiling). Override any of them with the single env var `CORROBORATION_CONFIG` (JSON object);
  invalid entries are logged and ignored, and `max_credibility` can be lowered but never raised above 0.95.
  `GET /signals/scoring` shows the block in force under `corroboration`.

## Database (PostgreSQL)

One database. Citizens write to its `reports` table through the deployed citizen app; the backend and dashboard read from it.
Set `DATABASE_URL` and nothing else.

- **Reads `reports`** (`id, text, location, lat, lng, need_type, urgency, source, "timestamp", metadata`; `metadata` =
  `{people_count, vulnerable[], here, phone?}`). Never writes to it, never copies the phone number.
  - `GET /reports` serves the table to the dashboard (and a live `report.new` event is sent when a row appears).
  - Detection reads the last `CITIZEN_REPORTS_WINDOW_HOURS` when a run starts; while a run is active, new reports are planned
    into it every `CITIZEN_POLL_SECONDS` (`services/citizen_sync.py`), same path as `POST /incidents`.
  - ids become `cr-<id>`; `food_water` is read as `supplies`, `moderate` as `medium`.
- **Adds its own tables** next to it: `runs` (whole `RunState` as JSONB, active-run flag), `incidents`, `report_submissions`.
  They are created on startup (`db.init_db`) and every store writes through on change, so a restart no longer loses runs.
  The database user therefore needs CREATE rights; `reports` is left alone.
- `DATABASE_URL` unset = in-memory only, as before. A database error is logged, never raised.
- `GET /health` shows `database` / `citizen_database` as `null` (not configured), `true` or `false` (unreachable).
- Not persisted: the hospital census (`services/hospital_state.py`) and the SSE bus; the simulator re-publishes them.
- `POST /reports` on this API is a demo/simulator intake that keeps reports in `report_submissions`; real citizens use the citizen app.
- Tests run on SQLite; for Postgres: `TEST_POSTGRES_URL=postgresql+psycopg://... pytest tests/test_database.py`.

## Simulation: evidence controls, reset, and the re-route fix

**Simulated evidence (`POST /sim/evidence`).** Body: `{run_id, incident_id, action}`, action = `news` | `official_alert` | `weather`
(back the incident) or `all_clear` | `normal_conditions` (contradict it) or `random`. No form: `services/sim_evidence.py` builds an
ordinary `Signal` from the incident (its hazard, a ~2 km area around it, the current time) and writes it to the **same
`signal_store`**; the incident is then scored again by the **normal Verification Agent**. Backing signals go through
`corroboration.find_support`, contradicting ones (`metadata.stance = "contradicts"`) through the new
`corroboration.find_contradiction`, which subtracts `contradiction_penalty_local/broad` (+ `contradiction_extra_source_penalty`
per extra source, capped by `contradiction_max_penalty`; all in `CorroborationConfig` / `CORROBORATION_CONFIG`). A contradicting
signal never counts as support. `random` draws backing-or-contradicting afresh on every call, so each incident gets its own draw.
Simulated signals are tagged `metadata.simulated` and addressed to one incident (`metadata.target_incident_id`), which lets them
match reports whose text names no hazard (they take the hazard of the nearest incident in the run). Real signals never carry
that key, so real matching is unchanged. `VerificationResult` gained `contradicted_by`.

**Reset (`POST /sim/reset`).** Removes only what the simulator tagged: runs started with `simulation: true` (`RunState.simulated`),
signals with `metadata.simulated`, incidents with source `sim` (also from real runs, with their plan items), the simulator's hospital
census and last world snapshot. Real incidents scored with simulated evidence are scored again without it. The newest real run
becomes active again and clients receive `sim.reset` + `run.active`. A removed run can no longer be written back by a pipeline
that was still running, and late `/sim/world` posts from it are ignored.

**Re-routing.** (1) A vehicle stopped at a blocked road/bridge is inside its radius; it may only *leave* it, so a route that drives
on towards the blockage is blocked and the vehicle turns round and detours (`Hazard.drives_into`). (2) OSRM requests send
`continue_straight=false`; the car profile otherwise forbids U-turns at waypoints, so detours that need one were never found.
