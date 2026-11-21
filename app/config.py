"""Application configuration loaded from environment / .env."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="GREMOTE_",
        extra="ignore",
    )

    secret_key: str = "change-me"
    admin_password: str = "change-me"
    session_ttl_seconds: int = 60 * 60 * 24 * 7
    database_path: str = "gremote.db"
    app_title: str = "gremote"


settings = Settings()
