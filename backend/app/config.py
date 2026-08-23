from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_DEFAULT_DB = "sqlite:///" + (Path(__file__).resolve().parent.parent / "patra.db").as_posix()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "PATRA"
    database_url: str = _DEFAULT_DB
    jwt_secret: str = "change-this-before-production-patra"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:18000,http://127.0.0.1:18000"
    )
    geocoder_user_agent: str = "patra-local-development/0.1"
    geocoder_timeout_seconds: float = 6.0
    admin_emails: str = "joohan92@naver.com"
    seed_test_account: bool = True
    groq_api_key: str = ""
    groq_chat_model: str = "openai/gpt-oss-120b"
    groq_timeout_seconds: float = 35.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def admin_email_list(self) -> list[str]:
        return [item.strip().lower() for item in self.admin_emails.split(",") if item.strip()]


settings = Settings()
