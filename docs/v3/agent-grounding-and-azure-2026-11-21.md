# 근거 없는 기본값 제거·에이전트 지침·로컬 Azure 점검

2026-11-21 후속 변경. 브랜치 `codex/mentoring-evidence-local-20261121`. 배포·push 없음.
이 기록의 정책 변경은 같은 날짜의 앞선 검증 기록보다 우선한다. 앞선 330원 합의 시험은 당시 기본 +10% 정책의 결과이며 현재 기본 동작이 아니다.

## 실제로 바꾼 것

| 대상 | 이전 | 현재 | 코드 |
|---|---|---|---|
| 실사용 후보 | 실제 배출기업이 시연 업체와도 연결 가능 | 시연/실제 조직을 양방향 분리 | `agents/orchestrator.py:discoverable_profiles` |
| 근거 기본값 | ref 또는 assumption이면 순서대로 채택 | 가정·보류·synthetic/testOnly/demo 제외, ref는 original_checked 필요 | `evidence/acceptance.py`, `judge/basis.py` |
| 협상 인상 | 기본 +10%, 반올림 결과가 같으면 +1원 | 승인 기록 없으면 자동 조정 없음. 0%에 몰래 +1원 없음 | `judge/negotiate.py` |
| 시범 기간·단가 변동폭 | 기본 1개월·정책 변동폭 | 승인 기록 없는 기간·폭은 계약에 넣지 않음. 기간·종료 조건 별도 합의 | `agents/orchestrator.py` |
| 정책 결정 주체 | 운영자라는 일반 명칭 | 실제 승인 미확인, proposed 상태. approvedBy/reason/source/approvedOn 필수 | `judge/defaults.v1.json`, `defaults.py` |
| 공통 품질 등급 | 팀 임계값으로 A/B/C 등급 | 공통 등급 산정 보류. 업체가 등록한 매입 기준과 반입 검수는 별도 | `judge/common.py`, `intake/grade_model.py` |
| 실제 처리 방식 | 양수 단가이면 재활용 추정 경로 존재 | 업체가 명시한 공정이 없으면 미확인 | `outcome_carbon.py:applied_process` |
| 모델 연결 | 필수 설정 누락 시 규칙 응답으로 계속 | HTTP 서버도 strict 설정 검사. 선택한 Ollama 설정 없으면 오류/담당자 확인 | `api.py`, `agents/factory.py` |
| 에이전트 지침 | 목표의 출처가 불명확 | 업무 위임과 계산 근거 분리, 지침 버전과 출처 기록 | `agents/mandate.py`, `company.py`, `vendor.py` |
| 화면 | 정책이라는 설명만 있음 | 승인 기록 표시, 지침의 출처·효력 한계 안내 | frontend `agent-stage.tsx`, `evidence-panel.tsx` |

기존 가정값은 과거 근거를 재현할 수 있도록 저장소에서 지우지 않는다. 신규 계산에서는 선택되지 않는다. 테스트·시연 데이터가 존재한다는 사실을 실적이나 실제 업체의 자료처럼 설명하지 않는다. 자동 생성 시연 조직은 실제 계정의 거래 후보에서 제외한다.

## 근거와 적용 범위

전체 항목별 목록은 `evidence-audit-2026-11-21.json`과 `mentoring-evidence-2026-11-21.md`에 있다. 공식기관 명칭이나 URL만 있다는 이유로 원문을 확인한 계수로 승격하지 않았다.

| 자료 | 원문 위치·발행 | 사용하는 범위 | 적용하지 않는 범위 |
|---|---|---|---|
| [Environmental savings from plastic waste recycling, JRC122455](https://publications.jrc.ec.europa.eu/repository/bitstream/JRC122455/environmental_savings_from_plastic_waste_recycling_-_jrc_report_-_v9.pdf) | European Commission JRC, 2021, DOI 10.2760/6309. Table 2 인쇄 p.17, Annex 5 p.44. 문헌별 조사기간은 원문 표에 따른다 | 유럽 사용 후 폐플라스틱의 지정 비교 시나리오. 수율·신재 대체와 전 과정 경계를 함께 사용 | 국내 실측/인증 감축량, 산업공정 스크랩, 임의 열분해에 전용하지 않음 |
| [WARM v16 Management Practices](https://www.epa.gov/system/files/documents/2024-01/warm_management_practices_v16_dec.pdf) | US EPA, 2023-12, Exhibit 7-12, 인쇄 p.7-14 / PDF p.106. MMBtu/short ton | 원문에 해당하는 미국 시나리오의 에너지 효과. 입력 기준과 에너지 종류 유지 | 공장의 전력요금 절약액, 국내 시설 전력 실측값으로 표시하지 않음 |
| [국가 LCI 데이터 목록](https://www.data.go.kr/data/15120968/fileData.do) | KEITI/공공데이터포털의 목록 정보 확인 | 필요한 국내 원문 자료의 식별 | 원문 수치·경계를 읽지 못한 처리계수는 보류 |
| [AI RMF 1.0](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf) | NIST, 2023-01, DOI 10.6028/NIST.AI.100-1. GOVERN 2.1·3.2 p.23, MAP 3.5 p.27, MEASURE 2.5 p.29 | 역할, 사람 감독, 검증 한계 문서화 참고 | +10%, 최적 협상 전략, 시장 최적가격, 제품 인증의 근거가 아님 |
| [CP-SAT Solver](https://developers.google.com/optimization/cp/cp_solver/) | Google OR-Tools 공식 문서, 2026-11-21 열람, return values / integer constraints | 정수 제약과 OPTIMAL·FEASIBLE 등 계산 상태 해석 | 입력에 없는 업체·가격까지 포함한 시장 전체 최적성 증명 아님 |
| 프로젝트 담당자의 요구사항 | 이 작업의 멘토링·운영검증·근거화 지시, 2026-11-21 | 회사 측은 확인된 순손익 비교, 업체 측은 등록한 매입 조건·권한 범위 준수 | 논문으로 검증된 보편적인 협상 효율이나 업체 이익 극대화로 포장하지 않음 |

이번 후속 작업에서 새 탄소·에너지 숫자를 임의로 추가하지 않았다. 기존에 원문을 확인한 JRC/WARM 자료만 해당 조건에서 사용 가능하며 외국 문헌 추정임을 유지한다. 수율·품질 대체율·실제 용도·기능단위·경계가 없거나 맞지 않으면 보류한다. 국내 시설 실측, 운송사 계량/적재·회송 자료, 업체 견적·계약서는 이번에 새로 확보하지 못했다.

## 에이전트가 하는 협상

업무 목표는 프로젝트 담당자의 요구사항과 각 회사가 등록한 조건에서 온다. 모델은 입력과 도구 결과를 바탕으로 질문·설명을 작성한다. 허용 단가 조정은 승인된 요청 폭과 업체 한도를 계산 도구가 적용하고, 근거 검증기가 메시지의 수치와 조건을 확인한다. 회사와 업체 담당자가 각각 승인해야 거래가 확정된다.

Nash bargaining 또는 Rubinstein 균형을 구현·실증했다고 주장하지 않는다. 참고 시세, 매입 기준, 협상 합의 단가, 실제 검수 단가는 서로 다른 정보다. 정책 승인 필드는 운영자가 검토한 결정 기록을 보관하는 구조이며, 그 필드가 있다는 사실만으로 외부 기관이 정책을 인증한 것은 아니다.

## Azure 로컬 점검

공식 [Azure CLI Windows 설치 안내](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-windows?tabs=azure-cli&view=azure-cli-latest)에 따라 CLI 2.93.0 ZIP을 작업 폴더 `review_sources/azure-cli-local/runtime`에 설치했다. 시스템 PATH와 배포 리소스는 변경하지 않았다. 로그인 캐시는 두 Git 저장소 밖 `review_sources/azure-cli-local/auth`에만 둔다.

팀 계정 중 실제 배포 구독과 리소스 권한이 있는 계정으로 로그인한다. 개인 계정도 해당 권한이 있으면 가능하다. CLI 로그인은 관리 영역 인증이며, 실제 모델 추론 권한·네트워크 연결을 보장하지 않는다. Microsoft의 [keyless 인증 예제](https://learn.microsoft.com/en-us/samples/azure-samples/azure-openai-keyless-python/azure-openai-keyless-python/)도 모델 호출용 RBAC 권한을 별도로 요구한다. 이 예제의 배포 명령은 실행하지 않는다.

`scripts/check_azure_local.py`는 현재 앱의 `FoundryChat` 어댑터를 사용한다. 업무 문서 없이 연결 확인 JSON만 요청하며, 30초 제한·추가 재시도 없음·출력/추론 합계 상한 1,024토큰이다. 실제 호출 시 모델 사용료는 발생할 수 있다. API key 또는 CLI에서 받은 Entra 토큰을 메모리에서 사용하고 출력하지 않는다. 테스트 성공은 이 계정과 해당 모델 경로의 응답 확인이며 배포 앱의 Managed Identity, DB, OCR, Ollama, 전체 협상 품질 검증을 대신하지 않는다.

필수 환경: `FOUNDRY_ENDPOINT`(Azure OpenAI HTTPS 루트), `FOUNDRY_DEPLOYMENT`. API key 방식은 `FOUNDRY_API_KEY`도 필요하다. 비추론 모델은 `FOUNDRY_REASONING_EFFORT=none`. 실행은 backend에서 `python -m scripts.check_azure_local --auth azure-cli --az <로컬 az.cmd 절대경로>`. 비밀값은 명령 인자나 문서에 적지 않는다.

현재 실제 연결 상태와 최종 테스트 결과는 아래 검증 결과 절에 기록한다. 모의 transport 시험은 실제 Azure 연결 성공으로 집계하지 않는다.

## 발표 표현

- 가능: “적용 조건이 일치하는 원문 자료와 사용자 증빙만 계산에 사용하고, 부족한 항목은 필요한 입력과 함께 보류합니다.”
- 가능: “협상 목표는 업무 위임, 가격 조정은 승인된 정책과 업체 권한에 따르며 양측 담당자가 확정합니다.”
- 금지: “모든 탄소 수치는 국내 실측”, “판매 가능한 인증 배출권”, “논문이 입증한 최적 협상”, “NIST 인증”, “CLI 설치만으로 Azure 통합 검증 완료”.

## 검증 결과

- 백엔드 변경 관련 회귀 **414 passed**, 3개 SWIG deprecation 경고, 195.15초. 별도 등급 모델 시험 **10 passed**, 0.27초로 총424개 통과. 전체 저장소의 모든 평가 세트 통과를 주장하지 않는다. 로그 `../../../grounding-final.log`, `../../../grounding-grade.log`.
- 프런트 단위 **261 passed / 3 skipped**, 변경 파일 ESLint 통과, 프로덕션 빌드·TypeScript·30개 페이지 생성 통과. 로그는 두 저장소의 상위 폴더 `grounding-frontend-tests.log`, `grounding-build.log`.
- 두 계정 실제 Chrome UI 검증 통과: 승인 전 문서 없음, 양측 승인 뒤 3개 초안, 단가300원·900kg에서920kg 정정, 보고서/CSV276,000원 일치, 탄소 보류, 정정 전 기록 유지, 타 조직 비공개 접근 차단. 입력 회사·증빙은 가상 시험 자료로 명시했다. 스크린샷·동영상·trace는 수집하지 않았다. 시험 서버와 전용 Chrome은 종료했다.
- 첫 회귀는 374통과/23실패였다. 구 가정 기대값을 보류 동작으로 바꾸고, 승인 정책 시험은 명시적인 시험 승인 기록을 넣었다. 과거 수치 보존 시험은 **테스트 모듈 안에서만** 원문 채택 검사를 모의한다. 실제 제품의 testOnly 차단은 별도 검증하며, 역사 재현 시험의 가상 수치가 공식 계수라는 뜻이 아니다. 최종 414개는 새 연결 검사와 추가 사례를 포함한다.
- 등급 모델 시험은 처음 5통과/5실패였다. 원자료 측정값이나 연결 가능한 모델이 있더라도 공통 등급 기준 자체가 검증되지 않으면 산정 보류해야 한다는 새 동작으로 기대값을 수정했다. 원시 시험용 분류기와 합성 데이터 시험은 실제 현장 정확도를 주장하지 않는다.
- 화면 시험의 첫 재실행은 목록 첫 번째 조직을 배출기업으로 오인해 마지막 접근 검사에서 실패했다. 테스트 대상 회사명을 정확히 찾아 검사하도록 고쳤고, 정상 배출기업의 비공개 경로 차단을 재확인했다.
- **Azure 실제 연결 성공**: 팀 계정 CLI 로그인 후 저장소 배포 설정과 같은 `team_AIBIC2026_03_RG_EUS2`를 확인했다. `aif-rsai-demo-o7xpbt`의 `gpt-5-mini` 배포(모델 버전2025-08-07)에 로컬 `FoundryChat`으로 실제 1회 요청하여 연결 확인 JSON을 받았다. Entra 인증, API2024-10-21, 30초/1,024토큰 제한. 키·토큰을 로그에 기록하지 않았다. `text-embedding-3-small`은 배포 목록만 확인했으며 추론 호출은 하지 않았다.
- Azure 앱 VM `vm-rsai-demo-app`은 running, GPU VM `vm-rsai-gpu`는 deallocated였다. 시작·중지·재배포·방화벽·RBAC 변경 없음. 운영 서버에 수정 코드를 올리지 않았다.
- **미검증**: 실제 두 회사의 Azure 기반 전체 협상, 배포 VM의 현재 모델 환경변수와 컨테이너 SHA, 운영 DB/OCR/Search 연결, Ollama/GPU 실행, 등급 모델의 실제 현장 정확도, 실주소 운송, 국내 시설 계량 자료. CLI 점검은 로컬 앱의 영구 API-key 설정을 자동 변경하지 않는다.
