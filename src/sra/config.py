from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="SRA_",
        extra="ignore",
    )

    # SEC rejects requests whose User-Agent carries no contact email.
    sec_user_agent: str
    dsn: str
    readonly_dsn: str
    ollama_url: str = "http://localhost:11434"
    generation_model: str = "gemma4:latest"
    embedding_model: str = "nomic-embed-text"

    # Ollama defaults num_ctx to 4096, which a handful of retrieved passages
    # exceeds on its own: the model then runs out of context mid-answer and
    # returns empty content with done_reason "length". Measured worst case is
    # about 9k tokens (a long system prompt plus three k=6 searches), so this
    # leaves useful headroom without oversizing the KV cache -- three
    # concurrent 32k slots exhausted a 16GB machine and took the server down.
    context_tokens: int = 16384
    max_output_tokens: int = 2048

    # SEC asks for no more than 10 req/s aggregate; stay under it.
    sec_max_requests_per_second: float = 8.0
    cache_dir: Path = Field(default=PROJECT_ROOT / ".cache")
    sql_row_limit: int = 200

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


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
