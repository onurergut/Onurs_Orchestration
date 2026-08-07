from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration"
    redis_url: str = "redis://localhost:6379/0"

    task_source_type: str = "csv"
    review_surface_type: str = "web_ui"
    api_key_header: str = "X-API-Key"

    default_review_timeout_seconds: int = 86400
    default_monthly_budget_usd: float = 500.0

    worktree_base_path: str = "/var/worktrees"
    worktree_ttl_days: int = 30

    log_level: str = "INFO"


settings = Settings()