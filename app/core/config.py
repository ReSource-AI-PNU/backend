from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 변수로 주입되는 설정. 값은 .env 파일이나 배포한 앱의 설정(Key Vault 참조, 시크릿)에서 읽는다.

    v3 환경 변수의 이름과 허용값은 여기가 정본이다. 로컬 서버(app/v3/devserver.py)는 pydantic 없이
    os.environ에서 같은 이름을 읽는다. 값을 확인하는 일은 한곳에서 한다: V3_LLM은
    app/v3/agents/factory.py, V3_AUTH와 토큰은 app/v3/httpio.py, V3_DISTANCE는 app/v3/judge/distance.py.
    """

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
    # V3_LLM: rule(규칙 정책, 모델 미연결) | foundry | anthropic. 그 밖의 값이면 서버가 뜨지 않는다
    v3_llm: str = "rule"
    foundry_endpoint: str = ""  # 리소스 주소까지만 (뒤에 /openai/... 를 붙이지 않는다)
    foundry_deployment: str = ""
    foundry_api_key: str = ""
    # 추론 강도 minimal | low | medium | high | none(보내지 않음). 비우면 low. 값 확인은 agents/factory.py
    foundry_reasoning_effort: str = ""
    foundry_max_completion_tokens: str = ""  # 응답 토큰 상한(추론 토큰 포함). 비우면 4000
    ollama_base_url: str = ""  # 업체가 오픈 모델을 고른 경우. VNet 안의 주소만 쓴다
    ollama_num_ctx: str = ""  # Ollama 문맥 길이. 비우면 16384
    # V3_LLM=anthropic일 때. 키는 ANTHROPIC_API_KEY 값으로 주거나(Key Vault 주입) ANTHROPIC_API_KEY_FILE에
    # 키만 담은 파일의 경로로 준다(값이 비어 있을 때만 파일을 읽는다). 기록과 예외 문구에 싣지 않는다
    anthropic_api_key: str = ""
    anthropic_api_key_file: str = ""
    anthropic_model: str = ""  # ANTHROPIC_MODEL. 비우면 claude-sonnet-5-5
    # 참이면 ollama를 고른 업체도(주소가 없을 때) anthropic 모델이 답한다. 화면에는 둘 다 나온다
    v3_llm_all_vendors: bool = False
    # 도로 거리 제공자: kakao | azure | none. 비우면 AZURE_MAPS_KEY가 있을 때 Azure Maps를 쓴다.
    # 없거나 호출이 실패하면 직선 거리 × 우회 계수(가정값)로 계산하고 결과에 가정값이라고 남긴다
    v3_distance: str = ""
    # KAKAO_REST_API_KEY: 카카오모빌리티 자동차 길찾기(읽기 전용 거리 조회). V3_DISTANCE=kakao일 때만 읽는다
    kakao_rest_api_key: str = ""
    # 업체 측 에이전트 서비스 (app/v3/vendor_service.py)
    vendor_org_id: str = ""
    vendor_service_key: str = ""
    # 따로 띄운 업체 측 에이전트의 주소 {"업체 조직 ID": "https://..."}. "*"는 나머지 모든 업체.
    # 비어 있으면 API 프로세스 안에서 업체 측 에이전트를 부른다. 값을 주면 모든 업체 메시지가 HTTP로 간다.
    v3_vendor_agents: dict[str, str] = {}
    # 참이면 시연 되돌리기(POST /api/v3/demo/reset)를 누구나 부를 수 있다. 기본은 관리자 조직만.
    v3_demo_open: bool = False
    # 요청한 조직을 정하는 방법. header(기본, 시연 전용: X-Org-Id를 믿는다) | token(조직별 서비스 토큰)
    # | bff(프런트 서버만 부른다: V3_BFF_KEY + X-User-Id + X-Org-Id, 계정과 조직 소속을 확인한다).
    # header 모드는 공개 주소에 그대로 두면 조직 경계가 지켜지지 않는다.
    v3_auth: str = "header"
    # bff 모드에서 프런트 서버(BFF)가 Authorization: Bearer로 보내는 키. Key Vault(v3-bff-key)에서 주입한다.
    # bff 모드인데 비어 있으면 모든 요청을 503으로 거절한다
    v3_bff_key: str = ""
    # token 모드의 조직별 서비스 토큰. JSON 글 그대로 받는다: {"조직 ID": "토큰"}(값은 목록도 된다).
    # Key Vault에서 주입한다. 읽고 확인하는 일은 app/v3/httpio.py의 org_tokens가 한다.
    v3_org_tokens: str = ""
    # V3_ORG_TOKENS_FILE: 위 JSON을 담은 파일의 경로(비밀을 파일로 마운트할 때). 있으면 이 파일을 먼저 읽는다
    v3_org_tokens_file: str = ""
    # V3_CORS_ORIGINS: CORS_ORIGINS에 더 허용할 출처. 쉼표로 나눈다(http(s)://호스트[:포트]). 틀린 꼴이면 멈춘다
    v3_cors_origins: str = ""
    # 탐지 모델(Azure ML 관리형 온라인 엔드포인트)의 채점 주소와 키. 비어 있으면 사진·영상 업로드는
    # "탐지 모델이 연결되지 않았습니다"로 답한다.
    v3_detector_endpoint: str = ""
    v3_detector_api_key: str = ""
    v3_detector_deployment: str = ""
    # 보관장 3D 스캔의 재구성 서버(H100, infra/h100/recon). H100 사설 주소의 8090. 비면 스캔 올리기를 받지 않는다
    v3_recon_url: str = ""
    # 문서 추출(Document Intelligence prebuilt-layout). 둘 중 하나라도 비면 UTF-8 글 파일만 읽는다
    azure_document_intelligence_endpoint: str = ""
    azure_document_intelligence_api_key: str = ""
    # 업로드 문서의 지시문 주입 검사(Content Safety Prompt Shields). 비면 검사하지 않고
    # shieldUnavailable=true로 표시한다
    azure_content_safety_endpoint: str = ""
    azure_content_safety_api_key: str = ""
    # 근거 검색 색인(주소와 키는 위의 AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_API_KEY).
    # 벡터 차원은 임베딩 모델과 같아야 한다(bge-m3 1024, text-embedding-3-small 1536). 정수가 아니면
    # 경고를 남기고 로컬 검색으로 돈다
    azure_search_index: str = "rsai-evidence"
    azure_search_vector_dimensions: str = "1024"
    # 임베딩: ollama | foundry | 비움(벡터 없이 글과 시맨틱 랭커). 색인과 질의가 같은 값을 써야 한다.
    # ollama는 OLLAMA_BASE_URL, foundry는 FOUNDRY_ENDPOINT·FOUNDRY_API_KEY와 아래 배포 이름을 쓴다
    v3_embeddings: str = ""
    ollama_embed_model: str = "bge-m3"
    foundry_embedding_deployment: str = ""
    # 실행 기록을 OpenTelemetry span으로 Application Insights에 내보낸다(app/v3/tracing.py).
    # azure-monitor-opentelemetry(extra tracing)가 없으면 경고만 남긴다
    applicationinsights_connection_string: str = ""

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
