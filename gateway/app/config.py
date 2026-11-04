from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gateway_api_key: str
    cortex_service_username: str
    cortex_service_password: str
    cortex_hub_base_url: str = "http://cortex-hub:5000"
    cortex_network_name: str = "cortex-internal"

    job_ttl_seconds: int = 900
    http_timeout_seconds: float = 180.0

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
