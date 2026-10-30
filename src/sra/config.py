import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="SRA_",
        extra="ignore",
    )

    dsn: str
    readonly_dsn: str
    ollama_url: str = "http://localhost:11434"
    generation_model: str = "gemma4:latest"

    # Ollama defaults num_ctx to 4096, which a long system prompt plus a few
    # tool results exceeds: the model then runs out of context mid-answer and
    # returns empty content with done_reason "length". 16k leaves headroom
    # without oversizing the KV cache -- three concurrent 32k slots exhausted
    # a 16GB machine and took the server down.
    context_tokens: int = 16384
    # Chat answers are intentionally concise; detailed evidence stays in the
    # retrieved rows and can be requested in a follow-up.
    max_output_tokens: int = 512

    cache_dir: Path = Field(default=PROJECT_ROOT / ".cache")
    sql_row_limit: int = 200
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Upstox (NSE/BSE live quotes). All optional with safe defaults so an
    # existing .env with no Upstox creds leaves every other command unchanged.
    #
    # Upstox's Analytics Token is a read-only, 1-year-validity token scoped to
    # Market Data -- exactly this tool's use case -- so it is preferred over
    # the standard OAuth2 access token, which must be refreshed daily through
    # an interactive browser consent step (see `sra upstox login`).
    upstox_analytics_token: str = ""
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_redirect_uri: str = "https://127.0.0.1/callback"
    upstox_max_requests_per_second: float = 5.0

    # Optional free key from bharatstockapi.com (60 requests a day): adds
    # shareholding patterns and mutual-fund portfolios. Empty disables both.
    bharatstock_api_key: str = ""


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def configure_logging() -> None:
    """Set up root logging once, for the CLI and the dashboard alike."""
    logging.basicConfig(
        level=settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO; a bulk ingest makes thousands.
    logging.getLogger("httpx").setLevel(logging.WARNING)
