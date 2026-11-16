# 한 번도 실행하지 못한 코드의 정적 검토

`app/v3`의 가장자리(FastAPI, SQLAlchemy, Alembic, MCP SDK, OR-Tools, 배포 파일)는 작성한 환경에서 돌려 보지 못했다. GitHub Actions CI에서 처음 돌 때 깨질 곳을 미리 찾아 고친 결과다. 검토 브랜치는 `v3/static`이다.

**이 검토도 그 코드를 실제로 실행하지는 못했다.** 아래 "확인한 것"은 읽기와 대조로 확인한 것이고, CI에서 통과한다는 뜻이 아니다.

## 방법

- **고정된 버전의 소스와 대조.** `uv.lock`이 고정한 버전의 휠을 PyPI에서 받아 풀기만 하고(설치·import 없음) 소스를 읽었다: fastapi 0.162.0, starlette 1.10.0, sqlalchemy 2.1.9, alembic 1.20.1, mcp 1.30.0, ortools 9.15.6755, pydantic-settings 2.16.2. 코드가 부르는 이름과 인자를 이 소스에서 찾았다.
- **공식 문서.** ACR Tasks YAML 참조(Microsoft Learn)의 `workingDirectory` 기본값, Bicep 변수에서 `list*` 함수를 쓰는 방식.
- **가짜 모듈로 순수 로직 실행**(저장소 밖 임시 폴더, 커밋하지 않음).
  - fastapi·starlette·pydantic-settings의 가짜(경로 표, 요청 객체, 응답 객체만 흉내 낸다)로 `tests/v3/test_api.py`와 `test_vendor_service.py`를 돌렸다: 21개 통과. 처리 함수의 로직과 테스트의 기대값이 서비스 동작과 맞는다는 것만 보여 준다. 프레임워크의 실제 동작(서명 해석, 경로 맞추기, multipart 읽기)은 보여 주지 않는다.
  - 전수 탐색으로 푸는 가짜 CP-SAT로 `cp_sat_pick`을 돌렸다: `test_cp_sat_matches_exhaustive_search` 통과, CP-SAT 경로를 켠 채 `tests/v3` 전체 통과. 모델(제약, 목적, 동점 정리)이 완전 탐색과 같은 답을 낸다는 것만 보여 준다.
- **ast 대조.** `tests/v3/test_static_review.py`(13개)로 남겼다.
- **CI와 같은 Python 3.12로 실행.** 이 PC에 있는 Python 3.12로 `tests/v3`를 돌렸다(로컬 기본은 3.13이다). 결과는 맨 아래.

## 고친 것

| 파일 | 무엇이 왜 틀렸나 | 고친 방법 |
|---|---|---|
| `uv.lock` | `pyproject.toml`에 더한 `sqlalchemy`, `alembic`, `psycopg`, `ortools`와 v3 extra의 `mcp`가 lock에 없었다(병합 커밋에 "다음 커밋에서 다시 만든다"고만 적혀 있었다). CI의 `uv sync --extra dev --extra v3 --locked`는 lock이 맞지 않으면 설치에서 멈춘다. 테스트가 하나도 돌지 않는다 | `uv lock`으로 다시 만들었다(해석만 하고 설치하지 않았다). 기존 고정 버전은 그대로이고 23개가 더해졌다 |
| `app/v3/api.py` | starlette는 읽을 수 없는 multipart(경계 없음 등)를 `HTTPException(400)`으로 알리는데, `guarded`의 마지막 `except Exception`이 이것을 삼켜 500 `INTERNAL`로 답했다. `devserver`는 같은 요청에 400 `INVALID_MULTIPART`다 | `read_upload`에서 받아 400 `INVALID_MULTIPART`로 답한다. 계약 8장의 표도 맞췄다 |
| `infra/deploy.sh` | ACR 작업의 단계는 이미지의 `WORKDIR`가 아니라 `/workspace`에서 돈다. `alembic upgrade head`가 `alembic.ini`와 `app` 패키지(`/app`)를 찾지 못해 마이그레이션이 실패하고 배포가 멈춘다 | 단계에 `workingDirectory: /app`을 더했다 |
| `infra/appservice.bicep`, `infra/deploy.sh` | API 앱 설정에 `V3_AUTH`, `V3_DEMO_OPEN`, `V3_ORG_TOKENS` 자리가 없어 token 모드로 배포할 방법이 없었다 | 매개변수 `v3Auth`(기본 `header`, README의 기본값과 같다), `v3DemoOpen`(기본 `false`)과 Key Vault 참조(`v3-org-tokens`)를 더했다. token일 때만 참조를 넣는다. `deploy.sh`는 환경 변수 `V3_AUTH`, `V3_DEMO_OPEN`을 받고, token인데 비밀이 없으면 배포 전에 멈춘다. Container Apps 경로에도 같은 설정을 넣었다 |

## 파일별 결과

### `app/v3/api.py`, `app/main.py`, `app/core/config.py`

확인한 것:

- `@router.get(...)`이 바깥, `@guarded`가 안쪽이다. `guarded`는 `functools.wraps`를 쓰고 `async def`다. fastapi 0.162.0의 `get_typed_signature`는 `inspect.signature`(`__wrapped__`를 따라간다)와 `inspect.unwrap`을 쓰므로 원래 서명(경로 변수와 `request: Request`)을 읽는다.
- 처리 함수의 매개변수는 경로 변수와 `request`뿐이고 반환 주석이 없다(질의 매개변수나 응답 모델로 해석될 것이 없다). 본문은 `request.body()`로 직접 읽으므로 FastAPI의 422 검증 응답이 나갈 길이 없다.
- `/evidence`가 `/evidence/{evidence_id:path}`보다 먼저 등록된다.
- `APIRouter`에 예외 처리기를 붙이지 않았다(붙일 수 없다). 오류 변환은 `guarded`가 한다.
- `request.form()`, `UploadFile.filename`·`read()`, `FormData.get`·`items`·`values`, `query_params.getlist`, `Response(content, media_type, headers)`가 starlette 1.10.0에 그대로 있다.
- 서비스 함수는 `run_in_threadpool`로 부르고 메모리 저장소일 때는 잠금 하나로 줄 세운다. 앱 상태는 모듈 전역에 처음 요청 때 만든다(lifespan을 쓰지 않는다).
- 설정 이름: 필드 이름이 곧 환경 변수 이름이다(별칭 없음). `v3_vendor_agents`만 JSON으로 읽는 사전이고 `v3_org_tokens`는 글 그대로 받아 `httpio.org_tokens`가 읽는다. `.env.example`과 bicep에 사전·목록 설정을 빈 문자열로 주는 곳은 없다(빈 문자열이면 pydantic-settings가 JSON으로 읽다 실패한다).
- 가짜 모듈로 `test_api.py` 16개가 통과했다(위 "방법").

CI에서 깨질 가능성이 남은 것:

- 실제 `TestClient`로는 돌려 보지 못했다. 가짜가 흉내 내지 못한 것: URL로 부호화한 근거 ID(`:`, `@`, 한글)를 `:path` 변환기가 받는 방식, 한글 질의 값, `Content-Type` 없는 본문 업로드.
- fastapi 0.162.0에는 내장 텔레메트리(`fastapi.telemetry`, `opentelemetry-api` 의존)가 있다. `OTEL_*` 환경 변수가 없으면 내보낼 곳이 없는 것으로 보이지만(일부만 읽었다) `app/v3/tracing.py`와 함께 켰을 때의 동작은 확인하지 못했다.
- 고치지 않고 적어 두는 것: `current_auth()`가 요청마다 토큰 JSON을 다시 읽는다(느릴 뿐 틀리지 않다). 올린 파일(`UploadFile`)을 닫지 않는다. multipart의 파일이 아닌 칸은 starlette 기본 상한(1MB)을 따른다.

### `tests/v3/test_api.py`, `test_vendor_service.py`, `test_sql.py`, `test_mcp_server.py`

확인한 것:

- import 경로와 `importorskip` 대상이 CI 설치(`--extra dev --extra v3`)와 맞는다. `test_mcp_server.py`는 v3 extra가 있어야 돌고, `track-d.yml`(`--extra dev`만)에서는 건너뛴다.
- `test_api.py`·`test_vendor_service.py`의 기대값은 가짜 모듈로 돌려 서비스 동작과 맞는 것을 확인했다.
- `test_mcp_server.py`가 보내는 `search_clauses` 입력은 toolbox의 스키마에 맞고, 도구 10개의 스키마에 JSON Schema로 틀린 모양은 없다(SDK가 호출 전에 `jsonschema.validate`를 한다. 스키마가 틀리면 `isError`가 된다).

CI에서 깨질 가능성이 남은 것:

- `test_sql.py`는 가짜로도 돌려 보지 못했다(읽기만 했다).
- `test_mcp_server.py`의 streamable HTTP 한 바퀴: 이름과 인자는 맞지만 `TestClient` 안에서 `StreamableHTTPSessionManager.run()`(anyio 작업 묶음)과 요청이 함께 도는지는 실행해야 안다.

### `app/v3/sql.py`, `migrations/versions/0004_v3.py`, `migrations/env.py`

확인한 것:

- `down_revision = "0003_deliveries"`가 0003 파일의 `revision`과 같다. 사슬이 0001부터 끊기지 않는다.
- 모델 세 개와 마이그레이션이 같은 테이블·열·기본 키를 만든다. `ix_v3_evidence_kind`는 모델의 `index=True`가 만드는 이름과 같다.
- sqlalchemy 2.1.9의 `Session.get(entity, ident, *, with_for_update=...)`: 복합 키를 사전(`{"kind", "id"}`)으로 받고, `with_for_update=False`는 `None`과 같다(`ForUpdateArg._from_argument`). SQLite는 `FOR UPDATE`를 빼고 낸다.
- JSON 열을 제자리에서 고치는 곳이 없다. 모든 쓰기가 새 값을 대입한다(`_write`, `setattr`). 읽은 값은 세션이 닫히기 전에 복사한다.
- 호출 하나가 트랜잭션 하나이고 `with` 블록을 빠져나올 때 commit한다(블록 안의 `return`도 commit을 거친다). 예외면 rollback한다.
- `from __future__ import annotations` 아래의 `Mapped[list]`: 주석은 모듈 이름 공간에서 풀리므로 `SqlRegistry.list` 메서드와 부딪히지 않는다.
- `tests/test_platform.py`의 `Base.metadata.create_all`: pytest가 수집 단계에서 `tests/v3/test_sql.py`를 import하므로 v3 테이블이 함께 만들어진다. 해가 없다. `test_migrations_upgrade_downgrade`는 이제 0004까지 SQLite에서 올리고 내린다(확장 만들기는 PostgreSQL일 때만 한다).

CI에서 깨질 가능성이 남은 것:

- **sqlalchemy가 2.1.9로 설치된다**(`pyproject`의 범위는 `>=2.0,<3`). `sql.py`와 `app/platform`은 2.0을 보고 쓴 코드다. 쓰는 이름은 2.1.9 소스에서 확인했지만 2.1에서 달라진 동작이 있는지는 실행해야 안다. 깨지면 `sqlalchemy>=2.0,<2.1`로 좁히고 lock을 다시 만드는 것이 가장 빠르다.
- `track-d.yml`의 PostGIS 테스트는 0004를 PostgreSQL에서 처음 올린다. `pg_available_extensions`로 `vector`가 없으면 건너뛰게 돼 있다(읽기만 했다).
- 고치지 않고 적어 두는 것: `next_id`와 `add_event`는 같은 키의 첫 저장이 동시에 일어나면 기본 키 충돌로 한쪽이 실패한다(잠글 행이 아직 없다). 시연 규모에서는 `api.py`가 줄 세우지 않는 SQL 저장일 때만 해당한다.

### `track-d.yml`의 `scripts.export_openapi` 검사

- **고치지 못했다. `track-d.yml`이 돌면 이 단계에서 실패한다.** `docs/openapi.json`에는 `/api/v3` 경로가 하나도 없고, v3와 무관하게 v1 경로 8개(`/api/v1/classify`, `/classify/answer`, `/c/questions/*` 4개, `/feasibility/preview`, `/models`)도 없다. 병합 뒤 다시 만든 적이 없다는 뜻이다. FastAPI를 불러올 수 있는 곳에서 `uv run python -m scripts.export_openapi`로 다시 만들어 커밋해야 한다. 손으로 쓰면 글자 단위 비교를 통과한다고 장담할 수 없어 만들지 않았다.
- v3 변경은 이 워크플로의 경로 조건(`migrations/**`, `scripts/**`, `pyproject.toml`, `uv.lock`)에 걸리므로 PR에서 돈다.

### `app/v3/vendor_service.py`, `app/v3/mcp_server.py`

확인한 것(mcp 1.30.0 소스):

- `mcp.server.lowlevel.Server`의 `@server.list_tools()`, `@server.call_tool()`이 있다. `call_tool` 처리기는 `(name, arguments)`를 받고, 목록을 돌려주면 `content`가 되고, 예외는 `isError: true` 결과가 된다.
- `StreamableHTTPSessionManager(app=, event_store=, json_response=, stateless=)`, `run()`, `handle_request(scope, receive, send)`가 있다. `stateless=True`면 요청마다 새 전송을 만들고 초기화 없이 `tools/list`를 받는다(`ServerSession`이 초기화된 상태로 시작한다).
- `security_settings`를 주지 않으면 DNS 리바인딩 검사가 꺼진다(`TestClient`의 `testserver` 호스트가 막히지 않는다).
- starlette 1.10.0의 `Route`는 함수가 아닌 끝점(`__call__`이 있는 객체)을 ASGI 앱으로 다루고 메서드를 가리지 않는다.
- `vendor_service.py`는 `api.py`의 `guarded`, `call`, `json_object`를 그대로 쓴다. 가짜 모듈로 테스트 5개가 통과했다.

### `app/v3/judge`의 OR-Tools 경로

확인한 것(ortools 9.15.6755 소스):

- CP-SAT: `CpModel.new_bool_var`, `add`, `maximize`, `minimize`, `CpSolver.solve`, `value`, `parameters.num_search_workers`, `cp_model.OPTIMAL`이 있다. `add`는 참거짓도 받는다(식이 상수로 접힐 때).
- 경로 풀이: `pywrapcp.RoutingIndexManager`, `RoutingModel`, `RegisterTransitCallback`, `RegisterUnaryTransitCallback`, `SetArcCostEvaluatorOfAllVehicles`, `AddDimensionWithVehicleCapacity`, `DefaultRoutingSearchParameters`, `SolveWithParameters`, `Start`, `IsEnd`, `NextVar`, `IndexToNode`, `routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC`가 `ortools.constraint_solver`에 있다.
- 가짜 CP-SAT(전수 탐색)로 배정 모델이 완전 탐색과 같은 답을 낸다.
- `test_ortools_cvrp_matches_exact_search`의 다섯 입력은 최단 거리 해의 회차 수가 하나뿐이다(전수로 세어 봤다). OR-Tools가 최단 거리를 찾으면 회차 수도 맞는다.

CI에서 깨질 가능성이 남은 것:

- **`test_ortools_cvrp_matches_exact_search`**: OR-Tools 경로 풀이는 지역 탐색이라 최적해를 보장하지 않는다. 다섯 입력(지점 3~7개) 가운데 하나라도 최적이 아닌 답을 내면 `solve_trips`가 `"exact"`를 남기고 테스트가 실패한다. 수치는 어느 쪽이든 같다(완전 탐색의 답을 쓴다).
- 실제 CP-SAT는 돌려 보지 못했다. 식 연산(`int * 변수`, `sum`, `>=`)이 9.15의 `LinearExpr`에서 그대로 되는지는 형 정보(`.pyi`)로만 확인했다. 예외가 나면 완전 탐색으로 돌아가 `"exact"`가 되고 `test_cp_sat_matches_exhaustive_search`가 실패한다.
- ortools가 들어오면서 `route_extra_cost`가 호출마다 실제 경로 풀이를 돈다(시간 상한 1초, 메타휴리스틱 없음). 보통은 곧 끝나지만 테스트 전체 시간이 늘 수 있다(CI 상한 10분).

### `pyproject.toml`, `ci.yml`, `track-d.yml`, `Dockerfile`, `scripts/v3/start.sh`

확인한 것:

- `ci.yml`: `uv sync --extra dev --extra v3 --locked` → `ruff check .` → `pytest -q`(전체). `track-d.yml`: `--frozen --extra dev`(mcp 없음) → 플랫폼 범위 ruff → `pytest -q`(전체, PostGIS 포함) → openapi 검사.
- CI의 ruff는 lock의 0.17.3이고 로컬과 같은 버전이다.
- `start.sh`의 역할(api, vendor-agent, mcp, migrate)이 실제 모듈 경로와 맞다. `alembic.ini`와 `migrations`를 이미지에 복사한다.
- ortools가 numpy와 pandas를 기본 의존성으로 끌고 온다. v3 코드와 테스트에 "numpy가 없을 때"에 기대는 곳은 없다.

CI에서 깨질 가능성이 남은 것:

- `Dockerfile`은 lock을 쓰지 않고 `pip install ".[v3,ml]"`로 그때의 최신 버전을 받는다. CI 테스트가 본 버전과 이미지의 버전이 다를 수 있다. `pyproject.toml`만 복사한 상태에서 설치하는 방식은 원래 Dockerfile과 같다.

### `infra/deploy.sh`, `infra/appservice.bicep`, `.github/workflows/deploy.yml`

확인한 것(실행하지 못했다. `bash -n`과 문서 대조만):

- ACR 작업 YAML의 `cmd`, `env`, `timeout`, `workingDirectory`, `az acr run -f … --set … --set-secret … /dev/null`.
- Bicep: `existing` 리소스, 조건부 리소스와 `!` 접근, `concat`, `list*`를 쓴 변수(Bicep이 쓰는 자리에 풀어 넣는다), `accessPolicies`의 `name: 'add'`.
- `deploy.yml`: `workflow_run`(CI 성공, push일 때만), OIDC 로그인, `deploy.sh`가 읽는 환경 변수 이름(`RG`, `PLATFORM`, `MIGRATE_MODE`).

남은 것:

- 전부 미실행이다. 처음 쓸 때 `az deployment group what-if`로 확인한다.
- token 모드로 배포하려면 Key Vault 비밀 `v3-org-tokens`를 먼저 넣어야 한다. 기본은 header 모드(시연 전용)이고 공개 주소에는 맞지 않는다.
- `deploy.sh`가 `ollamaBaseUrl=`처럼 빈 값을 `-p`로 넘긴다. az가 빈 문자열로 받는지는 확인하지 못했다.

### `app/v3/tracing.py`, `app/v3/agents/maf.py`

- 선택 패키지는 함수 안에서만 import한다. 모듈을 불러오는 것만으로는 실패하지 않는다.
- CI에는 `opentelemetry-api`만 있고(fastapi가 끌고 온다) `azure-monitor-opentelemetry`는 없다. `tracing.available()`은 둘 다 있어야 참이므로 꺼진 채로 돈다. `agent_framework`도 없어 `maf` 경로는 꺼진다.

## CI에서 깨질 가능성이 남은 것 (가능성 높은 순)

1. `track-d.yml`의 `docs/openapi.json` 비교. 지금 상태로는 반드시 실패한다(위 참조).
2. sqlalchemy 2.1.9에서 `test_sql.py`와 `tests/test_platform*.py`. 한 번도 실행하지 못한 조합이다.
3. `test_ortools_cvrp_matches_exact_search`: OR-Tools가 다섯 입력 모두에서 최적해를 내야 통과한다.
4. `test_mcp_server.py`의 streamable HTTP 두 테스트: `TestClient`와 세션 관리자의 수명 주기.
5. `test_api.py`의 실제 프레임워크 동작(부호화한 근거 ID 경로, fastapi 0.162.0의 내장 텔레메트리).
6. `test_cp_sat_matches_exhaustive_search`: 실제 CP-SAT의 식 연산.
7. `docker build` 작업: lock 없이 최신 버전을 설치한다.

## 실행 결과

로컬(네이티브 확장 없음. fastapi·sqlalchemy·mcp·ortools 테스트 10개는 건너뛴다):

- `PYTHONPATH=<pydeps>:. python3 -m pytest -q tests/v3` (Python 3.13): 867 passed, 10 skipped. 검토 전 854에서 `test_static_review.py` 13개가 늘었다.
- 같은 명령을 Python 3.12(CI와 같은 버전)로: 867 passed, 10 skipped.
- `ruff check .`: 통과. `ruff format --check app/v3 tests/v3 scripts/v3`: 통과. `py_compile`(app, tests, scripts, migrations): 통과.
- 건너뛴 10개가 CI에서 처음 도는 테스트다: `test_api.py`, `test_vendor_service.py`, `test_sql.py`, `test_mcp_server.py`(모듈 단위 4개), `test_judge_solvers.py`의 OR-Tools 비교 3개, `test_tracing.py`·`test_maf.py`의 선택 패키지 3개(이 3개는 CI에서도 건너뛴다).
