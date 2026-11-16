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
- **PostgreSQL:** 관리자 비밀번호는 `deploy-infra.sh`가 처음 배포할 때 만들어 Key Vault에만 둔다. 공용 접근에 "Azure 서비스 허용" 방화벽 규칙 하나만 열었다. 로컬에서 붙으려면 자기 IP 규칙을 따로 더한다. `POSTGIS`, `VECTOR`, `PG_TRGM` 확장을 허용했다. `CREATE EXTENSION`은 마이그레이션에서 한다.
- **지역:** 기본은 East US 2다. PostgreSQL과 AI Search만 Central US에 있다. East US 2에서 PostgreSQL B1ms는 `SkuNotAvailable`(영역 1~3 모두), AI Search는 `InsufficientResourcesAvailable`로 만들 수 없었다. 바꾸려면 `POSTGRES_LOCATION`, `SEARCH_LOCATION`을 준다.
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
