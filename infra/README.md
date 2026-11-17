# infra

Re:Source AI의 Azure 리소스를 리소스 그룹 범위 Bicep으로 정의한다. 대상은 `team_AIBIC2026_03_RG_EUS2`이고, 만든 리소스에는 모두 `project=resource-ai`, `env=demo`, `managed-by=agent` 태그가 붙는다.

## 쓰는 법

```bash
az login                                   # 쓰기 작업에 MFA가 강제된다
bash infra/deploy-infra.sh what-if base         # 무엇이 바뀌는지 먼저 본다
bash infra/deploy-infra.sh apply base           # 만든다
bash infra/stop.sh                              # 단계가 끝나면 비용이 드는 것을 멈춘다
bash infra/start.sh                             # 다시 켠다
bash infra/teardown.sh                          # 태그가 붙은 리소스만 지운다 (확인을 받는다)
```

## 앱 배포: 앱 VM (지금 쓰는 경로)

App Service는 구독의 할당량이 0이라 만들 수 없고(B1·B2·F1·S1·P0v3 모두 `SubscriptionIsOverQuotaForSku`, East US 2·East US·Central US, 2026-11-18 사전 검증), Container Apps는 공급자가 등록되지 않았다. 그래서 CPU VM 한 대에 Docker Compose로 앱을 띄운다. 아래 `deploy.sh`·`appservice.bicep` 경로는 할당량이 풀리기 전까지 쓰지 않는다.

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/rsai_demo_vm          # 처음 한 번
az deployment group what-if -g team_AIBIC2026_03_RG_EUS2 -f infra/vm.bicep \
  -p sshPublicKey="$(cat ~/.ssh/rsai_demo_vm.pub)" sshSourcePrefix=<내 공용 IP>/32
az deployment group create  -g team_AIBIC2026_03_RG_EUS2 -n rsai-app-vm -f infra/vm.bicep \
  -p sshPublicKey="$(cat ~/.ssh/rsai_demo_vm.pub)" sshSourcePrefix=<내 공용 IP>/32
bash infra/vm/deploy-vm.sh                 # 커밋된 소스를 VM에 올려 빌드하고 띄운다
SEED=1 bash infra/vm/deploy-vm.sh          # 끝에 시연 시작 상태로 되돌린다
INDEX=1 bash infra/vm/deploy-vm.sh         # 끝에 백엔드 컨테이너 안에서 근거 색인을 다시 만든다(운영 DB의 근거)
```

| 구성 | 내용 |
|---|---|
| `infra/vm.bicep` | Central US의 VM `vm-rsai-demo-app`(Standard_D4as_v5, 4 vCPU·16GB, Ubuntu 22.04, 디스크 128GB), 공용 IP와 DNS 이름, 별도 VNet `vnet-rsai-demo-app`, NSG(80·443은 인터넷, 22는 `sshSourcePrefix`만). cloud-init이 Docker를 설치한다 |
| `infra/vm/compose.yml` | Caddy(HTTPS, Let's Encrypt), frontend, backend(API), detector(탐지 채점 서버 `app/ml/score_api.py`, CPU 추론. Azure ML 엔드포인트 대신 `V3_DETECTOR_ENDPOINT`가 부른다), ollama(CPU: `qwen2.5:7b`, `bge-m3`). 인터넷에는 Caddy만 열리고 나머지는 Compose 네트워크 안에서만 닿는다 |
| `infra/vm/deploy-vm.sh` | Key Vault에서 비밀을 읽어 VM의 `/opt/rsai/env/*.env`(권한 600)에만 쓰고, `git archive`한 소스를 올려 VM 안에서 이미지를 빌드한다. 마이그레이션 → 시작 → Ollama 모델 받기 → HTTPS 응답 확인 순서다 |

- **지역:** PostgreSQL·AI Search와 같은 Central US에 둔다. East US 2에 두었을 때 DB 왕복이 평균 39ms라 근거를 하나씩 읽는 진단 실행 한 번이 10분을 넘겼다.
- **설정:** `V3_LLM=foundry`(기본. Foundry gpt-5-mini. 2026-11-18 출력 형식을 고친 뒤 초안 첫 시도 통과율 99~100%, 시연 업체 세 곳 모두 합의. `rule`은 규칙 정책), `V3_AUTH=bff`(기본. 실제 계정·조직: 프런트 서버만 Key Vault `v3-bff-key`로 백엔드를 부르고 백엔드가 사용자·소속을 확인한다. 키가 없으면 스크립트가 만들어 `backend.env`와 `frontend.env`의 `V3_BFF_KEY`에 넣는다. `V3_AUTH=token`이면 전처럼 `v3-org-tokens`), `AUTH_DEV_LOGIN=true`(시연용 로그인). bff에서 `SEED=1`은 시연 조직만 되돌리고(`scripts.v3.accounts_admin reset-demo`) 시연 진단을 승인 대기까지 돌려 둔다. 관리자는 가입한 뒤 `GRANT_ADMIN=<이메일>`로 배포하거나 백엔드 컨테이너에서 `python -m scripts.v3.accounts_admin grant-admin <이메일>`로 넣는다. Ollama는 기본으로 쓰지 않는다: GPU 할당량이 0이라 CPU로 도는데, 6천 토큰 프롬프트에 qwen2.5:7b가 8분 30초, 3b가 4분 10초 걸려(프롬프트 처리 초당 12.1·24.7토큰, 2026-11-18 측정) 실시간 대화가 되지 않는다. 그래서 Ollama 주소를 비우고 `V3_LLM_ALL_VENDORS=1`로 Ollama를 고른 업체(시연의 남해열분해)도 Foundry가 답한다. 운영 화면에는 설정한 모델과 실제로 답한 모델이 따로 보인다. GPU가 생기면 `USE_OLLAMA=1`로 배포한다.
- **Azure AI 연결:** `deploy-vm.sh`가 `backend.env`에 Document Intelligence(`AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`, 키 `docintel-key`), Content Safety Prompt Shields(`AZURE_CONTENT_SAFETY_ENDPOINT`, 키 `contentsafety-key`), AI Search(`AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_INDEX=rsai-evidence`, 키 `search-admin-key`), 임베딩(`V3_EMBEDDINGS=foundry`, `FOUNDRY_EMBEDDING_DEPLOYMENT=text-embedding-3-small`, `AZURE_SEARCH_VECTOR_DIMENSIONS=1536`), `APPLICATIONINSIGHTS_CONNECTION_STRING`을 넣는다. 주소는 태그와 종류로 찾고 키는 Key Vault에서 읽는다. 리소스를 찾지 못하면 그 연결만 빠지고 앱은 로컬 경로로 돈다.
- **임베딩:** 설계는 Ollama `bge-m3`였지만 Ollama는 VM 안에서만 닿아 맥에서 색인할 수 없다. 색인과 질의가 같은 모델을 쓰도록 둘 다 Foundry `text-embedding-3-small`(배포 이름 같음, GlobalStandard, 용량 50, 1536차원)로 한다. 색인 `rsai-evidence`는 2026-11-18 맥에서 시드 근거 48건(법령 26, 기준값 11, 가정값 11)으로 만들었다. 다시 만들 때는 `INDEX=1`(VM 안, 운영 DB)이나 맥에서 아래처럼 한다(키는 Key Vault에서 환경 변수로만 읽는다).

  ```bash
  AZURE_SEARCH_ENDPOINT=https://srch-rsai-demo-o7xpbt.search.windows.net AZURE_SEARCH_VECTOR_DIMENSIONS=1536 \
  V3_EMBEDDINGS=foundry FOUNDRY_EMBEDDING_DEPLOYMENT=text-embedding-3-small FOUNDRY_ENDPOINT=<Foundry 주소> \
  AZURE_SEARCH_API_KEY="$(az keyvault secret show --vault-name kv-rsai-demo-o7xpbt -n search-admin-key --query value -o tsv)" \
  FOUNDRY_API_KEY="$(az keyvault secret show --vault-name kv-rsai-demo-o7xpbt -n foundry-api-key --query value -o tsv)" \
  uv run python -m scripts.v3.index_evidence --create-index
  ```
- **추적:** 이미지에는 `tracing` extra(`azure-monitor-opentelemetry`)가 `EXTRAS`와 상관없이 들어간다. 실행 기록의 span은 Application Insights `appi-rsai-demo`의 `dependencies` 표에 `rsai.*` 이름으로 쌓인다.
- **모델 가중치:** Storage 비공개 컨테이너 `models`의 `models-v3mat-v4cond.tar.gz`(재질 v3, 상태 v4, 도메인 분류기). 읽기 전용 SAS 주소(2026-12-31까지)를 Key Vault `model-blob-url`에 둔다.
- **SSH:** NSG는 22를 한 주소에서만 연다. 배포하는 컴퓨터의 공용 IP가 바뀌면 `deploy-vm.sh`가 시작할 때 규칙을 지금 IP로 바꾼다(`SSH_SOURCE`로 직접 줄 수도 있다).
- **끄고 켜기:** `stop.sh`가 VM을 할당 해제하고, `start.sh`가 다시 켠다(태그 `project=resource-ai`). 할당 해제 중에는 디스크 비용만 든다. Compose 서비스는 `restart: unless-stopped`라 VM이 켜지면 같이 올라온다.
- **비용(정가):** VM 시간당 0.172달러(월 약 126), 공용 IP 시간당 약 0.005달러, 디스크 128GB StandardSSD.

## Bicep 묶음

묶음은 네 개다. `all`을 주면 한 번에 배포한다.

| 묶음 | 내용 | 상태 (2026-11-16) |
|---|---|---|
| `base` | VNet, Log Analytics, Application Insights, Container Registry, Key Vault, Storage, PostgreSQL | 배포됨. Container Apps 환경은 공급자 등록 대기 |
| `ai` | Foundry(AI Services) + 프로젝트 + 채팅 모델, Document Intelligence, Content Safety, AI Search | 배포됨. Azure Maps는 공급자 등록 대기 |
| `ollama` | Ollama VM (공용 IP 없음, 11434는 VNet 안에서만) | 미배포. GPU 할당량 대기 |
| `ml` | 기존 Azure ML 작업 영역 `team-workspace`에 CPU 클러스터 | 배포됨. GPU 클러스터와 Fabric 용량은 대기 |

## 아직 켤 수 없는 것

구독 관리자가 아래를 풀어 줘야 한다. 풀리면 환경 변수를 주고 다시 배포한다.

| 막힌 것 | 원인 | 풀린 뒤 |
|---|---|---|
| Container Apps 환경 | `Microsoft.App` 공급자 미등록 (조 계정에는 등록 권한이 없다) | `ENABLE_CONTAINER_APPS=true bash infra/deploy-infra.sh apply base` |
| Azure Maps | `Microsoft.Maps` 공급자 미등록 | `ENABLE_MAPS=true bash infra/deploy-infra.sh apply ai` |
| Fabric 용량 (F2) | `Microsoft.Fabric` 공급자 미등록 | `ENABLE_FABRIC=true FABRIC_ADMINS='["<UPN>"]' bash infra/deploy-infra.sh apply ml` |
| Ollama GPU VM | T4 계열 할당량 0, A10 계열은 East US에 2 vCPU만 남음 | `bash infra/deploy-infra.sh apply ollama` |
| Azure ML GPU 클러스터 | H100 계열 할당량만 있다 (시간당 약 6.98달러) | `ENABLE_GPU_CLUSTER=true bash infra/deploy-infra.sh apply ml` |

`containerapps-env`, `maps`, `fabric`, `ollama-vm` 모듈은 `bicep build`만 통과했고 실제 배포로 검증하지 못했다.

## 설계 결정

- **인증:** 조 계정에 역할 할당 권한이 없어 관리 ID와 RBAC를 쓸 수 없다. 서비스 키를 쓰고, 키는 배포할 때 Key Vault 비밀로만 들어간다. Key Vault는 접근 정책 방식이다.
- **Key Vault 비밀 이름:** `database-url`, `postgres-admin-password`, `storage-connection-string`, `acr-username`, `acr-password`, `appinsights-connection-string`, `foundry-api-key`, `docintel-key`, `contentsafety-key`, `search-admin-key`, `maps-key`(대기)
- **사람이 넣는 Key Vault 비밀(앱 배포에서 고른 기능에만 필요):** `anthropic-api-key`(`V3_LLM=anthropic`), `v3-org-tokens`(`V3_AUTH=token`, API와 프런트가 같이 읽는다), `kakao-rest-api-key`(`V3_DISTANCE=kakao`), `detector-api-key`(`V3_DETECTOR_ENDPOINT`), `auth-entra-secret`(`AUTH_ENTRA_ID`). `auth-secret`, `vendor-service-key`, `mcp-service-key`는 없으면 `deploy.sh`가 만들어 Key Vault에만 둔다. 값은 파일로 넣는다: `az keyvault secret set --vault-name <이름> -n <비밀 이름> --file <값을 담은 파일>`
- **PostgreSQL:** 관리자 비밀번호는 `deploy-infra.sh`가 처음 배포할 때 만들어 Key Vault에만 둔다. 공용 접근에 "Azure 서비스 허용" 방화벽 규칙 하나만 열었다. 로컬에서 붙으려면 자기 IP 규칙을 따로 더한다. `POSTGIS`, `VECTOR`, `PG_TRGM` 확장을 허용했다. `CREATE EXTENSION`은 마이그레이션에서 한다.
- **지역:** 기본은 East US 2다. PostgreSQL과 AI Search, 그리고 DB 왕복 때문에 앱 VM이 Central US에 있다. East US 2에서 PostgreSQL B1ms는 `SkuNotAvailable`(영역 1~3 모두), AI Search는 `InsufficientResourcesAvailable`로 만들 수 없었다. 바꾸려면 `POSTGRES_LOCATION`, `SEARCH_LOCATION`을 준다.
- **채팅 모델:** `gpt-5-mini` GlobalStandard 200K TPM. 할당량은 구독 전체가 나눠 쓴다.
- **AI Search:** Basic, 시맨틱 랭커는 무료 플랜. 중지할 수 없어 켜 두는 동안 시간당 약 0.10달러가 든다.
- **재사용:** Azure ML 작업 영역 `team-workspace`는 기존 것을 쓰고 컴퓨팅만 더했다. 그 작업 영역에 딸린 저장소, Key Vault, Application Insights는 건드리지 않는다.

## 비용 (정가, 달러)

| 리소스 | 켜 둘 때 | 멈췄을 때 | 멈추는 법 |
|---|---|---|---|
| AI Search Basic | 시간당 0.101 (월 약 74) | 같음 | 멈출 수 없다. 지우고 다시 색인한다 |
| PostgreSQL B1ms | 시간당 0.017 (월 약 12) + 저장소 32GB | 저장소만 | `stop.sh` |
| Container Registry Basic | 월 약 5 | 같음 | 없음 |
| Azure ML 클러스터 | 작업이 돌 때만 | 0 | 최소 노드 0 |
| Foundry, Document Intelligence, Content Safety | 호출한 만큼 | 0 | 없음 |
| Key Vault, Storage, 로그 | 소액 | 소액 | 없음 |

## 앱 설정 (`deploy.sh`와 `appservice.bicep`)

**지금은 쓰지 않는다.** App Service 할당량이 0이고 Container Apps 공급자가 등록되지 않아, 앱은 위의 앱 VM 경로로 배포한다. 아래는 할당량이나 공급자 등록이 풀렸을 때를 위한 기록이다. 미실행·미검증이다. `appservice.bicep`은 `bicep build`도 돌려 보지 못했다. 처음 쓸 때는 `az deployment group what-if`로 먼저 본다.

앱 설정은 배포할 때마다 통째로 바뀐다. 포털이나 `az webapp config appsettings set`으로 손수 넣은 값은 다음 배포에서 사라지므로 아래 이름으로 넣는다. 기본값은 모두 "끔"이다.

| `deploy.sh` 환경 변수 | Bicep 매개변수 | 앱 설정 | Key Vault 비밀 |
|---|---|---|---|
| `V3_LLM` (`foundry` 기본, `rule`, `anthropic`) | `v3Llm` | `V3_LLM` | `anthropic`이면 `anthropic-api-key` → `ANTHROPIC_API_KEY` |
| `ANTHROPIC_MODEL` | `anthropicModel` | `ANTHROPIC_MODEL` | |
| `V3_LLM_ALL_VENDORS` | `v3LlmAllVendors` | `V3_LLM_ALL_VENDORS` | |
| `V3_AUTH` (`header` 기본, `token`) | `v3Auth` | `V3_AUTH` | `token`이면 `v3-org-tokens` → API와 프런트의 `V3_ORG_TOKENS` |
| `V3_CORS_ORIGINS` | `v3CorsOrigins` | `V3_CORS_ORIGINS` | |
| `V3_DISTANCE` (비움 기본, `none`, `kakao`, `azure`) | `v3Distance` | `V3_DISTANCE` | `kakao`면 `kakao-rest-api-key` → `KAKAO_REST_API_KEY`, `azure`면 `maps-key` → `AZURE_MAPS_KEY` |
| `V3_VENDOR_AGENTS` (JSON, 기본 `{}`) | `v3VendorAgents` | `V3_VENDOR_AGENTS` | 비어 있지 않으면 `vendor-service-key` → API의 `VENDOR_SERVICE_KEY` |
| `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_INDEX` | `searchEndpoint`, `searchIndex` | 같은 이름 | 주소를 주면 `search-admin-key` → `AZURE_SEARCH_API_KEY` |
| `APP_INSIGHTS=true` | `enableAppInsights` | `APPLICATIONINSIGHTS_CONNECTION_STRING` | `appinsights-connection-string` |
| `V3_DETECTOR_ENDPOINT`, `V3_DETECTOR_DEPLOYMENT` | `detectorEndpoint`, `detectorDeployment` | 같은 이름 | 주소를 주면 `detector-api-key` → `V3_DETECTOR_API_KEY` |
| (프런트) `WEB_API` (`direct`, `proxy`) | 이미지 빌드 인자 | `NEXT_PUBLIC_API_BASE` | |
| (프런트) | `v3BackendUrl` (비우면 `https://<API 주소>/api/v3`) | `V3_BACKEND_URL` | |
| (프런트) `AUTH_URL`, `AUTH_DEV_LOGIN` | `authUrl`, `authDevLogin` | `AUTH_URL`, `AUTH_DEV_LOGIN`, `AUTH_TRUST_HOST=true` | `auth-secret` → `AUTH_SECRET` |
| (프런트) `AUTH_ENTRA_ID`, `AUTH_ENTRA_ISSUER` | `authEntraId`, `authEntraIssuer` | `AUTH_MICROSOFT_ENTRA_ID_ID`, `AUTH_MICROSOFT_ENTRA_ID_ISSUER` | `auth-entra-secret` → `AUTH_MICROSOFT_ENTRA_ID_SECRET` |

- 비밀 값은 배포 매개변수로 지나가지 않는다. App Service에서는 앱 설정에 Key Vault 참조만 들어가고, Container Apps에서는 `deploy.sh`가 Key Vault에서 읽어 앱 비밀로 넣는다.
- Key Vault 참조는 그 기능을 골랐을 때만 넣는다. 비밀이 없는데 참조를 넣으면 풀리지 않은 참조 글이 값으로 남아 앱이 그것을 키로 읽는다. `deploy.sh`는 배포 전에 비밀이 있는지 보고, 배포 뒤에 참조 상태를 읽어 실패로 나온 것이 있으면 오류로 끝난다.
- `WEB_API=proxy`는 프런트에 `/api/v3` 프록시가 있어야 한다(브라우저는 프런트의 `/api/v3/*`를 부르고, 프런트 서버가 `V3_BACKEND_URL`로 넘기며 조직 토큰을 붙인다). `V3_AUTH=token`이면 `proxy`만 된다.
- 프런트 앱도 Key Vault를 읽으려고 시스템 할당 ID와 접근 정책(get)을 갖는다.
