from pydantic import Field, field_validator
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
    v3_llm: str = "rule"  # rule(규칙 정책, 모델 미연결) | foundry | anthropic
    foundry_endpoint: str = ""  # 리소스 주소까지만 (뒤에 /openai/... 를 붙이지 않는다)
    foundry_deployment: str = ""
    foundry_api_key: str = ""
    ollama_base_url: str = ""  # 업체가 오픈 모델을 고른 경우. VNet 안의 주소만 쓴다
    # V3_LLM=anthropic일 때. 키는 값으로 주거나(Key Vault 주입) 파일 경로로 준다. 기록에 싣지 않는다
    anthropic_api_key: str = ""
    anthropic_api_key_file: str = ""
    anthropic_model: str = ""  # 비우면 claude-sonnet-5-5
    # 참이면 ollama를 고른 업체도(주소가 없을 때) anthropic 모델이 답한다. 화면에는 둘 다 나온다
    v3_llm_all_vendors: bool = False
    # 도로 거리 제공자: kakao | azure | none. 비우면 AZURE_MAPS_KEY가 있을 때 Azure Maps를 쓴다.
    # 없거나 호출이 실패하면 직선 거리 × 우회 계수(가정값)로 계산하고 결과에 가정값이라고 남긴다
    v3_distance: str = ""
    kakao_rest_api_key: str = ""  # 카카오모빌리티 자동차 길찾기(읽기 전용 거리 조회)
    # 업체 측 에이전트 서비스 (app/v3/vendor_service.py)
    vendor_org_id: str = ""
    vendor_service_key: str = ""
    # 따로 띄운 업체 측 에이전트의 주소 {"업체 조직 ID": "https://..."}. "*"는 나머지 모든 업체.
    # 비어 있으면 API 프로세스 안에서 업체 측 에이전트를 부른다. 값을 주면 모든 업체 메시지가 HTTP로 간다.
    v3_vendor_agents: dict[str, str] = {}
    # 참이면 시연 되돌리기(POST /api/v3/demo/reset)를 누구나 부를 수 있다. 기본은 관리자 조직만.
    v3_demo_open: bool = False
    # 요청한 조직을 정하는 방법. header(기본, 시연 전용: X-Org-Id를 믿는다) | token(조직별 서비스 토큰).
    # header 모드는 공개 주소에 그대로 두면 조직 경계가 지켜지지 않는다.
    v3_auth: str = "header"
    # token 모드의 조직별 서비스 토큰. JSON 글 그대로 받는다: {"조직 ID": "토큰"}(값은 목록도 된다).
    # Key Vault에서 주입한다. 읽고 확인하는 일은 app/v3/httpio.py의 org_tokens가 한다.
    v3_org_tokens: str = ""
    # 탐지 모델(Azure ML 관리형 온라인 엔드포인트)의 채점 주소와 키. 비어 있으면 사진·영상 업로드는
    # "탐지 모델이 연결되지 않았습니다"로 답한다.
    v3_detector_endpoint: str = ""
    v3_detector_api_key: str = ""
    v3_detector_deployment: str = ""

    # ① 식별 모델 (backend#8). 가중치는 git 에 없다 -> scripts/fetch_models.py 가 MODEL_BLOB_URL 에서 받는다.
    classifier_backend: str = "auto"  # auto | trained | zeroshot
    material_model_dir: str = "models/material"
    condition_model_dir: str = "models/condition"
    domain_clf_dir: str = "models/domain_clf"
    contaminant_namer_dir: str = (
        ""  # OWLv2 디렉터리. 비우면 이물 이름 붙이기 끔 (라벨·테이프·금속 인서트·탄화물)
    )
    model_blob_url: str = (
        ""  # 모델 tar.gz 의 Azure Blob SAS URL (또는 Azure ML 레지스트리 다운로드 URL)
    )
    classify_fps: float = 4.0

    @field_validator("database_url")
    @classmethod
    def use_installed_driver(cls, value: str) -> str:
        """Key Vault의 database-url은 postgresql:// 꼴이다. 설치된 드라이버(psycopg 3)로 붙게 고친다."""
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value


settings = Settings()
