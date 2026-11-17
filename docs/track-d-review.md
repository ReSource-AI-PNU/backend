# D 트랙 코드 검토 체크리스트

검토일: 2026-11-13 (한국 시간)

> 이 문서는 11월 13일의 구현 현황 기록이다. 최신 완료 판단은 [2026-11-16 코드·로컬 감사](track-d-audit-2026-11-16.md)를 따른다. 기본 자동검사는 통과했지만 추가 재현에서 핵심 결함 4개를 확인했으며, 수정 전 검토용 Draft PR로 제출한다. 아래 커밋·푸시 여부는 당시 기록이다.

**회의 분담표의 D 트랙 14개 항목에 대한 로컬 구현과 연동 코드를 작성했다. 원격 푸시·머지·커밋은 하지 않았다. 실제 기관/업체/다른 트랙 서비스 연결과 PostGIS 실서버 검증은 아직 별도 확인이 필요하다.**

## 사용자 검토

- [ ] `http://127.0.0.1:3010/track-d`에서 새 가상 진단을 생성한다.
- [ ] 물량 확인 → 묶음 계산 → 참여 제안 승인 → 이웃 동의 → 협상 → 합의안 승인을 확인한다.
- [ ] `/upload`에서 실제 입력 형태로 여러 흐름을 등록하고 `/session`에서 저장·재진입을 확인한다.
- [ ] 단계 이어서 실행이 미확인 물량·질문·참여 동의에서 멈추는지 확인한다.
- [ ] 문서 내용을 보고 이행안 승인·동결 후 수정이 차단되는지 확인한다.
- [ ] 변경 코드와 아래 브랜치 기준을 검토하고 수정할 점을 전달한다. 원격 반영은 아직 하지 않는다.

## 브랜치

| 저장소 | 로컬 브랜치 | 출발점 |
|---|---|---|
| track-d/backend | feat/track-d-platform | main `5d09b65824483223da9ae8b786fec2b441bd7713` |
| track-d/frontend | feat/track-d-platform | feat/walkthrough `b783fb104d4ec132899c2dc645389482692a2a44` |

기존 상위 backend/frontend/review_sources는 변경하지 않았다. specs 복제본도 수정하지 않았다. 프런트 main에 전체 화면이 없어 feat/walkthrough를 기준으로 했다. feat/route-preview-api와 합치지 않았다. 실제 변경은 위 두 브랜치의 미커밋 작업 트리에 있다.

## 회의 분담표 14개 항목

| 체크 | 이슈 | 구현 및 검증 근거 |
|---|---|---|
| [x] | ReSource-AI#5 공공데이터 수집·적재 | JSON/CSV/XML 수집, 페이지 순회, 5종 필드 매핑 설정, 출처 보존, 중복·실패 시 원자적 적재. 기관 실자료 검증은 별도 |
| [x] | backend#1 데이터 계약 | 기존 schemas.py 계약 유지·확장, 승인 상태/미산정 탄소/비공개 물량 표현, OpenAPI 생성 |
| [x] | backend#2 세션·오케스트레이터 | 흐름별 저장/답변/질문, CAS 버전 충돌, 6단계 실행, A/B/C HTTP 어댑터, 사람 승인·동결 게이트 |
| [x] | frontend#2 API 클라이언트·타입 | 생성 TypeScript, Auth.js BFF, 동일 출처 검사, DB 기반 회사 권한 |
| [x] | ReSource-AI#7 기업 시드 | 8개 단지 100곳·녹산 30곳, 중복 실행 가능한 가상 시드 |
| [x] | backend#13 DB·마이그레이션 | 조직/멤버/회사/세션/흐름/수신인/발송함/원문 자료, PostGIS 공간 인덱스, Alembic 3단계, 격리 실DB 테스트·CI |
| [x] | backend#14 OR-Tools 순회 | 적재량·분할 방문·시간창, 도로 행렬/추정 거리 명시, 회차별 물량 |
| [x] | backend#23 프로필·공개 범위 | 회사·업체 조건 편집, 타사 물량 비공개, 허가 자기 인증 금지, 운영자 증빙 검토 도구 |
| [x] | backend#24 참여 제안·회신 | 승인 후 수신함 등록, 수신 회사만 동의/불참, 불참 제외와 대체 후보, 인덱스 조회 |
| [x] | frontend#7 묶음 화면 | 기존 /session/[id]/matching 연결, 흐름 선택, 후보/확정 물량·충족률·순회 순서·참여 상태 |
| [x] | backend#15 허가·거래처 | 품목·수지·성상·처리 경로·검증 여부 대조, 매각/열분해 상대 선정, 무허가 대체 차단 |
| [x] | backend#34 A2A 협상 | A2A 0.3 JSON-RPC 전송, 가드레일·최대 라운드·오류 처리·사람 승인, 모의 업체와 운영 업체 구분 |
| [x] | backend#33 명령 | 제한된 의도 분류, 요약/흐름 경로 변경/후속 초기화, B 서비스 호출, 승인·발송 명령 거부 |
| [x] | frontend#11 업체·이웃 화면 | 기존 /vendor·/neighbor의 DB 수신함, 동의·불참과 사유, 접근 기업별 공개 범위 |

체크는 **D의 코드가 작성되어 로컬 검토 가능한 상태**를 뜻한다. 기관 계정 신청, 외부 API의 실제 구현, 법령 판단, 업체 동의까지 끝났다는 뜻은 아니다. A의 업로드·모델·걸으며 진단, B의 운영 손익·탄소 엔진/대시보드, C의 법령 RAG·문서 생성·여권·배포는 각 트랙의 담당 범위다. D는 그 결과를 받는 계약과 저장·실행 연결을 제공한다.

## 이번에 연결한 화면

- /track-d: 전체 검토 작업 화면
- /upload: 여러 흐름 직접 입력으로 세션 생성. 현장 매체 업로드/영상 처리는 A 서비스 범위
- /session, /session/[id] 및 6단계 하위 경로: 같은 DB의 목록·단계별 결과·질문·승인
- /neighbor, /vendor: 실제 수신인 인덱스로 조회하는 수신함
- /settings: 회사 위치·공급 흐름·수거 정책·시간창
- /settings/members, /join: 멤버 초대·역할·제거·수락

PLATFORM_API_URL이 없으면 기존 팀 모의 화면을 유지한다. 다른 트랙의 대시보드·여권·워크스루를 운영 연결 완료로 표시하지 않았다.

## 개발 검증

- [x] Python 전체 테스트 82개 + 기존 하위 검사 40개 통과
- [x] 프런트 타입 검사·전체 린트·프로덕션 빌드 통과
- [x] 가상 통합 브라우저 E2E 5개 통과
- [x] 로그인 조직·멤버 초대·권한 위조 차단 E2E 1개 통과 (개발 로그인 사용)
- [x] 모바일 가로 넘침 검사, 생성·매칭·단계 화면 캡처 확인
- [x] SQLite 실제 마이그레이션 및 새 저장 구조 적용
- [ ] PostGIS 실서버 테스트: 로컬 접속 정보 없어 1개 skip. 실행 코드와 CI 작성
- [ ] 실제 Entra 로그인·외부 A/B/C·A2A·Azure Maps·기관 데이터: 실계정/서비스 연결 후 검증. 외부 응답은 MockTransport로 계약·실패 동작 검사
- [ ] GitHub CI 원격 실행: 푸시하지 않아 실행하지 않음

최종 결과는 verification.json에 기록한다. 검토 DB에는 테스트 진단이 남아 있으며, 새 진단을 만들어 검사하면 된다.

## 사용자가 직접 준비할 일

- [ ] 팀에 프런트 feat/walkthrough 기준과 D 계약을 공유하고 검토하기
- [ ] PostgreSQL/PostGIS 테스트 DB 또는 Docker 실행 환경 준비하기
- [ ] Entra 앱 등록/리디렉션 주소/테스트 계정 접근권한 준비하기
- [ ] A/B/C 담당에게 실제 서비스 주소와 integration-contracts.md의 입력·응답 계약 확인받기
- [ ] 실제 업체에 A2A 0.3 시험 주소·인증·견적 응답·중복 처리 계약 확인받기
- [ ] 업체 인터뷰로 허가 증빙, 최소 물량의 월/회차 기준, 차량 적재량, 수거 시간·요일·횟수·단가 확인하기
- [ ] 공공데이터 활용 신청·이용조건·원문 API/파일과 실제 필드 정의 확보하기
- [ ] 도로 경로를 쓰려면 Azure Maps 키와 해당 지역 이용 가능 여부 확인하기

비밀키·비밀번호는 채팅이나 Git에 올리지 말고 로컬 환경 파일 또는 비밀 저장소에 넣는다. 위 접근 정보가 준비되면 개발자가 연동 설정을 채우고 실환경 검증을 진행할 수 있다. 현재 코드는 비어 있는 연결을 실제 성공으로 처리하지 않는다.

## 실행

현재 PC에서는 track-d/start-review.ps1, stop-review.ps1을 사용한다. API는 127.0.0.1:8010, 화면은 127.0.0.1:3010이며 숨김 프로세스로 실행한다. 시스템 Node 22.11 충돌 때문에 앱에 포함된 Node 24를 선택한다. 전역 설치는 변경하지 않았다.

새 환경: Python 3.12에서 `uv sync --extra dev` → `python ../setup-review.py` → `alembic upgrade head` → `python -m scripts.seed_platform --demo`. 프런트는 Node 24에서 `npm ci`. setup-review.py는 이미 있는 환경 파일을 덮어쓰지 않는다.

백엔드 검사: `pytest -q`, `ruff check app/platform scripts migrations tests/test_platform.py tests/test_platform_postgis.py`, `python -m scripts.export_openapi`.
프런트 검사: `npm run generate:api`, `npm run typecheck`, `npm run lint`, `npm run build`, `npm run test:track-d`.
로그인 검사는 PLATFORM_DEMO=false·AUTH_DEV_LOGIN=true인 로컬 서버에서 TRACK_D_AUTH_E2E=1로 `playwright test tests/track-d-auth.spec.ts`를 실행한다. 실제 Entra 연동 검사를 대신하지는 않는다.

연동 상세, 발송 재시도 계약, 데이터 수집 및 허가 검토 명령은 integration-contracts.md를 참고한다.
