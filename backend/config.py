"""Central configuration loaded from environment variables."""

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings — the only module that reads env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: str = Field(default="anthropic", alias="LLM_PROVIDER")
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_model: str = Field(default="claude-sonnet-4-20250514", alias="LLM_MODEL")
    weather_api_key: str | None = Field(default=None, alias="WEATHER_API_KEY")
    routing_api_key: str | None = Field(default=None, alias="ROUTING_API_KEY")
    allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173", alias="ALLOWED_ORIGINS"
    )
    # Optional regex for other devices, e.g. r"http://192\.168\.\d+\.\d+:517\d"
    allowed_origin_regex: str | None = Field(default=None, alias="ALLOWED_ORIGIN_REGEX")
    mock_mode: bool = Field(default=False, alias="MOCK_MODE")
    # Default city served by /layers and used by weather, detection, route and verification.
    default_city: str = Field(default="Pune", alias="DEFAULT_CITY")
    # Allow GET /layers?city=<other> to fetch from Nominatim/Overpass when the city is not cached.
    live_osm: bool = Field(default=True, alias="LIVE_OSM")
    # Force the simulated weather alert even when real weather is clear (reliable demos).
    simulate_weather: bool = Field(default=False, alias="SIMULATE_WEATHER")

    # Coordinator database (runs, plans, incidents, submitted reports). Unset = in-memory only, as before.
    # e.g. postgresql://user:pass@host:5432/relief  (postgres:// and postgresql:// are both accepted)
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    # Only if the citizen app's `reports` table lives in a DIFFERENT database than DATABASE_URL. Normally empty:
    # citizens write to the same database the backend uses.
    citizen_database_url: str | None = Field(default=None, alias="CITIZEN_DATABASE_URL")
    citizen_reports_table: str = Field(default="reports", alias="CITIZEN_REPORTS_TABLE")
    citizen_reports_schema: str | None = Field(default=None, alias="CITIZEN_REPORTS_SCHEMA")
    # JSON overriding column auto-detection, e.g. {"text": "description", "lat": "latitude"}
    citizen_column_map: str | None = Field(default=None, alias="CITIZEN_COLUMN_MAP")
    citizen_reports_window_hours: int = Field(default=72, alias="CITIZEN_REPORTS_WINDOW_HOURS")
    citizen_reports_limit: int = Field(default=200, alias="CITIZEN_REPORTS_LIMIT")
    # How often (seconds) new citizen reports are pulled into the active run. 0 = only when a run starts.
    citizen_poll_seconds: int = Field(default=15, alias="CITIZEN_POLL_SECONDS")
    # Keep replaying the shipped Pune seed reports (data/reports.json) next to real ones. Set false in production.
    # Canned Pune demo data (data/reports.json + data/news.json) injected into every Pune run. OFF by default: it is not in
    # the database, so with it on the dashboard fills with reports and "[SIMULATED]" news nobody filed. (.env.example said
    # false all along; the code default said true.)
    seed_reports: bool = Field(default=False, alias="SEED_REPORTS")
    # Every N seconds the in-memory stores are brought back in line with the database (0 = only on reset / scenario start).
    db_resync_seconds: int = Field(default=15, alias="DB_RESYNC_SECONDS")

    # --- External evidence: common signals store + SACHET (official alerts) -------------------------------
    # Signals are external evidence (official alerts, later news/weather) kept apart from citizen reports.
    signals_retention_hours: int = Field(default=72, alias="SIGNALS_RETENTION_HOURS")  # expired signals older than this are pruned
    sachet_enabled: bool = Field(default=True, alias="SACHET_ENABLED")
    # Comma-separated CAP RSS feeds (country or state level). Each item links to a CAP 1.2 XML document.
    sachet_feed_urls: str = Field(
        default="https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml", alias="SACHET_FEED_URLS"
    )
    sachet_poll_seconds: int = Field(default=900, alias="SACHET_POLL_SECONDS")  # 0 = never poll (manual refresh only)
    sachet_timeout_seconds: float = Field(default=15.0, alias="SACHET_TIMEOUT_SECONDS")
    sachet_max_items: int = Field(default=40, alias="SACHET_MAX_ITEMS")  # newest N feed items per feed per refresh
    sachet_max_bytes: int = Field(default=1_000_000, alias="SACHET_MAX_BYTES")  # per downloaded document
    sachet_concurrency: int = Field(default=5, alias="SACHET_CONCURRENCY")
    sachet_default_ttl_hours: float = Field(default=24.0, alias="SACHET_DEFAULT_TTL_HOURS")  # alerts with no <expires>
    sachet_ingest_non_actual: bool = Field(default=False, alias="SACHET_INGEST_NON_ACTUAL")  # Test / Exercise / Draft
    sachet_preferred_language: str = Field(default="en", alias="SACHET_PREFERRED_LANGUAGE")

    # --- External evidence: Open-Meteo (rain / river flood / wind / thunderstorm / heat) ------------------
    # Model data, not an authority: low-ish trust, and a signal only exists while a threshold is crossed.
    open_meteo_enabled: bool = Field(default=True, alias="OPEN_METEO_ENABLED")
    open_meteo_poll_seconds: int = Field(default=1800, alias="OPEN_METEO_POLL_SECONDS")  # 0 = never poll
    open_meteo_timeout_seconds: float = Field(default=15.0, alias="OPEN_METEO_TIMEOUT_SECONDS")
    open_meteo_max_bytes: int = Field(default=2_000_000, alias="OPEN_METEO_MAX_BYTES")
    open_meteo_forecast_url: str = Field(default="https://api.open-meteo.com/v1/forecast", alias="OPEN_METEO_FORECAST_URL")
    open_meteo_flood_url: str = Field(default="https://flood-api.open-meteo.com/v1/flood", alias="OPEN_METEO_FLOOD_URL")
    # Comma-separated city names / slugs (cached cities only: the box and centre come from data/layers_<slug>.json).
    # Empty = DEFAULT_CITY.
    open_meteo_cities: str = Field(default="", alias="OPEN_METEO_CITIES")
    open_meteo_flood_enabled: bool = Field(default=True, alias="OPEN_METEO_FLOOD_ENABLED")
    open_meteo_ttl_hours: float = Field(default=3.0, alias="OPEN_METEO_TTL_HOURS")  # a signal lives this long after each refresh
    open_meteo_certain_within_hours: float = Field(default=24.0, alias="OPEN_METEO_CERTAIN_WITHIN_HOURS")  # beyond this lead: "possible"
    open_meteo_flood_past_days: int = Field(default=30, alias="OPEN_METEO_FLOOD_PAST_DAYS")  # baseline window for river discharge
    open_meteo_flood_horizon_days: int = Field(default=5, alias="OPEN_METEO_FLOOD_HORIZON_DAYS")
    open_meteo_flood_min_discharge: float = Field(default=5.0, alias="OPEN_METEO_FLOOD_MIN_DISCHARGE")  # m3/s, ignores trickles
    # Threshold ladders: JSON objects over minor / moderate / severe / extreme (partial overrides are fine).
    open_meteo_rain_24h_mm: Optional[str] = Field(default=None, alias="OPEN_METEO_RAIN_24H_MM")
    open_meteo_rain_1h_mm: Optional[str] = Field(default=None, alias="OPEN_METEO_RAIN_1H_MM")
    open_meteo_flood_ratios: Optional[str] = Field(default=None, alias="OPEN_METEO_FLOOD_RATIOS")  # peak / recent median discharge
    open_meteo_wind_gust_kmh: Optional[str] = Field(default=None, alias="OPEN_METEO_WIND_GUST_KMH")
    open_meteo_heat_c: Optional[str] = Field(default=None, alias="OPEN_METEO_HEAT_C")

    # --- External evidence: GDELT (news) -------------------------------------------------------------------
    # News is low-trust by design: it can only ever back up what other evidence says. Article pages are never fetched.
    gdelt_enabled: bool = Field(default=True, alias="GDELT_ENABLED")
    gdelt_api_url: str = Field(default="https://api.gdeltproject.org/api/v2/doc/doc", alias="GDELT_API_URL")
    gdelt_poll_seconds: int = Field(default=1800, alias="GDELT_POLL_SECONDS")  # 0 = never poll
    gdelt_timeout_seconds: float = Field(default=20.0, alias="GDELT_TIMEOUT_SECONDS")
    gdelt_max_bytes: int = Field(default=2_000_000, alias="GDELT_MAX_BYTES")
    gdelt_cities: str = Field(default="", alias="GDELT_CITIES")  # as OPEN_METEO_CITIES; empty = DEFAULT_CITY
    gdelt_query_terms: str = Field(
        default="flood,flooded,flooding,waterlogging,heavy rain,cloudburst,landslide,dam release,cyclone",
        alias="GDELT_QUERY_TERMS",
    )
    gdelt_timespan: str = Field(default="24h", alias="GDELT_TIMESPAN")  # e.g. 6h, 24h, 3d
    gdelt_max_records: int = Field(default=50, alias="GDELT_MAX_RECORDS")  # 1..250 per city per refresh
    gdelt_source_lang: str = Field(default="english", alias="GDELT_SOURCE_LANG")  # empty = any language
    gdelt_min_interval_seconds: float = Field(default=5.5, alias="GDELT_MIN_INTERVAL_SECONDS")  # GDELT allows 1 request / 5 s
    # 429 handling: retry with backoff (honouring Retry-After), then stop and stay quiet for the cooldown. The first poll
    # after startup waits too, so a server that restarts often (--reload) does not hit GDELT on every restart.
    gdelt_retries: int = Field(default=2, alias="GDELT_RETRIES")
    gdelt_cooldown_seconds: int = Field(default=900, alias="GDELT_COOLDOWN_SECONDS")
    gdelt_startup_delay_seconds: int = Field(default=20, alias="GDELT_STARTUP_DELAY_SECONDS")
    gdelt_ttl_hours: float = Field(default=24.0, alias="GDELT_TTL_HOURS")  # an article is evidence this long after it was seen
    gdelt_corroboration_domains: int = Field(default=3, alias="GDELT_CORROBORATION_DOMAINS")  # distinct outlets that raise certainty one rung

    # Scoring constants (all optional; defaults live in services/signals/scoring.py). JSON objects, e.g.
    # SIGNAL_SEVERITY_WEIGHTS={"extreme":1,"severe":0.8,"moderate":0.5,"minor":0.25,"unknown":0.3}
    signal_trust_sachet: Optional[float] = Field(default=None, alias="SIGNAL_TRUST_SACHET")
    signal_trust_gdelt: Optional[float] = Field(default=None, alias="SIGNAL_TRUST_GDELT")
    signal_trust_open_meteo: Optional[float] = Field(default=None, alias="SIGNAL_TRUST_OPEN_METEO")
    signal_severity_weights: Optional[str] = Field(default=None, alias="SIGNAL_SEVERITY_WEIGHTS")
    signal_certainty_weights: Optional[str] = Field(default=None, alias="SIGNAL_CERTAINTY_WEIGHTS")
    signal_urgency_weights: Optional[str] = Field(default=None, alias="SIGNAL_URGENCY_WEIGHTS")
    signal_component_weights: Optional[str] = Field(default=None, alias="SIGNAL_COMPONENT_WEIGHTS")
    signal_freshness_half_life_hours: Optional[float] = Field(default=None, alias="SIGNAL_FRESHNESS_HALF_LIFE_HOURS")
    signal_freshness_floor: Optional[float] = Field(default=None, alias="SIGNAL_FRESHNESS_FLOOR")

    # Verification: every corroboration number (evidence boosts, crowd boosts, radii, windows, ceiling) is one JSON
    # object, e.g. {"evidence_boost_local": 0.15}. Defaults live in services/corroboration.py (CorroborationConfig).
    corroboration_config: Optional[str] = Field(default=None, alias="CORROBORATION_CONFIG")

    @field_validator(
        "llm_api_key",
        "weather_api_key",
        "routing_api_key",
        "allowed_origin_regex",
        "database_url",
        "citizen_database_url",
        "citizen_reports_schema",
        "citizen_column_map",
        "signal_trust_sachet",
        "signal_trust_gdelt",
        "signal_trust_open_meteo",
        "open_meteo_rain_24h_mm",
        "open_meteo_rain_1h_mm",
        "open_meteo_flood_ratios",
        "open_meteo_wind_gust_kmh",
        "open_meteo_heat_c",
        "signal_severity_weights",
        "signal_certainty_weights",
        "signal_urgency_weights",
        "signal_component_weights",
        "signal_freshness_half_life_hours",
        "signal_freshness_floor",
        "corroboration_config",
        mode="before",
    )
    @classmethod
    def empty_str_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @property
    def sachet_feeds(self) -> List[str]:
        return [u.strip() for u in self.sachet_feed_urls.split(",") if u.strip().lower().startswith(("http://", "https://"))]

    @staticmethod
    def _names(raw: str) -> List[str]:
        return [n.strip() for n in (raw or "").split(",") if n.strip()]

    @property
    def open_meteo_city_names(self) -> List[str]:
        return self._names(self.open_meteo_cities) or [self.default_city]

    @property
    def gdelt_city_names(self) -> List[str]:
        return self._names(self.gdelt_cities) or [self.default_city]

    @property
    def origins_list(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def effective_mock_mode(self) -> bool:
        """Mock mode when forced or when no LLM key is available."""
        if self.mock_mode:
            return True
        if not self.llm_api_key or self.llm_api_key == "your_key_here":
            return True
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
