# 로컬 검증 기록 — 2026-11-21

이전 판 검증이다. [후속 검증](agent-grounding-and-azure-2026-11-21.md)이 현재 동작과 Azure 실제 연결 결과를 기록한다. 아래 330원/303,600원은 이전 정책 시험 결과다.

브랜치: 백엔드와 프런트엔드 모두 `codex/mentoring-evidence-local-20261121`.
기준: backend `dc32960`, frontend `04b0419`. 운영 서버의 현재 배포 SHA는 이번 로컬 작업에서 다시 조회하지 않았다. 사용자 제공 운영 재현 결과와 아래 로컬 실행 결과는 별개다. push, 배포, 운영 계정/자료 변경, 외부 메시지, 결제, 서명을 실행하지 않았다.

## 실행 환경과 증적

- Windows, Python 3.12, Node 24.22.1. 프런트엔드 Next 16.4.6 / React 19.3.0 / next-auth 5.0.0-beta.32. 저장소 지정 버전의 Next로 프로덕션 빌드했다.
- 디스크 부족으로 전체 의존성 재설치 대신 기존 패키지 및 작업 전용 추가 패키지를 junction으로 연결했다. `npm ci`를 새 환경에서 수행한 재현 검증은 아니다. 패키지 매니페스트·lockfile은 수정하지 않았다.
- 백엔드 메모리 저장소 + BFF 인증 + 규칙 정책. 실제 클라우드 LLM 호출 없음. 악성 모델 응답 검사는 ScriptedLLM/모의 응답이며 실제 모델 평가가 아니다.
- 프런트엔드는 `next build --webpack`, `next start`로 실행했다. 타입 검사 및 30개 페이지 생성 완료. 빌드 로그: 두 저장소 상위의 `local-build-final.log`.
- 화면 검증: 실제 Chrome + Playwright, 두 독립 브라우저 context, 새 가상 계정. 자동 스크린샷·녹화·trace 없음. 합성 텍스트 허가증과 매입표로 로컬 추출 경로를 사용했으며 실제 OCR 모델·허가 진위를 검증한 것이 아니다.

## 최종 실행 결과

- **최종 통합 실행: 백엔드 358 passed / 0 failed**, 182.21초, 외부 라이브러리 deprecation 경고 3개. 아래 핵심281개와 추가77개를 마지막 코드에서 한 번에 실행했다. 로그 `combined-final.log`.
- 핵심 백엔드 회귀: **281 passed** (154.08초, 라이브러리 deprecation 경고 5개). 신규 근거 검증, 계약 검토, 가드레일, 계산 근거 공유/권한, 문서 렌더링, 단위, 스캔, 인증, 악성 모델/입력 120개 시나리오 포함. 로그 `mentoring-final.log`.
- 프런트엔드 단위: **261 passed / 3 skipped / 0 failed**. skip은 백엔드 venv 경로 부재를 검사하는 전이 시험 1개, `V3_API_BASE`가 필요한 실행 서버 스키마 시험 2개다. 성공으로 세지 않았으며 별도 BFF 브라우저 흐름을 실제 실행했다. 로그 `frontend-tests-final.log`.
- 변경 프런트엔드 ESLint, 프로덕션 빌드/TypeScript: 통과.
- 두 사용자 브라우저 흐름: **통과**. `frontend/scripts/verify-mentoring-local.mjs`, 로그 `browser-verification-final.log`.
- 추가 회귀(자원순환·실적·과거 기준·검수·협상·허가): **77 passed** (36.96초, 라이브러리 deprecation 경고 3개). 로그 `extended-final.log`.

프런트엔드 로컬 커밋: `759e3f7`. 최종 화면 재검증도 통과했으며, 검증용 두 서버와 전용 Chrome은 종료했다. 백엔드 변경은 이 문서를 포함한 같은 이름의 로컬 브랜치 커밋으로 보존한다. 원격 전송은 하지 않았다.

핵심 회귀 명령(backend 루트, 의존성 설치 환경):

```powershell
python -m pytest tests/v3/test_mentoring_evidence.py tests/v3/test_contract_review.py tests/v3/test_guardrails.py tests/v3/test_shared_calc_evidence.py tests/v3/test_view_evidence_scope.py tests/v3/test_render.py tests/v3/test_units.py tests/v3/test_scan.py tests/v3/test_scan_demo.py tests/v3/test_fix2_auth.py tests/v3/test_rules_adversarial.py -q -p no:cacheprovider
```

## 두 계정 화면에서 확인한 수정 전/후

| 사례 | 기존 문제/미검증 | 이번 실제 화면 결과 |
|---|---|---|
| 가격 범위 | 하한100/상한90 저장 가능 | 저장 거절 메시지. 정상 음수 하한 -100/상한1000 저장 |
| 기존 처리 | 미입력인데 소각·250원 표시 | 확인 필요로 표시. 담당자가 소각·250원을 입력해야 반영 |
| 업체 자료 | 허가 추출값과 원문 대조 혼동 가능 | 가상 문서 추출값 확인 후 배출기업의 별도 원문 대조 기록 |
| 협상 | 정책 조정과 모델 자율 결정을 혼동 | 기준표300원→규칙 요청/한도 검토→합의330원, 규칙 실행으로 표시 |
| 양측 승인 | 한쪽 승인만 검증하는 위험 | 한쪽 승인 뒤 문서 없음. 두 번째 승인 뒤 요청서·계획서·계약서 초안 생성 |
| 운반비 | 요청서 ‘운송비 제외’ 고정 문구 | 세 문서 모두 ‘운반비 단가 포함’ |
| 다음 등급 | A등급 한 줄인데 다음 등급 단가 참조 | 없는 다음 등급 문구 없음. 반입 보류·재협의 기준 |
| 검수·정정 | 중복 집계/과거 값 변경 위험 | 900kg×330원 기록→920kg 정정. 최신 금액303,600원, 정정 전900kg 유지 |
| 보고서·CSV | 화면과 내보내기의 기준 불일치 위험 | 화면/CSV 모두920kg·330원·303,600원. 정정 사유와 전판 포함. 탄소 미산정 |
| 비공개 | 타 조직 ID 변경 접근 위험 | 업체 계정으로 배출기업 비공개 streams 요청403/404 |

테스트 문서의 상호에서 run ID가 누락된 초기 시도는 원문 대조 선택지가 없어 실패했다. 서비스가 타 업체 문서로 배제한 정상 결과였으며, 테스트 입력 회사명을 일치시킨 후 통과했다. 이를 제품 오류로 숨기거나 검사를 우회하지 않았다.

## 추가 검사 및 미검증

처음 추가 회귀 실행은 **43 passed / 34 failed**였다. 원문 미확인 계수로 탄소를 계산하고, 누락 처리비/수율을 자동 채우며, 중심점 거리만으로 탄소 최적을 고르는 이전 기대값이 새 요구와 충돌했다. 관련 시험을 보류 정책에 맞추고, 수치가 있는 과거 실적 보존 시험은 `tests/v3/evidence_fixtures.py`의 명시적 가상 계수로 분리했다. 해당 fixture의 `original_checked`는 검증 완료 상태를 모의하는 테스트 입력이며 실제 자료 조사 결과가 아니다.

이 과정에서 이전 형식으로 이미 저장된 실적 기준을 신규 원문 검증 규칙 때문에 없앨 수 있는 경로도 수정했다. 과거 기록은 당시 계수·수치와 ‘현재 근거 검증을 보증하지 않음’ 주석으로 보존하고, 원래 계산 기준이 없는 기록은 오늘 값으로 채우지 않는다. 새 계수 버전을 계산 그래프에서 고정한 경우에도 원래 버전 번호와 처리 종류를 추적한다.

추가 회귀 재실행 명령:

```powershell
python -m pytest tests/v3/test_circular.py tests/v3/test_report.py tests/v3/test_report_snapshot.py tests/v3/test_outcome_inspections.py tests/v3/test_outcome_carbon.py tests/v3/test_negotiation.py tests/v3/test_permit_level_transport_carbon.py tests/v3/test_permit_verification.py -q -p no:cacheprovider
```

전체 저장소 테스트/평가 세트의 최종 통과를 주장하지 않는다. 초기 광범위 실행은 의존성·디스크 부족과 구 정책 기대값으로 실패/중단됐고, 변경 관련 회귀를 선별해 최종 실행했다. 운영 배포 E2E, 실제 배포 모델 조합, 외부 Foundry/Anthropic/Ollama, 클라우드 OCR, H100 재구성 실행, 실주소 라우팅, SQL 운영 데이터 마이그레이션은 이번 검증에 포함되지 않는다. 코드에 있는 기본 모델 이름은 실제 운영 모델의 증거가 아니다.

## 화면 검증 재실행

1. backend 루트에서 `python scripts/serve_local_verification.py`. loopback5800, 메모리 저장소, 가상 시드, BFF/규칙 정책을 강제한다.
2. frontend 루트에서 아래 환경으로 빌드/실행한다. 아래 키는 공개된 **로컬 검증 전용 값**이며 운영에 사용하면 안 된다.

```powershell
$env:NEXT_PUBLIC_API_BASE='/api/v3'
$env:V3_BACKEND_URL='http://127.0.0.1:5800/api/v3'
$env:V3_AUTH='bff'
$env:V3_BFF_KEY='local-evidence-verification-only-20261121'
$env:AUTH_SECRET=[guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$env:AUTH_TRUST_HOST='true'
$env:AUTH_URL='http://127.0.0.1:5300'
$env:AUTH_DEV_LOGIN='false'
npx next build --webpack
npx next start --hostname 127.0.0.1 --port 5300
```

3. 별도 테스트 프로필 Chrome을 loopback remote-debugging 포트59333으로 실행하고 `node scripts/verify-mentoring-local.mjs`. 재시도 전 메모리 backend를 재시작해 앞선 가상 업체가 후보에 섞이지 않도록 한다. 스크립트는 loopback 외 주소를 거절한다.

## 후속 우선순위

1. 국내 원문 계수의 기능단위·공정 경계·운송 적재/회송 및 적용 라이선스 확인. 현재 수치 유지/사용 보류.
2. 실제 시설 수율·품질시험·용도별 대체율·계량표·운반/선별/세척 견적 확보 후 검토된 입력으로 등록.
3. 별도 자원과 권한이 있는 환경에서 전체 평가 정답 갱신 및 실제 모델·OCR·운영 저장소 통합 시험. 모델 호출의 통화 금액 예산 차단은 거래 승인과 별도로 구현 필요.
