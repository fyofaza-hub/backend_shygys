from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    env: Literal["development", "test", "production"] = "development"

    database_url: str = "postgresql://shygys:shygys@localhost:5432/shygys"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: SecretStr = SecretStr("")
    moderation_queue_alert_threshold: int = 100
    token_minutes: int = 30
    cors_origins: list[str] = ["http://localhost:3000"]
    frontend_url: str = "http://localhost:3000"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = "no-reply@example.invalid"
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: SecretStr = SecretStr("")
    s3_region: str = "us-east-1"
    s3_bucket: str = "creative-shygys"
    enable_clamav: bool = False
    clamav_host: str = ""
    clamav_port: int = 3310
    clamav_timeout: float = 8
    require_media_scan: bool = False
    media_max_bytes: int = 12 * 1024 * 1024
    ai_provider: str = "alemai"
    alemai_base_url: str = "https://llm.alem.ai/v1"
    alemai_api_key: SecretStr = SecretStr("")
    alemai_translation_model: str = Field(
        default="qwen3-8", validation_alias=AliasChoices("AI_TRANSLATION_MODEL", "ALEMAI_TRANSLATION_MODEL")
    )
    alemai_fallback_model: str = ""
    alemai_kk_model: str = ""
    alemai_embedding_model: str = ""
    alemai_reranker_model: str = ""
    alemai_reranker_url: str = ""
    ai_glossary: dict[str, str] = {
        "Восточно-Казахстанская область": "kk=Шығыс Қазақстан облысы / en=East Kazakhstan Region",
        "Креативный Шығыс": "kk=Креативті Шығыс / en=Creative Shygys",
    }
    browserless_url: str = ""
    browserless_token: SecretStr = SecretStr("")
    tile_style_url: str = ""

    @property
    def alemai_api_root(self) -> str:
        return self.alemai_base_url.strip().rstrip("/").removesuffix("/chat/completions")

    @property
    def alemai_enabled(self) -> bool:
        provider = self.ai_provider.strip().lower().replace("_", "").replace("-", "")
        return provider in {"alemai", "alemaiplus"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
