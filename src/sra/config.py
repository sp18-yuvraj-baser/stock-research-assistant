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
    # returns empty content with done_reason "length".
    context_tokens: int = 32768
    max_output_tokens: int = 2048

    # SEC asks for no more than 10 req/s aggregate; stay under it.
    sec_max_requests_per_second: float = 8.0
    cache_dir: Path = Field(default=PROJECT_ROOT / ".cache")
    sql_row_limit: int = 200


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
