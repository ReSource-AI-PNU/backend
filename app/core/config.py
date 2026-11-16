from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 변수로 주입되는 설정. 값은 .env 파일 또는 Container Apps 시크릿에서 읽는다."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    cors_origins: list[str] = ["http://localhost:3000"]

    openai_endpoint: str = ""
    chat_deployment: str = ""
    foundry_key: str = ""

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

    # 설계안 v3 (app/v3). DATABASE_URL이 있으면 SQL 저장, 없으면 메모리 저장으로 돈다.
    v3_llm: str = "rule"  # rule(규칙 정책, 모델 미연결) | foundry
    foundry_endpoint: str = ""  # 리소스 주소까지만 (뒤에 /openai/... 를 붙이지 않는다)
    foundry_deployment: str = ""
    foundry_api_key: str = ""
    ollama_base_url: str = ""  # 업체가 오픈 모델을 고른 경우. VNet 안의 주소만 쓴다
    # 업체 측 에이전트 서비스 (app/v3/vendor_service.py)
    vendor_org_id: str = ""
    vendor_service_key: str = ""

    # ① 식별 모델 (backend#8). 가중치는 git 에 없다 -> scripts/fetch_models.py 가 MODEL_BLOB_URL 에서 받는다.
    classifier_backend: str = "auto"  # auto | trained | zeroshot
    material_model_dir: str = "models/material"
    condition_model_dir: str = "models/condition"
    domain_clf_dir: str = "models/domain_clf"
    contaminant_namer_dir: str = ""  # OWLv2 디렉터리. 비우면 이물 이름 붙이기 끔 (라벨·테이프·금속 인서트·탄화물)
    model_blob_url: str = ""  # 모델 tar.gz 의 Azure Blob SAS URL (또는 Azure ML 레지스트리 다운로드 URL)
    classify_fps: float = 4.0


settings = Settings()
