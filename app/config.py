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
    # Extra/override subnets for the unicast TV sweep, e.g. "192.168.1.0/24,192.168.2.0/24".
    # Empty = the /24 around this machine.
    scan_subnet: str = ""


settings = Settings()
