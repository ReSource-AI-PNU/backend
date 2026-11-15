from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 변수로 주입되는 설정. 값은 .env 파일 또는 Container Apps 시크릿에서 읽는다."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    cors_origins: list[str] = ["http://localhost:3000"]

    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = ""
    azure_search_endpoint: str = ""
    azure_search_api_key: str = ""
    azure_maps_key: str = ""
    azure_storage_connection_string: str = ""
    database_url: str = ""
    platform_service_key: str = ""
    platform_demo: bool = False
    platform_stage_endpoints: dict[str, str] = {}
    platform_stage_token: str = ""
    platform_a2a_peers: dict[str, dict[str, str]] = {}
    platform_adapter_timeout: float = Field(default=15, ge=1, le=60)
    platform_road_matrix_url: str = ""
    platform_road_matrix_token: str = ""


settings = Settings()
