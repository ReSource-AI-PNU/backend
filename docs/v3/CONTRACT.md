# 설계안 v3 구현 계약

여러 작업이 동시에 진행되므로, 모듈 사이의 약속을 여기에 적는다. 이 문서와 코드가 어긋나면 이 문서를 먼저 고치고 알린다.

설계안 v3 원문은 이 저장소에서 읽을 수 없는 상태에서 작성했다. 근거 ID 형식(v3 7.2), 대화 시퀀스(v3 4.3, 4.4), 도구 목록(v3 8장)은 구현 지시문을 바탕으로 여기서 정한 것이고, 원문과 다르면 맞춰 고친다.

## 1. 원칙

1. **핵심은 순수 파이썬이다.** `app/v3/` 아래 `evidence`, `judge`, `a2a`, `agents`, `intake`, `demo`, `service.py`, `store.py`는 표준 라이브러리만 쓴다. HTTP 호출은 `urllib`로 하고, 호출부는 `transport` 함수를 주입받아 테스트에서 가짜로 바꾼다.
2. **가장자리만 프레임워크를 쓴다.** `api.py`(FastAPI), `sql.py`(SQLAlchemy), `mcp_server.py`(MCP SDK), `vendor_service.py`(FastAPI)는 핵심 함수를 부르기만 한다.
3. **모델은 숫자를 만들지 않는다.** 숫자와 조항은 판단 도구와 근거 레지스트리에서만 나오고, 근거 검증기를 통과한 것만 밖으로 나간다.
4. **판단 도구는 결정적이다.** 같은 입력이면 같은 출력과 같은 calc 근거 ID가 나온다. 현재 시각, 난수, 딕셔너리 순서에 기대지 않는다. 날짜가 필요하면 `today` 인자로 받는다.
5. **조직 경계.** 근거를 읽는 모든 함수는 `org_id`를 받고, 그 조직이 볼 수 없는 근거는 돌려주지 않는다. 배출 기업끼리는 서로의 데이터를 볼 수 없다.
6. **가정값은 가정값이라고 표시한다.** 출처를 확인하지 못한 값은 `assumption` 근거로 등록하고, 근거가 없으면 "확인 필요"로 둔다.

## 2. 디렉터리와 담당

| 경로 | 내용 |
|---|---|
| `app/v3/ids.py` | 근거 ID 규칙 (작성됨) |
| `app/v3/evidence/model.py`, `registry.py` | 근거 레코드, 레지스트리 인터페이스와 메모리 구현 (작성됨) |
| `app/v3/evidence/units.py`, `validator.py`, `render.py`, `search.py`, `seeds.py`, `seeds/` | 단위 환산, 근거 검증기, 자리표시자 렌더링, 검색, 가정값·기준값·법령 적재 |
| `app/v3/judge/` | 판단 도구 ①~⑥과 도구 등록부 `toolbox.py` |
| `app/v3/a2a/message.py` | A2A 메시지와 claim (작성됨) |
| `app/v3/a2a/thread.py`, `guardrails.py`, `policy.py` | 스레드 상태와 12턴 상한, 업체 가드레일, 질문 정책과 대화 개설 조건 |
| `app/v3/agents/` | LLM 클라이언트, 회사 측 에이전트, 업체 측 에이전트, 진행기 |
| `app/v3/intake/` | 측정값 CSV, 등급 모델, 문서 추출, Prompt Shields |
| `app/v3/demo/` | 시연 데이터 (모두 가상) |
| `app/v3/store.py`, `service.py`, `devserver.py` | 저장소 인터페이스와 메모리 구현, 서비스 함수, 표준 라이브러리 HTTP 서버 |
| `app/v3/feedback.py` | 재학습 데이터 수집(7단계): 교정, 확정 뒤 실제 결과, 내보내기, 배포 게이트, 기준값 보정 후보 |
| `app/v3/eval/` | 평가(8단계): 사례 세트 `cases/*.json`, 실행기, 형식 준수 시험, Foundry 평가 입력, 리포트 |
| `app/v3/api.py`, `sql.py`, `mcp_server.py`, `vendor_service.py` | 프레임워크 가장자리 |
| `app/v3/tracing.py`, `agents/maf.py`, `judge/distance.py`, `evidence/search_azure.py` | Azure 연결부(추적, Agent Framework, 도로 거리, 색인). 설정이 있을 때만 켜진다 |
| `scripts/v3/ingest_laws.py`, `index_evidence.py`, `register_foundry_agents.py` | 법령 수집, 근거 색인, 에이전트 등록 스크립트 |
| `tests/v3/` | 테스트. 가장자리 테스트는 `pytest.importorskip`으로 시작한다 |

## 3. 근거

`app/v3/evidence/model.py`의 `Evidence`가 정본이다. JSON으로 나갈 때는 `to_dict()`를 쓴다(`asOf`, `validUntil`, `kindLabel` 포함).

종류는 `law`, `ref`, `doc`, `measure`, `model`, `human`, `calc`, `assumption`이다.

- 수치 근거는 `value`(숫자)와 `unit`을 가진다.
- 조항 근거(`law`, 그리고 원문을 가진 `doc`·`ref`)는 `excerpt`에 원문을 글자 그대로 담는다. 인용 대조는 이 원문의 부분 문자열로 한다.
- `calc` 근거의 `meta`는 `{"tool": 이름, "inputs": 입력, "formula": 계산식 설명, "inputEvidence": [근거 ID]}`를 담는다. 화면의 "계산 사슬"은 `inputEvidence`를 따라간다.
- `access`는 볼 수 있는 조직 ID 목록이고 `"*"`는 공개다.

ID는 `app/v3/ids.py`의 함수로만 만든다.

### 단위

단위 문자열은 다음만 쓴다: `원`, `원/kg`, `원/t`, `원/월`, `원/회`, `원/km`, `원/tCO2e`, `kg`, `t`, `kg/월`, `t/월`, `%`, `km`, `m`, `m3`, `kg/m3`, `tCO2e`, `kgCO2e`, `tCO2e/월`, `kgCO2e/kg`, `tCO2e/t`, `회`, `회/월`, `일`, `개`.

`원/km`(차량 운행 단가), `회`(회차 수), `원/tCO2e`(배출권 단가)는 환산 상대가 없는 단위다. 같은 단위끼리만 비교한다.

`units.convert(value, from_unit, to_unit) -> float`는 같은 차원끼리만 바꾸고(예: `원/t` ↔ `원/kg`, `t` ↔ `kg`, `tCO2e` ↔ `kgCO2e`, `kgCO2e/kg` ↔ `tCO2e/t`, `m` ↔ `km`), 못 바꾸면 `UnitError`를 낸다. `units.same_value(a, unit_a, b, unit_b) -> bool`은 환산한 뒤 상대 오차 1e-6(절대 1e-9)으로 비교한다.

## 4. 근거 검증기

```python
# app/v3/evidence/validator.py
@dataclass(frozen=True)
class Rejection:
    code: str
    message: str            # 한국어 한 문장. 에이전트에게 그대로 돌려줘 다시 쓰게 한다
    claim_id: str | None = None
    detail: dict = field(default_factory=dict)

@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    rejections: tuple[Rejection, ...] = ()
    def to_dict(self) -> dict: ...

def validate_text(text, claims, registry, *, org_id, today) -> ValidationResult
def validate_message(message, registry, *, today) -> ValidationResult   # org_id는 message.sender
```

검사와 거부 코드:

| 코드 | 조건 |
|---|---|
| `NUMBER_OUTSIDE_PLACEHOLDER` | 자리표시자 밖 본문에 숫자가 있다. 예외는 날짜(`2026-11-16`, `2026.11.16`, `2026년 11월 16일`, `11월`), 대회 단계 표기(`①`~`⑥`, `1단계`~`6단계`), 식별자(`st_`, `org_`, `th_`, `doc_`로 시작하는 토큰), 목록 번호(줄 머리의 `1.`, `2)`), 그리고 재질 이름에 든 숫자가 아니라 `ALLOWED_TOKENS`에 등록한 낱말이다 |
| `UNKNOWN_PLACEHOLDER` | 본문의 `{{x}}`에 맞는 claim이 없다 |
| `UNUSED_CLAIM` | claim이 본문에 쓰이지 않았다 |
| `CLAIM_WITHOUT_EVIDENCE` | claim의 `evidence`가 비어 있다 |
| `EVIDENCE_NOT_FOUND` | 근거 ID가 레지스트리에 없다 |
| `EVIDENCE_FORBIDDEN` | 보내는 조직이 그 근거를 볼 수 없다 |
| `EVIDENCE_EXPIRED` | 근거의 `valid_until`이 `today`보다 이르다 |
| `VALUE_MISMATCH` | number claim의 값이 근거 값과 다르다(단위 환산 뒤 비교). calc 근거도 같은 규칙이다 |
| `UNIT_MISMATCH` | 단위를 환산할 수 없다 |
| `QUOTE_MISMATCH` | clause claim의 값이 근거 `excerpt`의 부분 문자열과 글자 그대로 같지 않다 |

우회 방지: 식별자 바로 뒤에 단위가 붙은 것(`st_320원`), 월·일이 범위를 벗어난 날짜 꼴(`2026-11-99`), 단위가 따라오는 한글 수(`삼백이십 원`)는 숫자로 본다. 인용(clause)은 10자 이상이거나 근거 원문 전체여야 한다(짧은 조각으로 숫자를 흘리는 것을 막는다).

number claim에 근거가 여러 개면 첫 번째 근거가 값의 출처이고 나머지는 보조 근거다(존재와 접근만 확인한다).

`render.render_text(text, claims) -> str`은 자리표시자를 값과 단위로 바꾼 문장을 만든다(문서 내보내기용). 숫자는 천 단위 쉼표를 넣고 소수는 필요한 자리까지만 쓴다.

## 5. 판단 도구

```python
# app/v3/judge/toolbox.py
@dataclass(frozen=True)
class ToolSpec:
    name: str
    stage: int                 # 1~6
    description: str           # 한국어
    input_schema: dict         # JSON Schema
    fn: Callable[..., dict]

TOOLS: dict[str, ToolSpec]
def call_tool(name, arguments, *, registry, org_id, today) -> dict
```

모든 도구 함수는 `fn(registry, *, org_id, today, **arguments) -> dict` 꼴이고, 결과는 다음 모양이다.

```json
{
  "tool": "judge_route",
  "status": "calculated | conditional | needs_information",
  "output": { },
  "claims": [ {"id": "monthlyAfter", "kind": "number", "value": 510000, "unit": "원/월", "evidence": ["calc:..."]} ],
  "checks": [ {"key": "min_volume", "status": "met | conditional | unmet", "text": "...", "ask": "업체에 물을 질문 또는 null", "evidence": ["..."]} ],
  "evidence": ["이 호출이 새로 등록한 calc 근거 ID"]
}
```

`output`에 들어가는 숫자는 모두 `claims`에도 있어야 한다. `conditional`의 `checks[].ask`는 업체 측 에이전트에게 보낼 질문이 된다.

| 단계 | 도구 | 하는 일 |
|---|---|---|
| ① | `identify_stream` | 흐름의 재질·출처·형태를 정리하고 등급을 낸다. 측정값이 있으면 측정 등급, 없으면 사진 추정 오염 비율과 질문으로 받은 수분으로 추정하고 `"basis": "photo_estimate"`를 붙인다. 둘이 다르면 확인 질문을 `checks`에 넣는다 |
| ② | `judge_route` | 경로를 세 상태로 판정한다. 현행 처리와 단가를 입력으로 받고, 없으면 "혼합 소각" 가정값을 쓰며 현행 상태에 따른 민감도를 함께 낸다. 업체 매입 기준과 대조한다 |
| ② | `check_loading` | 최소 물량과 적재 검사. 출처(PIR·PCR)와 관계없이 모든 외부 반출 경로에 똑같이 적용한다. 같은 업체로 가는 같은 공장의 흐름은 먼저 합치고, 무게와 부피를 함께 본다 |
| ③ | `search_clauses` | 조항 검색 (law 근거) |
| ③ | `check_requirements` | 흐름과 경로에 걸리는 요건을 판정한다. 인용은 원문 부분 문자열로만 한다 |
| ③ | `match_permit` | 업체 허가 품목과 흐름을 대조한다. 맞지 않으면 대화를 열지 않는다 |
| ④ | `find_vendors` | 지역, 허가 품목, 매입 기준으로 업체를 찾고, 걸러진 업체는 이유와 함께 돌려준다 |
| ⑤ | `cost_carbon` | 도로 거리, 배출계수, KAU로 비용과 탄소를 계산한다 |
| ⑤ | `route_extra_cost` | 업체 노선에 정류장을 더할 때의 추가 운송비 (CVRP, 무게와 부피) |
| ⑥ | `optimize_assignment` | 흐름과 업체 배정. 회사 측 월 순비용을 최소화한다. 탄소는 기본 목적 함수에 넣지 않고 `carbonWeight`가 0보다 크면 가중치로 더한다. `exclude`(`[{streamId, vendorOrgId}]`)로 업체가 거절했거나 조건을 내지 않은 짝을 뺀다 |

OR-Tools를 쓸 수 있으면 CVRP와 CP-SAT를 쓰고, 없으면 같은 답을 내는 순수 파이썬 풀이(작은 문제의 완전 탐색)로 돈다. 두 경로의 결과가 같은지 테스트한다.

`route_extra_cost`와 `optimize_assignment`는 `output.solver`에 쓴 풀이를 남긴다: `"ortools" | "exact" | "greedy"`.

- OR-Tools의 답은 순수 파이썬 검사(용량, 빠진 지점, 최소 물량)로 다시 확인한 뒤에만 쓴다. 풀지 못했거나 검사를 통과하지 못하면 순수 풀이의 답을 쓴다.
- 작은 문제(정류장 8곳 이하, 배정 조합 20만 이하)는 완전 탐색이 정답이다. OR-Tools가 같은 답을 냈을 때만 `"ortools"`, 아니면 `"exact"`라고 남긴다. 어느 쪽이든 수치와 calc 근거 ID는 같다.
- 그보다 큰 문제는 OR-Tools의 답을 쓴다(노선은 탐욕 분할보다 짧을 때만. 아니면 `"greedy"`). OR-Tools가 없으면 노선은 탐욕 분할, 배정은 오류다.
- 풀이는 도구 입력으로 고를 수 없다.

등급 기준은 `app/v3/intake/grade_thresholds.v1.json` 하나다(근거 `assumption:등급기준@v1`). `identify_stream`과 `intake.grade_model`이 같은 기준을 쓴다.

기준값 키는 `judge/basis.py`가 근거로 잇는다. `kau.price`는 KAU 종가 ref 근거(단위 `원/tCO2e`)를 쓰고, `reference.price.<재질>.<형태>`는 가격조사 ref 근거를 "참고 시세"로만 보여 준다(업체 매입 단가가 아니므로 손익 계산에는 쓰지 않는다).

현행 처리 단가에서 나온 계산 근거(`currentUnitCost`, `monthlyNow`, `difference`, 민감도)는 계산한 조직만 본다.

도로 거리는 `distance_provider(points) -> km 행렬 | None`을 주입받는다. 제공자가 없으면 직선 거리에 우회 계수를 곱한 값을 `assumption` 근거로 등록하고 "가정값"으로 표시한다. `judge/distance.py`의 `AzureMapsDistance`(Route Matrix 2025-01-01)가 그 구현이고, 호출이 실패하거나 닿지 못하는 지점이 있으면 `None`을 돌려줘 가정값 경로로 돈다(일부만 도로 거리인 행렬은 만들지 않는다).

## 6. 도메인 레코드 (JSON, camelCase)

```json
Org     {"id": "org_hanbit", "name": "(주)한빛정밀", "role": "emitter | vendor | admin",
         "region": "부산 강서구 녹산산단", "location": [128.85, 35.09], "order": 1}

Stream  {"id": "st_sprue", "orgId": "org_hanbit", "name": "스프루·런너", "process": "사출",
         "wasteItem": "폐플라스틱", "material": "PP", "origin": "PIR | PCR | null", "form": "가공 전 | 압축 | 분쇄 | 펠렛 | 필름 | null",
         "lot": "...", "monthlyKg": 600, "monthlyKgBasis": "measured | estimated | null",
         "bulkDensityKgPerM3": 250,
         "measure": {"contaminationPct": 1.0, "moisturePct": 0.3, "measuredOn": "2026-11-10", "evidence": ["measure:..."]},
         "photo":   {"contaminationPct": 2.0, "evidence": ["model:..."]},
         "currentTreatment": {"method": "혼합 소각", "unitCostKrwPerKg": 250, "evidence": ["doc:..."]}}

VendorProfile {"orgId": "org_saegil",
         "permits": [{"wasteItem": "폐플라스틱", "materials": ["PP", "PE"], "evidence": ["doc:..."]}],
         "criteria": [{"id": "pc_pp_a", "material": "PP", "grade": "A", "form": "분쇄", "maxContaminationPct": 2, "maxMoisturePct": 1,
                       "unitPriceKrwPerKg": 320, "minMonthlyKg": 500, "evidence": ["doc:..."]}],
         "fleet": {"depot": [128.9, 35.1], "capacityKg": 5000, "capacityM3": 20, "costPerKm": 1200, "evidence": ["doc:..."]},
         "guardrails": {"floorPriceKrwPerKg": 250, "days": ["화", "목"], "minPickupsPerMonth": 2, "autoDecline": ["PVC 혼입"]},
         "model": {"provider": "foundry | ollama", "name": "gpt-5-mini"}}
```

`unitPriceKrwPerKg`는 배출 기업이 받는 돈이면 양수, 내는 처리비면 음수다. 화면에는 부호를 "매입"과 "처리비"로 풀어 보여 준다.

등급은 `A`, `B`, `C`, `등외`다.

## 7. 화면용 진단 뷰

`service.get_view(session_id, org_id) -> dict`가 돌려주는 모양이고, 화면은 이것만 그린다. 숫자와 조항은 모두 claim 객체로 온다.

```json
{
  "session": {"id": "ses_1", "emitterOrgId": "org_hanbit", "status": "idle | running | awaiting_answers | awaiting_approval | confirmed", "version": 3},
  "viewer": {"orgId": "org_hanbit", "role": "emitter"},
  "stages": [{"stage": 1, "key": "identify", "label": "식별·분류", "status": "waiting | running | done | needs_check"}],
  "streams": [{"id": "st_sprue", "name": "...", "material": "PP", "origin": "PIR", "form": "...",
               "monthlyKg": {"claim"}, "grade": {"value": "A", "basis": "measured | photo_estimate", "evidence": ["..."]},
               "currentTreatment": {"method": "혼합 소각", "unitCost": {"claim"}, "assumed": true}}],
  "threads": [{"id": "th_1", "vendorOrgId": "org_saegil", "vendorName": "(주)새길자원", "vendorModel": {"provider": "foundry", "name": "gpt-5-mini"},
               "status": "not_opened | open | agreed | declined | handoff | confirmed", "statusReason": "...", "reasonEvidence": ["..."],
               "streamIds": ["st_sprue"], "turns": 4, "maxTurns": 12,
               "messages": [{"id": "msg_1", "seq": 1, "from": "org_hanbit", "to": "org_saegil", "type": "inquiry", "stage": 4,
                             "streamIds": ["st_sprue"], "text": "... {{c1}} ...", "claims": [{"claim"}],
                             "validation": {"ok": true, "attempts": 1, "rejections": []}, "createdAt": "..."}]}],
  "graph": {"nodes": [{"id": "st_sprue", "type": "stream | criterion | vendor | evidence", "label": "..."}],
            "edges": [{"from": "st_sprue", "to": "pc_pp_a", "status": "met | conditional | unmet", "label": "...", "evidence": ["..."]}]},
  "result": {"status": "calculated | conditional | needs_information",
             "assignments": [{"streamId": "st_sprue", "vendorOrgId": "org_saegil", "route": "sell", "status": "calculated",
                              "unitPrice": {"claim"}, "monthlyAfter": {"claim"}, "monthlyNow": {"claim"}, "difference": {"claim"},
                              "carbon": {"claim"}, "checks": [], "alternatives": [{"vendorOrgId": "...", "route": "...", "monthlyAfter": {"claim"}, "note": "..."}]}],
             "totals": {"monthlyNow": {"claim"}, "monthlyAfter": {"claim"}, "difference": {"claim"}, "carbon": {"claim"}},
             "assumptions": ["assumption:..."], "sensitivity": [{"label": "현행이 위탁 재활용일 때", "difference": {"claim"}}]},
  "questions": [{"id": "q_1", "batch": 1, "streamId": "st_film", "field": "moisturePct", "text": "...", "answered": false}],
  "approvals": [{"threadId": "th_1", "emitter": false, "vendor": false, "blocked": false, "blockedReason": null}],
  "documents": [{"id": "doc_1", "kind": "request | plan", "title": "...", "text": "... {{c1}} ...", "claims": [{"claim"}], "validation": {"ok": true}}],
  "events": [{"seq": 1, "at": "...", "actor": "company_agent | vendor_agent | validator | tool | system", "kind": "message | tool_call | validation | handoff | approval",
              "threadId": "th_1", "summary": "...", "detail": {}}]
}
```

`{"claim"}`은 `{"id", "kind", "value", "unit", "evidence"}` 객체다. 값이 없으면 `null`을 넣고 화면은 "확인 필요"를 보여 준다.

구현하면서 더한 항목(있던 항목의 모양은 그대로다):

- `session.phase`, `session.today`
- `streams[]`: `orgId`, `process`, `wasteItem`, `lot`, `contaminants`, `monthlyKgBasis`, `contamination`·`photoContamination`·`moisture`(claim), `unknownFields`, `grade.label`. 업체가 보는 뷰에서는 `currentTreatment`가 `null`이다.
- `threads[]`: `emitterOrgId`, `assignedStreamIds`(최적안에서 이 업체에 배정된 흐름), `agreedSeq`, `vendorModel.configured`·`vendorModel.used`. `messages[]`에는 `model`과 `meta`(`asks`, `excluded`, `reason`, `terms`의 `items[{streamId, claimId}]`·`days`·`conditions`)가 붙는다. 조건의 값은 `claims`에만 있다.
- `graph.edges[]`: `kind`(`permit | criterion | assignment | excluded | regulation | document`)
- `result`: `referencePrices[{key, material, form, label, asOf, price: claim, note}]`(가격조사 참고 시세. 손익 계산에는 쓰지 않는다), `carbonInObjective`, `evidence`. `assignments[]`에는 `vendorName`, `threadId`, `carbonValue`(claim, KAU 환산), `currentAssumed`가 붙고, `checks[]`는 `{key, status, text, evidence}`에 업체가 내건 조건이면 `claims`가 붙는다(`text`의 자리표시자를 그 claims로 그린다). `alternatives[]`에는 `vendorName`, `unitPrice`(claim)가 붙는다. `sensitivity[]`는 흐름별이고 `streamId`가 붙는다.
- `questions[]`: `unknown`, `answerType`(`number | choice | text`), `unit`, `choices`
- `unresolved[{streamId, field}]`: 질문 한도를 다 쓰고도 모르는 값
- `approvals[]`: `streamIds`
- `documents[]`: `threadId`, `streamIds`, `rendered`(자리표시자를 값으로 바꾼 글), `createdAt`
- `events[].kind`: `thread`, `question`, `document`, `stage`를 더했다

재학습 데이터 수집(7단계)에서 더한 항목:

- `feedback`: `{"corrections": [...], "outcomes": [...]}`. 이 진단에 쌓인 교정과 실제 결과다. 교정은 배출 기업만 보고, 실제 결과는 그 스레드의 업체와 배출 기업만 본다.
  - 교정: `{"type": "correction", "id": 근거 ID, "sessionId", "orgId", "streamId", "field", "before", "after", "note", "seq", "target": "detector | grade", "photoEvidence": [사진 근거 ID], "recordedOn"}`
  - 실제 결과: `{"type": "outcome", "id", "sessionId", "threadId", "vendorOrgId", "emitterOrgId", "streamId", "inspectedOn", "actualGrade", "actualUnitPriceKrwPerKg", "actualKg", "note", "evidenceKind": "human | doc", "evidence": {"grade", "unitPrice", "kg"}, "features": {"material", "contaminationPct", "moisturePct", "basis"}, "criterionId", "tablePrice": claim, "offeredPrice": claim, "deviation": claim | null, "recordedOn"}`. `deviation`은 실제 단가에서 매입 기준표 단가를 뺀 값이고 calc 근거(`calc:price_deviation:...`)에 연결된다.
- `events[].kind`: `feedback`(교정이나 실제 결과를 기록함)을 더했다. 업체가 보는 뷰에는 나가지 않는다.
- `result.assignments[]`: 모르는 값 때문에 업체를 정하지 못한 흐름은 `status`가 `needs_information`이고 `checks`에 `unresolved`가 붙는다(현행 유지를 "확정"으로 보이지 않게 한다).

관리자 화면(`GET /admin/status`)의 `retrain`(재학습 표):

```json
{"gradeLabels": {"count": 0, "usable": 0, "incomplete": 0, "note": "..."},
 "outcomes": {"count": 0},
 "corrections": {"count": 0, "byField": {"material": 1}, "detectorCandidates": 0},
 "gate": {"allowed": false, "reason": "...", "minRealLabels": 30, "minAccuracy": 0.9, "realLabelCount": 0,
          "evaluated": {"model": "등급 기준값 규칙 v1", "n": 0, "accuracy": null, "label": "실제 등급 기준"}},
 "calibration": {"count": 0, "autoApply": false, "note": "...",
                 "groups": [{"streamId", "vendorOrgId", "criterionId", "count",
                             "items": [{"inspectedOn", "sessionId", "tablePrice": {"claim"}, "actualPrice": {"claim"}, "difference": {"claim"}}]}]},
 "modelVersions": [{"model", "version", "runId", "trainedOn", "dataBasis", "realLabelCount", "syntheticCount",
                    "metrics": {}, "baseline": {}, "gate": {"allowed", "reason"}, "deployed", "registeredOn"}]}
```

- `GET /admin/status`는 관리자만 부르므로 건수와 건별 목록이 모두 나간다(`feedback.retrain_table(app, viewer)`를 조직 시야로 부르면 `calibration.groups`는 그 조직이 당사자인 것만 나온다).
- `calibration`은 평균을 내지 않고 건별 목록과 건수만 준다. 기준값을 자동으로 바꾸지 않는다(`autoApply`는 늘 `false`).
- `modelVersions`는 `POST /admin/retrain/runs`로 등록된 실행만 담는다. 등록된 것이 없으면 빈 목록이다.
- `gate`는 실제 등급 행(재질·오염도·수분이 모두 있는 기록)으로 지금 기준값 규칙을 평가해 `intake.grade_model.deploy_gate`에 넣은 결과다.

## 8. HTTP API

접두어는 `/api/v3`이고, 요청하는 조직은 `X-Org-Id` 헤더로 밝힌다(시연용. 실제 인증은 기존 Entra 설정에 연결한다).

| 메서드와 경로 | 내용 |
|---|---|
| `GET /health` | `{"ok": true}`. 조직 헤더 없이 부른다 |
| `POST /demo/reset` | 시연 데이터를 처음 상태로 되돌린다. 관리자만(아래 "관리자 조직") |
| `GET /orgs` | 조직 목록. 조직 레코드의 `order`가 작은 것부터(시연 주인공 `org_hanbit`이 맨 앞, 운영 조직이 맨 뒤). `order`가 없는 조직은 그 뒤에 ID 순 |
| `GET /orgs/{orgId}/streams`, `PUT /orgs/{orgId}/streams/{streamId}` | 흐름 조회와 수정(현행 처리 입력 포함) |
| `GET /vendors/{orgId}`, `PUT /vendors/{orgId}` | 업체 프로필(허가, 매입 기준, 노선·차량, 가드레일, 모델 선택) |
| `POST /sessions` | `{emitterOrgId}`로 진단을 만든다. `emitterOrgId`가 없으면 요청한 조직이다 |
| `GET /sessions/{id}` | 진단 뷰 |
| `POST /sessions/{id}/run` | 에이전트를 다음 멈춤 지점까지 돌린다. 진단 뷰를 돌려준다 |
| `POST /sessions/{id}/answers` | `{questionId, value | "unknown"}` |
| `POST /sessions/{id}/approvals` | `{threadId}`. 요청한 조직 쪽 승인을 기록한다. 양쪽이 승인하면 confirm 메시지와 문서를 만든다 |
| `GET /evidence/{id}` | 근거 하나와 계산 사슬 `{evidence, chain: [evidence]}` |
| `GET /evidence?q=&kind=` | 근거 검색 |
| `GET /intake/csv/template`, `POST /intake/csv` | 측정값 CSV 양식과 업로드 |
| `POST /intake/documents` | 문서 업로드와 추출 |
| `POST /intake/documents/confirm` | `{orgId, documentName, fields}`. 담당자가 확인한 추출값(`confirmed: true`)만 doc 근거로 등록한다. 응답은 `{"evidence": [근거 ID]}` |
| `GET /admin/status`, `POST /admin/eval` | 색인·기준값 상태와 재학습 표, 사례 시험 실행(관리자만) |
| `POST /sessions/{id}/corrections` | `{streamId, field, before, after, note}`. 배출 기업 담당자가 고친 값을 기록한다 |
| `POST /sessions/{id}/outcomes` | `{threadId, streamId, actualGrade, actualUnitPriceKrwPerKg, actualKg, inspectedOn, note, documentName?}`. 업체가 확정 뒤 실제 결과를 기록한다 |
| `GET /admin/retrain/export`, `POST /admin/retrain/runs` | 재학습 행 내보내기, 재학습 실행 결과 등록(관리자만) |

오류는 `{"error": {"code": "...", "message": "한국어 문장"}}`과 맞는 상태 코드로 낸다.

요청을 읽다가 나는 오류는 두 서버(`api.py`, `devserver.py`)가 `app/v3/httpio.py`의 같은 함수로 낸다. 코드와 상태 코드가 서버에 따라 달라지지 않는다.

| 코드 | 상태 | 조건 |
|---|---|---|
| `INVALID_JSON` | 400 | 본문이 JSON이 아니다 |
| `INVALID_BODY` | 422 | 본문이 JSON 객체가 아니거나 필수 항목(`questionId`·`value`, `threadId` 등)이 없다 |
| `FILE_REQUIRED` | 422 | 올릴 파일이 없거나 비어 있다 |
| `TOO_LARGE` | 413 | 본문이 20MB를 넘는다 |
| `ORG_REQUIRED`, `UNKNOWN_ORG` | 401 | `X-Org-Id` 헤더가 없거나 모르는 조직이다 |
| `FORBIDDEN`, `WRONG_ROLE` | 403 | 그 조직이 할 수 없는 일이다 |
| `*_NOT_FOUND` | 404 | 없거나 볼 수 없다(볼 수 없는 것은 있는지도 알려 주지 않는다) |
| `INVALID_VALUE`, `INVALID_KIND`, `INVALID_ANSWER` | 422 | 값이 틀렸다 |
| `NOT_AGREED`, `APPROVAL_BLOCKED`, `THREAD_NOT_CONFIRMED` | 409 | 지금 상태에서는 할 수 없다 |

이전에 `api.py`만 쓰던 `INVALID_REQUEST`(400)와 `FILE_TOO_LARGE`는 없어졌다. 업체 측 에이전트 서비스(`vendor_service.py`)도 같은 코드를 쓴다.

관리자 조직:

- 시연 데이터에 운영 조직 `org_admin`(`"Re:Source AI 운영"`, `role: "admin"`)이 있다. 흐름도 업체 프로필도 없다.
- `/admin/*`(`GET /admin/status`, `POST /admin/eval`, `GET /admin/retrain/export`, `POST /admin/retrain/runs`)는 `X-Org-Id`가 관리자 조직일 때만 답한다. 다른 조직은 403 `FORBIDDEN`, 헤더가 없으면 401 `ORG_REQUIRED`.
- `POST /demo/reset`도 관리자만 부른다. 예외 두 가지: `App(demo_open=True)`이면 누구나 부른다(`devserver`의 기본값. `--demo-closed`로 끈다. `api.py`는 설정 `V3_DEMO_OPEN`, 기본 `false`). 그리고 조직이 하나도 없는 빈 저장소의 첫 시드는 막지 않는다(관리자 조직이 아직 없다).
- 배출 기업과 업체는 관리자 화면을 볼 수 없다. 화면에서 조직을 `org_admin`으로 바꿔야 한다.

재학습 데이터 수집(7단계) 주소의 규칙:

- `POST /sessions/{id}/corrections`: 그 진단의 배출 기업만 부를 수 있다(업체는 403, 당사자가 아니면 404). `field`는 `box | material | origin | form | contaminant | grade | contaminationPct | moisturePct`다. human 근거(`human:<조직>:교정-<흐름>-<항목>-<순번>`)로 등록하고, 앞의 다섯 항목은 탐지 모델 재학습 데이터 후보가 된다. 기록만 남기며 흐름의 값은 바꾸지 않는다(흐름은 `PUT /orgs/{orgId}/streams/{streamId}`로 고친다). 응답은 `{"correction": 기록, "evidence": [근거 ID]}`.
- `POST /sessions/{id}/outcomes`: 그 스레드의 업체만, 스레드가 `confirmed`일 때만 받는다(아니면 `THREAD_NOT_CONFIRMED` 409). `streamId`는 그 스레드에서 확정된 흐름이어야 하고, 실제 등급이나 실제 단가 가운데 하나는 있어야 한다. `documentName`을 주면 doc 근거, 없으면 human 근거로 등록한다. 소유는 업체이고 배출 기업과 나눈다. 같은 흐름과 검수일로 다시 보내면 같은 근거의 새 판이 된다. 응답은 `{"outcome": 기록, "evidence": [근거 ID]}`.
- `GET /admin/retrain/export`: `{"gradeLabels": [등급 모델 학습 행, synthetic=false], "detectionCorrections": [탐지 교정], "gate": 게이트}`. 여러 조직의 기록이 섞여 있어 `role`이 `admin`인 조직만 받는다.
- `POST /admin/retrain/runs`: Azure ML 재학습 작업(`scripts/v3/ml/compare_and_gate.py`)이 낸 `gate.json`을 그대로 받는다. 받은 값을 적을 뿐 점수나 판정을 만들지 않는다. 관리자만.
- `POST /admin/eval`의 응답: 맨 위(`scenario`, `sessionStatus`, `checked`, `passed`, `validatorPassRate`, `rewritten`, `threads`, `resultStatus`)는 시연 시나리오 한 건의 결과이고, `cases`가 사례 세트(50건)의 결과다: `{"model", "summary": {caseCount, draftCount, reviewCounts, verdictMatched, verdictMatchRate, openCount, openCases, validatorChecked, validatorPassed, validatorPassRate, forbiddenViolationCount, forbiddenClean, ...}, "results": [{id, title, review, open, verdictMatch, mismatches, forbiddenViolations, validator}]}`.

업로드는 `multipart/form-data`의 `file`, `orgId`, (문서는) `kind`로 받는다. `kind`는 `permit | purchase_criteria | material_spec | msds | contract`다.

## 9. 작업 규칙

- 커밋 작성자는 `Jeonsubb <glvlglvl01@naver.com>`이다. 커밋 메시지는 한국어로 쓰고 `feat:`, `fix:`, `test:`, `docs:` 접두어를 붙인다.
- 로컬에서는 네이티브 확장을 쓸 수 없다. 핵심 모듈과 그 테스트는 표준 라이브러리와 pytest만으로 돌아야 한다.
- 테스트 실행: `PYTHONPATH=<pydeps>:. python3 -m pytest -q tests/v3` 와 `python3 -m ruff check app/v3 tests/v3` (줄 길이 100).
- 자기 담당 경로 밖의 파일은 고치지 않는다. 계약을 바꿔야 하면 이 문서에 적고 보고한다.
- 비밀값을 코드, 테스트, 문서에 넣지 않는다. 시연 데이터의 회사와 수치는 모두 가상이라고 표시한다.
- 화면과 메시지 문구는 한국어로 쓴다.
