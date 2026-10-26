from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения. Читаются из переменных окружения и `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    content_dir: str = "./content"
    bot_token: str = ""
    telegram_proxy: str | None = None
    owner_tg_id: int | None = None
    channel_id: int | None = None
    chat_id: int | None = None
    channel_invite_link: str | None = None
    tbank_terminal_key: str = ""
    tbank_password: str = ""
    tbank_notification_url: str = ""
    public_base_url: str = ""
    heartbeat_url: str | None = None
    heartbeat_token: str | None = None
    summary_bulk_threshold: int = 10
    payment_window_start_day: int = 22
    payment_window_end_day: int = 24
    tz: str = "Europe/Moscow"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
