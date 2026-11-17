# D 트랙 연동·운영 설정

코드는 `feat/track-d-platform` 로컬 브랜치에 있으며 원격 반영하지 않았다. D가 제공하는 전송 계약을 A/B/C 및 상대 업체가 구현·확인해야 한다. 존재하는 팀 API라고 가정해 임의 주소를 호출하지 않는다.

## 실행 설정

백엔드 `.env.example`, 프런트 `.env.example`을 참고한다. 양쪽 `PLATFORM_SERVICE_KEY`는 동일한 임의 비밀값을 사용한다. 브라우저에는 전달하지 않는다. 프런트는 `PLATFORM_API_URL`이 설정되면 D 연결 화면을 사용하고, 없으면 원래 팀의 모의 화면을 유지한다.

운영은 `PLATFORM_DEMO=false`, `AUTH_DEV_LOGIN=false`, Entra 로그인 설정이 필요하다. Auth.js 사용자 ID·이메일만 신뢰된 BFF가 전달하며, 실제 회사 역할은 백엔드 멤버 DB에서 조회한다. 브라우저의 역할 헤더를 신뢰하지 않는다. 다른 회사 ID를 지정하면 403이다. 가입은 `/onboarding`, 회사 위치·공급 흐름·업체 조건은 `/settings`, 초대·역할 관리는 `/settings/members`, 초대 수락은 `/join`이다. 초대 토큰은 7일·1회 사용·이메일 일치 조건이며 DB에는 해시만 보관한다. 초대 메일은 자동 전송하지 않는다.

검토용 가상 회사 전환은 `PLATFORM_DEMO=true`와 루프백 Host에서만 허용한다. 테스트는 등록된 `isDemo` 기업만 선택할 수 있다. 가상 세션의 B 계산·규정·문서는 표시된 시연 데이터이며 탄소 값이 없으면 null이다.

## A/B/C 단계 HTTP 계약

`PLATFORM_STAGE_ENDPOINTS`는 JSON 객체이다. 사용 키는 `classify`, `feasibility`, `regulation`, `cost-carbon`, `proposal`, `dispatch`다. `matching`은 D 내부 구현이다. 모든 URL은 서버 설정으로만 받는다. HTTPS를 사용하며 local/test 환경에서만 localhost HTTP를 허용한다. 리다이렉트는 따르지 않는다.

```json
{"feasibility":"https://your-b-service.example/feasibility","regulation":"https://your-c-service.example/regulation"}
```

위 주소는 설명용 예시다. 실제 서비스 주소로 바꿔야 한다. `PLATFORM_STAGE_TOKEN`은 Bearer 인증값이다. 단계 호출은 POST이며 다음 본문을 보낸다.

```json
{"contractVersion":"resource-stream-v1","step":"feasibility","session":{"id":"ses_...","streams":[],"questions":[],"corrections":[]}}
```

`session`에는 실제 `app/schemas.py.Session` 전체가 들어간다. 위 JSON은 핵심 필드만 줄인 설명이다. `Idempotency-Key`는 `sessionId:version:step`이다. 응답은 `{"result": <해당 단계 결과>, "questions": []}`이며 결과 타입은 FeasibilityResult / RegulationResult / CostCarbonResult / ProposalResult다. 식별은 `{"streams":[...],"questions":[]}`이다. 식별 모델은 사람의 확정 물량·출처를 덮어쓰지 못한다. 외부 응답에 들어 있는 질문 answer는 사람의 기존 답변과 일치할 때만 보존한다. 흐름 ID 누락·중복·외부 참조, 잘못된 시나리오, 통신 오류는 해당 단계에서 중단한다.

경로 변경 명령은 `Correction(field="route", after=<경로>)`를 포함해 B 서비스를 다시 호출한다. B가 요청한 경로로 판정하지 않거나 정보 부족이면 기존 결과를 유지한다. 지원하는 명령은 상황 요약, 특정 흐름의 재투입/매각/묶음/열분해/소각 경로 변경, 규정/매칭/비용/이행안 이후 결과 초기화다. 임의 자연어 명령을 범용 실행하지 않는다. 승인·발송은 명령으로 수행할 수 없다.

API 전체 계약은 `docs/openapi.json`이며 프런트 `src/lib/platform/generated.ts`를 생성한다. 외부 서비스 미설정 시 운영 결과를 시연 결과로 대체하지 않는다. 실제 법령 색인·식별 모델·손익 및 탄소 엔진·문서 작성 엔진 자체는 각 담당 트랙의 범위다.

## A2A 업체 협상

`PLATFORM_A2A_PEERS` 형식은 `{"업체ID":{"url":"https://업체주소","token":"비밀값"}}`이다. [A2A 0.3 JSON-RPC](https://a2a-protocol.org/v0.3.0/specification/)의 `message/send`를 구현했다. 1.0과 혼용하지 않는다. DataPart 내부 계약은 다음과 같다.

```json
{"schema":"resource-pickup-v1","terms":{"day":"목","slot":"오전","perKg":140,"perMonth":2},"weightKg":1230,"binding":false}
```

업체 회신은 같은 schema, terms, `accepted: true/false`를 포함한 DataPart 하나다. JSON-RPC 응답 ID를 대조한다. Message 및 Task의 `completed`/`input-required` 응답을 해석하고 필요한 contextId/taskId를 다음 라운드에 전달한다. 비동기 working/submitted는 진행 중으로 보고 사람 확인 상태로 종료한다. 제한 없는 폴링은 하지 않는다. 가드레일 밖 제안·라운드 초과·형식 오류는 미합의다. 합의는 견적안이며 별도 사람 승인 전 계약이 아니다. 상대는 Idempotency-Key/messageId를 중복 처리하지 않아야 한다.

## 도로 거리·시간창·회차별 물량

`AZURE_MAPS_KEY`를 설정하면 [Azure Maps 2025-01-01 트럭 거리 행렬](https://learn.microsoft.com/en-us/rest/api/maps/route/post-route-matrix?view=rest-maps-2025-01-01)을 사용한다. longitude/latitude 순서, 과거 교통 기준이며 최대 50개 고유 지점을 받는다. 실제 키·서비스의 해당 지역 커버리지는 별도 확인한다.

대체 공급자는 `PLATFORM_ROAD_MATRIX_URL`과 `PLATFORM_ROAD_MATRIX_TOKEN`으로 연결한다. POST `{"contractVersion":"resource-matrix-v1","points":[[경도,위도]],"travelMode":"truck"}` → `{"meters":[[0]],"seconds":[[0]]}`의 정방 행렬이다. 일부 경로 실패·누락·음수·비유한 값이면 경로를 보류한다. 도로 서비스가 아예 미설정이면 직선거리·시속 30km 가정임을 화면에 명시한다. 설정된 서비스 실패를 직선거리로 숨겨 대체하지 않는다.

기업 `pickupWindow`는 0시부터 분 단위 start/end/serviceMinutes다. 기본 08:00~18:00, 방문당 15분은 등록 전 기본값이므로 업체 확인 후 바꾼다. OR-Tools는 차량 적재량과 시간창, 분할 방문을 적용한다. 협상 승인한 오전/오후 및 월 횟수에 맞춰 다시 계산한다. `minimumBasis=pickup`이면 월 확정 물량/월 횟수가 최소 수거량을 충족해야 한다. `month`는 월 계약 최소 물량이다. 경로는 회차별 물량을 사용한다. 산출 차량 수는 필요한 차량 수이며 실제 차량 배정·예약은 별도 업무다.

불참 후 `/bundles/{id}/replace`는 기존 동의와 불참 기록을 보존하며 다른 후보를 추가한다. 새 후보는 별도의 참여 제안 승인·동의를 거쳐야 한다. 참여 회신은 해당 기업으로 등록된 수신인만 할 수 있다.

## 승인·발송

`POST /sessions/{id}/approval`에 version, confirm=true, routeId를 보내면 검토한 이행안을 동결한다. 후속 세션 변경은 409이며 외부 문서는 outbox에 저장한다. 이 단계는 외부 발송을 하지 않는다. `GET /sessions/{id}/deliveries`로 목록을 확인한 다음 `POST`에 version, confirm=true로 별도 발송한다.

C의 dispatch 서비스에는 `contractVersion=resource-dispatch-v1`, deliveryId, sessionId, document, approval을 전달한다. **동일 deliveryId/Idempotency-Key는 반드시 한 번만 발송**하고 같은 영수증을 반환해야 한다. 응답 `{"status":"sent","receiptId":"영수증ID"}`만 발송 확인으로 인정한다. 타임아웃은 발송 실패가 아닌 `uncertain`이다. 재시도는 같은 ID를 사용한다. 프로세스 중단으로 남은 sending은 timeout+60초 lease 후 같은 ID로 재확인한다. DB claim으로 동시 중복 요청을 막고, 영수증을 영속 저장한다. 가상 문서는 외부로 발송할 수 없다.

## 공공데이터 수집과 허가 확인

`docs/sources/*.example.json`은 5종 자료용 설정 양식이다. 실제 기관 URL·필드명이 확인되기 전에는 실행할 수 없는 예시이다. 기관별 endpoint 환경변수, 레코드 경로, 원문 ID, 정규화 필드 매핑, 페이지 변수, 인증 환경변수를 채운다. JSON/CSV/XML을 지원하며 XML의 DTD·외부 엔터티는 거부한다. 인증정보는 secret_params에 환경변수 이름으로 참조하고 공개 원문 source_url에는 넣지 않는다.

```sh
python -m scripts.collect_public_data docs/sources/prices.local.json
python -m scripts.collect_public_data docs/sources/prices.local.json --import-db
python -m scripts.import_public_data prices docs/public-data-template.csv
```

기본은 수집·정규화 검증만 하고 `--import-db`에서 전체 페이지를 한 트랜잭션으로 적재한다. 중간 실패·중복 페이지·상한 초과 시 부분 적재하지 않는다. 원문 출처·수집일·이용조건·ID를 보존하며 verified=false로 저장한다. 공공데이터는 판정 엔진에 자동 반영되지 않는다. 자료 내용의 실제 검증은 각 엔진 담당자가 해야 한다.

실제 업체의 허가 증빙을 사람이 확인한 뒤 DB 운영자가 실행한다.

```sh
python -m scripts.verify_company_permit COMPANY_ID --operator REVIEWER_ID --evidence "확인한 허가 증빙의 위치와 번호" --confirm
```

검토 당시 수거 정책과 검토자를 감사 기록으로 남긴다. 일반 기업 프로필 API로 permitVerified를 올릴 수 없다. 수거 정책이 바뀌면 검증 상태가 해제된다.

## DB 검증

SQLite 로컬 저장과 Alembic 0001~0003 마이그레이션 왕복은 자동 검사했다. PostGIS는 `TRACK_D_TEST_POSTGRES_URL`에 테스트 DB를 설정한 뒤 `pytest tests/test_platform_postgis.py -q`로 검사한다. 임의 UUID 테스트 스키마 안에만 테이블을 만들고 제거한다. 실행 계정에는 PostGIS 설치·스키마 생성 권한이 필요하다. 기존 운영 DB에서 실행하지 않는다.

`.github/workflows/track-d.yml`은 PostGIS 서비스와 전체 Python 테스트, 생성 계약 불일치 검사를 수행한다. 로컬에서는 PostGIS 접속 정보가 없어 해당 테스트를 skip했다. 워크플로는 아직 푸시하지 않았으므로 원격 CI 실행 결과는 없다.
