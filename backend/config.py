"""Central configuration loaded from environment variables."""

from functools import lru_cache
from typing import List

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

    @field_validator(
        "llm_api_key", "weather_api_key", "routing_api_key", "allowed_origin_regex", mode="before"
    )
    @classmethod
    def empty_str_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

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
