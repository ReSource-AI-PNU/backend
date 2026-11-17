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
| `app/v3/a2a/term_words.py` | 숫자 없이 조건을 뜻하는 낱말 표(상수). `guardrails.check_term_words`가 쓴다 |
| `app/v3/textutil.py` | 한국어 조사(`josa`, `particle`). 코드가 만드는 문장에서 이름 뒤 조사를 받침에 맞게 붙인다 |
| `app/v3/httpio.py` | 두 서버가 같이 쓰는 요청 읽기, 요청 오류, 요청한 조직 정하기(`Auth`) |
| `app/v3/agents/` | LLM 클라이언트, 회사 측 에이전트, 업체 측 에이전트, 진행기 |
| `app/v3/intake/` | 측정값 CSV, 등급 모델, 문서 추출, Prompt Shields, 사진·영상 탐지(`detector.py`) |
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
- 합계 계산 근거(`calc:optimize_assignment:…:totalNow`, `totalAfter`, `totalDifference`, `totalCarbon`)의 `inputEvidence`는 흐름별 계산 근거(`s0MonthlyNow` 등)다. 사슬이 합계 → 흐름별 계산 → 원 근거(문서, 가정값)로 이어진다. 합계 claim의 `evidence`도 `[합계 근거, 흐름별 근거…]`다.
- **근거 목록에는 같은 ID가 두 번 실리지 않는다.** claim의 `evidence`, 그래프 엣지와 검사의 `evidence`는 처음 나온 순서를 지킨 채 중복을 뺀다. number claim의 첫 번째 근거가 값의 출처라는 규칙은 그대로다.
- `access`는 볼 수 있는 조직 ID 목록이고 `"*"`는 공개다. 조직에게 근거를 내보낼 때(`search.get_evidence`, `search_evidence`, `evidence_chain`)는 이 목록에서 보는 조직과 근거를 가진 조직(`meta.ownerOrgId`)만 남긴다. 업체의 매입 기준 근거는 그 업체와 대화한 배출 기업 모두에게 열려 있어, 목록을 그대로 내면 한 배출 기업이 같은 업체와 거래하는 다른 배출 기업을 알게 된다.
- 판단 도구는 호출마다 호출 기록을 calc 근거로 남긴다: `calc:<도구>:<입력 지문>:call`. 값은 없고 `excerpt`에 판정과 검사 결과, `meta`에 `{"tool", "inputs", "status", "checks": [{key, status}], "inputEvidence", "call": true}`가 있다. 숫자를 내지 않는 호출(`match_permit`, `check_requirements`)도 근거 저장소에 남는다. 도구 결과의 `evidence` 마지막 항목이 이 기록이다.
- **상대 조직과 나누는 calc 근거에는 한쪽만의 값이 없다.** 판단 도구가 대화 상대에게도 여는 근거(`access`에 부른 조직 밖의 조직이 있다)는 `meta.inputs`와 ID의 입력 지문을 한쪽만의 값을 뺀 입력(`judge.common.shareable`)으로 만든다. 빼는 값은 흐름의 배출 기업만의 항목(`currentTreatment`, `reuse`, `pretreatHoursPerMonth`, `laborCostKrwPerHour`, `pretreatEvidence`), 현행 처리비 답(`answers.currentCost`, `unknownFields`의 `currentCost`), 업체의 차량 운행 단가(`fleet.costPerKm`)다. 그래서 나누는 근거의 ID는 상대가 모르는 값에 따라 달라지지 않는다(ID로 값을 맞혀 볼 수 없다).
  - 그런 값에서 나온 수치(`currentUnitCost`, `monthlyNow`, `difference`, 민감도, `carbonDifference`, `kauValue`, `pretreatHours`, `laborRate`, `monthlyLabor`, 인건비를 뺀 `cost_carbon`의 `monthlyAfter`)는 부른 조직만 보는 근거이고, 입력 원본과 그 지문을 쓴다.
  - 나누는 호출 기록에는 현행 처리와 탄소에 관한 검사(`current_cost`, `current_assumed`, `current_method`, `carbon`, `kau`)와 부른 조직만 보는 계산 근거의 ID를 싣지 않는다. 도구 결과(`checks`, `evidence`)에는 그대로 있다.
  - 근거를 조직에게 내보낼 때(`search.shown`을 거치는 조회, 검색, 계산 사슬)는 계산한 조직(`meta.inputs.orgId`)이 아니면 `meta.inputs`를 `{"hidden": true}`로 가린다. 이 규칙 전에 저장된 근거도 가려진다.

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

**본문 밖 문장도 검사한다.** `validate_message`는 메시지에 함께 실려 화면과 문서로 나가는 문장도 본문과 같은 규칙으로 본다(`validate_notes`, 문장 목록은 `Message.free_texts()`): 닫는 이유(`meta.reason`), 받지 않는 흐름의 이유(`meta.excluded[]`), 조건 문장과 요일(`meta.terms.conditions`, `meta.terms.days`). 자리표시자 밖 숫자가 있으면 `NUMBER_OUTSIDE_PLACEHOLDER`, 이 메시지의 claims에 없는 자리표시자가 있으면 `UNKNOWN_PLACEHOLDER`이고 `detail.where`에 자리가 적힌다.

**거부 기록에는 값이 없다.** 메시지의 `validation.rejections`와 실행 기록의 `detail.rejections`는 `{"code", "message": 코드별 일반 문장, "claimId": null, "detail": {}, "attempt"}`다(`agents/draft.py`의 `public_rejections`). 거부된 초안에 들어 있던 숫자·조항·claim ID, 가드레일 수치는 싣지 않는다. 자세한 사유(`Rejection.message`, `detail`)는 다시 쓰는 모델에게만 돌려준다. 회사 측과 업체 측이 같다.

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

결과 한 장의 `checks`에는 판단 도구의 세 상태 말고 `needs_information`도 온다. 배정이 빠졌거나(`handoff`), 모르는 값이 남았거나(`unresolved`), 검사를 건너뛴(`skipped`) 항목이고 화면은 "확인 필요"로 그린다.

`output`에 들어가는 숫자는 모두 `claims`에도 있어야 한다. `conditional`의 `checks[].ask`는 업체 측 에이전트에게 보낼 질문이 된다.

| 단계 | 도구 | 하는 일 |
|---|---|---|
| ① | `identify_stream` | 흐름의 재질·출처·형태를 정리하고 등급을 낸다. 측정값이 있으면 측정 등급, 없으면 사진 추정 오염 비율과 질문으로 받은 수분으로 추정하고 `"basis": "photo_estimate"`를 붙인다. 둘이 다르면 확인 질문을 `checks`에 넣는다 |
| ② | `judge_route` | 경로를 세 상태로 판정한다. 현행 처리와 단가를 입력으로 받고, 없으면 "혼합 소각" 가정값을 쓰며 현행 상태에 따른 민감도를 함께 낸다. 업체 매입 기준과 대조한다. 오염도가 사진 추정이거나 수분이 담당자 답이면 상한 안이어도 확정하지 않고 `contamination_basis`·`moisture_basis` 검사(조건부, "반입 검수에서 확인")를 붙인다 |
| ② | `judge_reuse` | 경로 후보 가운데 공정 재투입. 입력은 `stream` 하나다. 공정에서 나온 것(PIR)이고 단일 재질이며 혼입물이 없을 때만 후보(`output.eligible: true`)로 본다. 측정 등급이 A가 아니면 후보에서 빼고(`eligible: false`), 오염도와 수분의 측정값이 없으면 후보이되 `reuse_quality`가 `conditional`이다. 신재 구입 단가와 다시 넣을 수 있는 양(`stream.reuse`)이 있으면 절감액을 계산하고(`reuseKg`, `monthlyRevenue`, `monthlyAfter`, `difference`), 없으면 `needs_information`과 `checks`의 `reuse_inputs`로 답한다. 업체가 끼지 않는 경로라 계산 근거는 배출 기업만 본다. `checks[].key`는 `reuse_origin`, `reuse_material`, `reuse_quality`, `reuse_inputs` |
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
         "photo":   {"contaminationPct": 2.0, "evidence": ["model:..."],
                     "materialEstimate": {"class": "plastic_rigid", "label": "경질 플라스틱", "wasteItem": "폐플라스틱", "confidence": 0.9, "evidence": ["model:..."]}},
         "currentTreatment": {"method": "혼합 소각", "unitCostKrwPerKg": 250, "evidence": ["doc:..."]},
         "pretreatHoursPerMonth": 4, "laborCostKrwPerHour": 12000, "pretreatEvidence": ["human:..."],
         "reuse": {"virginPriceKrwPerKg": 1500, "reusableKgPerMonth": 400, "regrindCostKrwPerKg": 120, "evidence": ["human:..."]}}

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

흐름의 `pretreatHoursPerMonth`·`laborCostKrwPerHour`·`reuse`는 순비용 계산에만 쓰는 배출 기업의 입력이다(없으면 `null`). `PUT /orgs/{orgId}/streams/{streamId}`로 넣고, 넣은 값은 `human` 근거로 등록된다. 업체에는 가지 않는다(업체가 받는 흐름 목록과 뷰에 없다).

- `pretreatHoursPerMonth`는 0 이상(시간/월), `laborCostKrwPerHour`는 0보다 큰 값(원/시간)이다. 작업 시간이 있을 때만 전처리 인건비를 계산하고, 시간당 인건비를 비우면 최저임금 기준값(`ref:최저임금:시간급@2026`)으로 계산한다. 작업 시간을 `null`로 보내면 둘 다 지운다.
- `reuse`는 `virginPriceKrwPerKg`(0보다 큰 값), `reusableKgPerMonth`(0보다 큰 값)가 함께 있어야 하고 `regrindCostKrwPerKg`는 비우면 0이다. `null`로 보내면 지운다. 틀린 값은 422 `INVALID_VALUE`다.
- 담당자 질문에 현행 처리비만 답했으면(`field: "currentCost"`) `currentTreatment.methodAssumed`가 `true`로 남는다. 처리 방법은 묻지 않았으므로 혼합 소각으로 보았다는 뜻이다.

## 7. 화면용 진단 뷰

`service.get_view(session_id, org_id) -> dict`가 돌려주는 모양이고, 화면은 이것만 그린다. 숫자와 조항은 모두 claim 객체로 온다.

```json
{
  "session": {"id": "ses_1", "emitterOrgId": "org_hanbit", "status": "idle | running | awaiting_answers | awaiting_approval | confirmed", "version": 3},
  "viewer": {"orgId": "org_hanbit", "role": "emitter"},
  "stages": [{"stage": 1, "key": "identify", "label": "식별·분류", "status": "waiting | running | done | needs_check"}],
  "streams": [{"id": "st_sprue", "name": "...", "material": "PP", "origin": "PIR", "form": "...",
               "monthlyKg": {"claim"}, "grade": {"value": "A", "basis": "measured | photo_estimate", "evidence": ["..."]},
               "currentTreatment": {"method": "혼합 소각", "unitCost": {"claim"}, "assumed": true},
               "costInputs": {"pretreatHoursPerMonth": 4, "laborCostKrwPerHour": 12000,
                              "reuse": {"virginPriceKrwPerKg": 1500, "reusableKgPerMonth": 400, "regrindCostKrwPerKg": 120}}}],
  "threads": [{"id": "th_1", "vendorOrgId": "org_saegil", "vendorName": "(주)새길자원", "vendorModel": {"provider": "foundry", "name": "gpt-5-mini"},
               "vendorAuthor": {"side": "vendor", "kind": "agent", "label": "업체 측 AI"},
               "status": "not_opened | open | agreed | declined | handoff | confirmed", "statusReason": "...", "reasonEvidence": ["..."],
               "streamIds": ["st_sprue"], "turns": 4, "maxTurns": 12,
               "messages": [{"id": "msg_1", "seq": 1, "from": "org_hanbit", "to": "org_saegil", "type": "inquiry", "stage": 4,
                             "streamIds": ["st_sprue"], "text": "... {{c1}} ...", "claims": [{"claim"}],
                             "validation": {"ok": true, "attempts": 1, "rejections": []}, "createdAt": "...",
                             "author": {"side": "company", "kind": "agent", "label": "회사 측 AI"}}]}],
  "graph": {"nodes": [{"id": "st_sprue", "type": "stream | criterion | vendor | evidence", "label": "..."}],
            "edges": [{"from": "st_sprue", "to": "pc_pp_a", "status": "met | conditional | unmet", "label": "...", "evidence": ["..."]}]},
  "result": {"status": "calculated | conditional | needs_information",
             "assignments": [{"streamId": "st_sprue", "vendorOrgId": "org_saegil", "route": "sell", "status": "calculated",
                              "unitPrice": {"claim"}, "monthlyAfter": {"claim"}, "monthlyNow": {"claim"}, "difference": {"claim"},
                              "revenue": {"claim"}, "transport": {"claim"}, "labor": {"claim"}, "transportPaidBy": "vendor | emitter | null",
                              "reuse": {"eligible": true, "status": "calculated | conditional | needs_information", "reason": "...",
                                        "monthlyAfter": {"claim"}, "difference": {"claim"}, "checks": []},
                              "carbon": {"claim"}, "checks": [], "alternatives": [{"vendorOrgId": "...", "route": "...", "monthlyAfter": {"claim"}, "note": "..."}]}],
             "totals": {"monthlyNow": {"claim"}, "monthlyAfter": {"claim"}, "difference": {"claim"}, "carbon": {"claim"}},
             "assumptions": [{"id": "assumption:...", "label": "현행 혼합 소각 처리비 (폐플라스틱)", "value": 250, "unit": "원/kg"}], "sensitivity": [{"label": "현행이 위탁 재활용일 때", "difference": {"claim"}}]},
  "questions": [{"id": "q_1", "batch": 1, "streamId": "st_film", "field": "moisturePct", "text": "...", "answered": false}],
  "approvals": [{"threadId": "th_1", "emitter": false, "vendor": false, "blocked": false, "blockedReason": null}],
  "documents": [{"id": "doc_1", "kind": "request | plan", "title": "...", "text": "... {{c1}} ...", "claims": [{"claim"}], "validation": {"ok": true}}],
  "events": [{"seq": 1, "at": "...", "actor": "company_agent | vendor_agent | validator | tool | system", "kind": "message | tool_call | validation | handoff | approval",
              "threadId": "th_1", "summary": "...", "detail": {}}]
}
```

`{"claim"}`은 `{"id", "kind", "value", "unit", "evidence"}` 객체다. 값이 없으면 `null`을 넣고 화면은 "확인 필요"를 보여 준다.

### 순비용의 내역과 공정 재투입

`result.assignments[]`의 `monthlyAfter`는 옮긴 뒤의 월 순비용이고(받으면 양수), 아래 값으로 나뉜다. 넷 다 배출 기업이 보는 뷰에만 온다.

| 필드 | 뜻 |
|---|---|
| `revenue` | 매각 수익. 처리비를 내는 경로면 음수다. `route`가 `reuse`면 신재 절감액(다시 넣는 양 × (신재 구입 단가 − 분쇄 비용))이다 |
| `transport` | 배출 기업이 따로 내는 월 운반비. 업체가 내거나 조건을 모르면 `null` |
| `labor` | 전처리 인건비(월 작업 시간 × 시간당 인건비). 작업 시간을 넣지 않았으면 `null` |
| `transportPaidBy` | 운반비를 누가 내는가. `vendor`(업체 단가에 포함), `emitter`(배출 기업이 따로 냄), 업체가 조건을 밝히지 않았으면 `null` |

- `route`에 `reuse`(공정 재투입)가 더해졌다: `sell | treat | keep | reuse`. `reuse`면 `vendorOrgId`·`threadId`·`unitPrice`가 `null`이다. 배정 최적화가 현행 유지, 재투입, 업체를 같은 기준(월 순비용)으로 견준다.
- `reuse`는 `judge_reuse`의 결과다. `eligible`은 후보 여부(조건을 다 보지 못했으면 `null`), `reason`은 한 줄 이유, `monthlyAfter`·`difference`는 신재 구입 단가와 다시 넣을 수 있는 양을 넣었을 때만 온다(없으면 `null`), `checks[]`는 `key`가 `reuse_`로 시작하는 것만 싣는다(`{key, status, text, evidence}`). `judge_reuse`를 부르지 않은 흐름은 `reuse: null`이다.
- 다시 넣지 못하는 양은 현행 처리 단가로 계산해 `monthlyAfter`에서 뺀다.
- `streams[].costInputs`는 흐름 레코드의 `pretreatHoursPerMonth`, `laborCostKrwPerHour`, `reuse`(근거 ID는 빼고 세 값만)다. 자료 화면이 지금 값을 보여 줄 때 쓴다.
- **업체가 보는 뷰에는 `revenue`·`transport`·`labor`·`reuse`가 `null`이고 `streams[].costInputs`는 필드 자체가 없다.** 재투입으로 간 흐름은 업체의 `assignments`에 나오지 않는다. `transportPaidBy`는 업체 자신의 조건이므로 남는다.
- `questions[].field`에 `currentCost`(현행 처리비, 원/kg)가 더해졌다. 재질·품질 질문 뒤에 질문 한도가 남을 때만 묻고, 업체에 넘기는 질문 목록에서는 뺀다(`questions.PRIVATE_FIELDS`).

### 사람이 읽는 이름

화면이 내부 키(`s0Difference`, `pc_pp_a`, `assumption:...`)를 그대로 보여 주지 않도록 이름을 함께 준다. 이름은 모두 코드(`app/v3/names.py`)가 정한다.

- **claim의 `label`과 `role`(선택 필드).** `{"id", "kind", "value", "unit", "evidence", "label": "매입 단가", "role": "unitPrice"}`. 이름을 정하지 못한 claim에는 두 필드가 없다. 근거 검증기는 이 두 필드를 보지 않는다. 상대 조직이 보낸 `label`은 쓰지 않고 받는 쪽에서 `role`과 키로 다시 만든다(이름 칸에 숫자를 실어 보내지 못한다).

  | `role` | `label` | 비고 |
  |---|---|---|
  | `unitPrice` | 매입 단가 | 값이 0 이상인 단가 |
  | `treatmentFee` | 처리비 | 값이 음수인 단가. **값은 음수 그대로다.** 화면은 크기로 보여 준다 |
  | `currentUnitCost` | 현행 처리 단가 | |
  | `referencePrice` | 참고 시세 | |
  | `monthlyKg` | 월 발생량 | |
  | `totalKg`, `totalM3` | 합친 월 물량, 합친 월 부피 | |
  | `monthlyAfter` | 변경 뒤 월 손익 | 합계는 "변경 뒤 월 손익 합계" |
  | `monthlyNow` | 현행 월 손익 | 합계는 "현행 월 손익 합계" |
  | `difference` | 현행 대비 차이 | 합계는 "현행 대비 차이 합계" |
  | `sensitivity` | 현행 가정이 다를 때의 차이 | |
  | `carbon` | 탄소 변화 | 합계는 "탄소 변화 합계" |
  | `carbonValue` | 탄소 감축 환산액 | |
  | `monthlyRevenue`, `transport`, `extraCost` | 월 매입액, 월 운송비(또는 "km당 운송비"), 추가 운송비 | |
  | `distance` | 왕복 거리 등 | |
  | `pickups`, `perPickup`, `trips` | 월 수거 횟수, 한 번 수거량(부피는 "한 번 수거 부피"), 회차 수 | |
  | `capacity` | 차량 적재 중량, 차량 적재 부피 | |
  | `threshold` | 최소 물량, 오염도 상한, 수분 상한 | 업체 기준값 |
  | `contamination` | 오염도(측정), 사진 추정 오염 비율 | |
  | `moisture` | 수분 | |
  | `priceDeviation` | 기준표 대비 단가 차이 | |
  | `clause` | 조항 인용 | `kind`가 `clause`인 claim |

- **처리비 문장.** 메시지 본문에 부호를 설명하는 문구("(음수는 배출 기업이 내는 처리비)")는 쓰지 않는다. 매입이면 "매입 단가는 {{…}}입니다", 처리비면 "처리비는 {{…}}입니다"로 쓰고, claim 값은 음수 그대로 두며 `role`이 `treatmentFee`다. 문서의 `rendered`와 `render.render_text`는 `treatmentFee`를 크기로 적는다("합의 처리비 80원/kg").
- **calc 근거의 `label`.** 내부 키가 아니라 한국어다. 흐름 하나를 다루는 계산이면 흐름 이름이 앞에 붙는다(예: "스프루·런너 현행 대비 차이 계산", "합친 월 물량 계산").
- **판단 그래프의 기준 노드.** `type`이 `criterion`인 노드의 `label`은 재질·등급·형태다(예: "PP · A등급 · 분쇄", 형태가 없는 기준은 "PP · B등급", 등외는 "PP+ABS · 등외"). `id`는 그대로 기준 ID다.
- **`result.assumptions`의 모양이 바뀌었다.** 이전에는 근거 ID 문자열의 배열(`["assumption:..."]`)이었고, 지금은 `[{"id", "label", "value", "unit"}]`이다. `id`는 근거 ID(눌러서 근거 패널을 연다), `label`은 근거의 이름, `value`·`unit`은 근거의 값이다. 값 하나로 적을 수 없는 가정(등급 기준표)은 `value`와 `unit`이 `null`이다.
- **`messages[].meta.asks[].field`에 올 수 있는 값.** `material | origin | form | contaminationPct | moisturePct | lotSame | monthlyKg | terms`. 앞의 일곱은 업체가 배출 기업에 묻는 흐름 항목이고(`questions[].field`와 같은 낱말이다), `terms`는 회사 측이 업체에 조건을 달라고 할 때다. 이 밖의 값은 메시지에 실리지 않는다(`agents/questions.py`의 `ASK_FIELDS`).

### 작성자 표시 (`author`, `vendorAuthor`)

대화 화면에는 구현 이름(모델 배포 이름, "규칙 정책(모델 미연결)")을 보여 주지 않는다. 화면은 `threads[].messages[].author.label`과 `threads[].vendorAuthor.label`을 쓴다.

```json
{"side": "company | vendor", "kind": "agent | system", "label": "회사 측 AI | 업체 측 AI | 자동 안내"}
```

| `side` | `kind` | `label` | 언제 |
|---|---|---|---|
| `company` | `agent` | 회사 측 AI | 회사 측 에이전트가 쓴 메시지(모델이든 규칙 정책이든 같다) |
| `vendor` | `agent` | 업체 측 AI | 업체 측 에이전트가 쓴 메시지 |
| `company`, `vendor` | `system` | 자동 안내 | 코드가 쓴 안내(`model.provider`가 `code`. handoff, 허가 품목 decline 등) |

- `vendorAuthor`는 그 대화의 업체 측 작성자 표시다(`{"side": "vendor", "kind": "agent", "label": "업체 측 AI"}`).
- 아래 `model`과 `vendorModel`은 그대로 남아 있다(화면이 `author`로 옮겨 가는 동안 지우지 않는다). 구현 이름은 운영 정보이므로 일반 화면에는 쓰지 않고, 관리자 화면(`GET /admin/status`의 `models`)과 실행 기록에서만 본다. 화면이 옮겨 가면 일반 뷰의 `model`·`vendorModel`은 지울 수 있다.

### 메시지를 쓴 구현 (`model.provider`)

`threads[].messages[].model`과 `threads[].vendorModel`은 `{"provider", "name"}`이다. 실제로 그 메시지를 쓴 구현을 적는다(쓰지 않은 모델 이름을 붙이지 않는다). `provider`에 올 수 있는 값:

| `provider` | `name`의 예 | 뜻 |
|---|---|---|
| `rule` | `규칙 정책(모델 미연결)` | 모델이 연결되지 않아 규칙 정책(`agents/policy_rule.py`)이 썼다. 모델이 아니다 |
| `foundry` | `gpt-5-mini`(배포 이름) | Azure AI Foundry의 모델이 썼다(`FoundryChat`, Agent Framework 경로 포함) |
| `ollama` | `qwen3`(모델 이름) | 업체가 고른 오픈 모델이 썼다(`OllamaChat`) |
| `code` | `진행기(코드)` | 모델이 아니라 코드가 쓴 메시지다. 아래 "코드가 쓰는 메시지" |

- `vendorModel`에는 `configured`(업체 프로필의 설정 `{"provider": "foundry | ollama", "name"}`)와 `used`(이 진단에서 그 구현이 실제로 답을 썼는지)가 붙는다. `vendorModel.provider`는 `rule | foundry | ollama` 가운데 하나다(`code`는 메시지에만 붙는다).
- 실행 기록의 `events[].detail.model`도 같은 모양이다. 테스트의 `ScriptedLLM`은 `provider`가 `scripted`이고 배포된 서버에서는 나오지 않는다.

코드가 쓰는 메시지(`provider`가 `code`). 본문은 정해 둔 문장이고 숫자와 claim이 없다:

| `type` | 보내는 쪽 | 언제 |
|---|---|---|
| `handoff` | 어느 쪽이든 | 초안이 두 번 거부됨, 턴 상한, 받은 답이 재검증에서 거부됨, 업체 측에 닿지 못함, 문서에서 지시문 주입 탐지 |
| `decline` | 어느 쪽이든 | 이 대화에 허가 품목이 맞는 흐름이 하나도 남지 않음. `meta.reasonEvidence`에 허가증 근거 |
| `answer` | 업체 | 모델이 허가 품목이 맞지 않는 흐름에 조건을 내려다 두 번 거부됐고, 맞는 흐름이 남아 있음. `meta.excluded`에 뺀 흐름 |
| `question` | 배출 기업 | 받은 조건에 허가 품목이 맞지 않는 흐름이 있어 그 답을 붙이지 않고, 그 흐름만 빼고 남은 흐름의 조건을 다시 물음. `meta.excluded`, `meta.asks`(`field`는 `terms`) |
| `counter` | 배출 기업 | 합의 뒤에 업체의 조건이 바뀌어 대화를 다시 엶(아래 "합의 뒤 업체 조건 변경"). `meta.asks`(`field`는 `terms`) |

### 업체가 보는 뷰의 result

업체(`viewer.role`이 `vendor`)가 보는 `result`는 다음 모양이다. 배출 기업의 현행 대비 손익과 다른 업체의 제안은 나가지 않는다.

```json
{"status": "calculated | conditional | needs_information",
 "assignments": [{"streamId", "vendorOrgId", "vendorName", "threadId", "route", "status", "unitPrice": {"claim"},
                  "currentAssumed", "checks": [],
                  "monthlyAfter": null, "monthlyNow": null, "difference": null, "carbon": null, "carbonValue": null,
                  "alternatives": []}],
 "totals": {"monthlyNow": null, "monthlyAfter": null, "difference": null, "carbon": null},
 "assumptions": [], "sensitivity": [], "referencePrices": [{"참고 시세. 배출 기업이 보는 것과 같다"}]}
```

- `assignments`에는 자기에게 배정된 흐름만 있고, 하나도 없으면 `status`는 `needs_information`이다.
- `monthlyAfter`, `monthlyNow`, `difference`, `carbon`, `carbonValue`는 늘 `null`, `alternatives`는 늘 빈 배열이다.
- `checks`에는 업체가 내건 조건(`vendor_condition`)과 적재 검사(`min_volume`, `loading`)만 남는다.
- `totals`의 네 값은 모두 `null`, `assumptions`와 `sensitivity`는 빈 배열이다. `carbonInObjective`와 `evidence`는 없다.
- `result`가 아직 없으면(`null`) 그대로 `null`이다.

구현하면서 더한 항목(있던 항목의 모양은 그대로다):

- `session.phase`, `session.today`
- `streams[]`: `orgId`, `process`, `wasteItem`, `lot`, `contaminants`, `monthlyKgBasis`, `contamination`·`photoContamination`·`moisture`(claim), `unknownFields`, `grade.label`. 업체가 보는 뷰에서는 `currentTreatment`가 `null`이다.
- `threads[]`: `emitterOrgId`, `assignedStreamIds`(최적안에서 이 업체에 배정된 흐름), `agreedSeq`, `vendorModel.configured`·`vendorModel.used`, `vendorAuthor`. `messages[]`에는 `author`, `model`과 `meta`(`asks`, `excluded`, `reason`, `terms`의 `items[{streamId, claimId}]`·`days`·`conditions`)가 붙는다. 조건의 값은 `claims`에만 있다.
- `graph.edges[]`: `kind`(`permit | criterion | assignment | excluded | regulation | document`). `excluded` 엣지의 `evidence`에는 허가 품목 때문에 뺀 경우 허가증 근거가 실린다.
- **업체가 보는 뷰의 `graph`.** 자기 매입 기준 노드(`type: "criterion"`)와, 흐름을 그 기준과 대조한 엣지(`kind: "criterion"`, 흐름 → 기준은 `met | conditional | unmet`과 첫 번째 확인할 것, 기준 → 업체는 "매입 기준표"), 최적안 배정 엣지(`kind: "assignment"`)가 들어간다. 같은 판단 도구(`judge_route`)의 결과에서 업체의 매입 기준·허가 품목과, 대화에서 업체에 열린 흐름 자료(오염도, 수분, 형태, 혼입물, 물량의 근거)를 대조한 검사만 쓴다. 배출 기업의 현행 처리와 비용·탄소에 관한 검사, 규정 엣지(`regulation`), 다른 업체의 기준은 넣지 않는다. 엣지의 `evidence`는 업체가 열 수 있는 근거뿐이다.
- `result`: `referencePrices[{key, material, form, label, asOf, price: claim, note}]`(가격조사 참고 시세. 손익 계산에는 쓰지 않는다), `carbonInObjective`, `evidence`. `assignments[]`에는 `vendorName`, `threadId`, `carbonValue`(claim, KAU 환산), `currentAssumed`가 붙고, `checks[]`는 `{key, status, text, evidence}`에 업체가 내건 조건이면 `claims`가 붙는다(`text`의 자리표시자를 그 claims로 그린다). `alternatives[]`에는 `vendorName`, `unitPrice`(claim)가 붙는다. `sensitivity[]`는 흐름별이고 `streamId`가 붙는다.
- `result.assignments[].checks[]`의 `key`: 경로 판단에서 온 `contaminationPct`, `moisturePct`, `contamination_basis`, `moisture_basis`, `form`, `monthly_kg_basis`, `material_basis`, `mixture`, `permit`, `current_assumed`, `current_method`(현행 처리비만 답을 받아 처리 방법을 혼합 소각으로 보았을 때), 비용·탄소에서 온 `transport`, `carbon`, 업체가 내건 조건 `vendor_condition`, 그리고 `price_differs`, `unresolved`, `skipped`, `handoff`, 적재 검사 `min_volume`, `loading`, `loading_volume`이다.
  - **적재 검사(`check_loading`)는 업체가 정해진 배정마다 늘 들어간다.** `min_volume`(최소 물량)과 `loading`(무게·부피 적재), 밀도나 부피 용량을 몰라 부피를 확인하지 못했으면 `loading_volume`. 다른 검사와 달리 충족(`met`)한 것도 나간다. `claims`에 합친 월 물량·최소 물량(`min_volume`), 월 수거 횟수·한 번 수거량·차량 적재 한도(`loading`)가 붙는다. 그 업체에 실제로 배정된 흐름만 합친 값이다. 충족하지 못한 적재 검사가 있으면 그 배정은 `calculated`가 아니다.
  - `contamination_basis`, `moisture_basis`: 값은 매입 기준 상한 안이지만 측정값이 아니라 사진 추정이나 담당자 답이다(조건부. "반입 검수에서 확인").
  - `handoff`: 이 흐름을 맡기로 합의한 대화가 담당자에게 넘어가 배정을 뺐다(`status`는 `needs_information`, `vendorOrgId`는 `null`).
- `approvals[]`에는 승인할 수 있는 대화(`agreed`, `confirmed`)만 있다. 담당자에게 넘어간(`handoff`) 대화의 승인 칸은 나가지 않는다. 합의한 뒤에 배정된 흐름의 자료(재질, 물량, 측정값 등)가 바뀌면 `blocked`가 되고 `blockedReason`에 `STREAM_CHANGED`가 적힌다. 막힐 때 앞서 한 승인은 양쪽 모두 지워진다.
- `approvals[].blockedCodes`(배열, 새 항목): 막힌 사유 코드다. `blockedReason`(한국어 문장)에 들어 있는 코드를 처음 나온 순서대로 뽑은 것이고, 막히지 않았으면 빈 배열이다. 값은 8장 "승인을 막는 사유 코드"의 `EVIDENCE_CHANGED`, `STREAM_CHANGED`, `VENDOR_TERMS_CHANGED`와 근거 검증기의 거부 코드다. 화면은 문장을 뜯어 읽지 말고 이 배열을 본다.
- **막힘은 관련된 모든 대화에, 승인을 누르기 전에 걸린다.** 근거나 흐름 자료가 바뀌면(측정값 CSV를 같은 측정일로 다시 올림, 흐름 수정, 업체 프로필 수정, 사진 업로드, 문서 추출값 확정) 그 조직이 들어간 승인 대기 진단의 합의한 대화를 모두 다시 확인하고, 뷰를 만들 때(`GET /sessions/{id}`)도 다시 확인한다. 그 근거를 메시지에 인용한 대화는 배정과 관계없이 모두 막힌다. 바뀐 것이 없으면 뷰를 여러 번 읽어도 저장소와 실행 기록은 그대로다.

### 합의 뒤 업체 조건 변경 (`VENDOR_TERMS_CHANGED`)

합의(`agreed`) 뒤에 업체가 매입 기준표·허가 품목·가드레일을 바꿔 그 대화의 합의 조건에 영향을 주면:

1. 그 대화의 승인을 양쪽 모두 지우고 `blocked`로 둔다. `blockedCodes`에 `VENDOR_TERMS_CHANGED`가 실린다. 고친 때(`PUT /vendors/{orgId}`)와 뷰를 만들 때 확인한다. 이 상태에서 승인하면 409 `APPROVAL_BLOCKED`다.
2. `POST /sessions/{id}/run`을 다시 부르면 그 대화를 다시 연다. 회사 측이 코드가 쓴 `counter`를 보내고(스레드는 `open`으로 돌아간다) 업체 측이 지금 조건으로 다시 답한다. 같은 스레드에서 이어지고 턴 상한(12)은 그대로다. 상한에 닿으면 `handoff`다.
3. 대화가 끝나면 이미 합의했거나 확정한 다른 대화의 배정은 그대로 둔 채 최적안과 결과 한 장을 다시 만든다. 새 조건이 최적안에 들어가면 그 대화는 다시 `agreed`가 되고 승인을 새로 받는다. 단가가 내려 다른 업체의 제안이 더 나으면 그 흐름은 그쪽으로 옮겨 가고, 옮겨 받은 대화도 승인을 다시 받는다.
4. 다시 연 대화가 합의에 이르지 못하고 닫히면(가드레일 밖이라 조건을 내지 못함, 턴 상한) 앞서 합의했던 흐름은 다른 제안으로 옮겨 가거나 `needs_information`과 `handoff` 검사로 나간다.

무엇이 "영향을 주는가": 배정된 흐름마다 지금 프로필로 다시 본 것이 합의한 때와 다르면 영향을 준 것이다. 보는 것은 허가 품목이 맞는지, 골라지는 매입 기준과 그 단가·최소 물량·오염도 상한·수분 상한, 합의한 단가와 요일이 가드레일(하한·상한 단가, 가능 요일) 안인지, 자동 거절 조건에 걸리는지, 그리고 수거 횟수를 정하는 값(월 최소 횟수, 차량 적재 중량·부피)이다. 합의 조건과 관계없는 변경(쓰이지 않은 다른 기준, 가능 요일 추가, 합의 단가보다 낮은 하한, 모델 선택, km당 운송비)은 막지 않는다.

- 다시 여는 것은 `VENDOR_TERMS_CHANGED`뿐이다. `STREAM_CHANGED`가 함께 걸린 대화는 다시 열지 않는다(흐름 자료가 바뀌면 진단을 새로 한다).
- 다시 연 뒤에는 그 `counter` 앞에 업체가 낸 답을 승인 전 재검증에서 보지 않는다(지난 조건의 기록이다. 업체가 기준표 값을 고치면 그 답의 근거도 함께 바뀐다). 회사 측 메시지와 `counter` 뒤의 메시지는 그대로 본다.
- 허가 품목만 일부 빠진 경우는 아래 "허가 품목이 일부만 맞지 않을 때"를 따른다(대화를 다시 열지 않고 그 흐름만 뺀다).

### 허가 품목이 일부만 맞지 않을 때

한 대화의 흐름 가운데 일부만 허가 품목이 맞지 않으면 대화 전체를 닫지 않는다. 그 흐름만 빼고 나머지는 유지한다. 모델이 무엇을 내든 코드가 그렇게 한다.

- 뺀 흐름은 `graph.edges[]`에 `kind: "excluded"`(이유 "허가 품목이 맞지 않습니다.", `evidence`에 허가증 근거)로 나가고, 그 흐름에 받은 제안은 지워져 배정에도 대안에도 쓰이지 않는다. 실행 기록에 `kind: "thread"` 한 줄이 남는다(`detail.excludedStreamIds`, `detail.reasonEvidence`).
- 업체 측에서 알았을 때: 코드가 쓴 `answer`(`meta.excluded`)가 대화에 붙고 나머지 흐름은 이어진다. 회사 측에서 알았을 때(받은 조건에 그 흐름이 있음): 그 답은 붙이지 않고 코드가 쓴 `question`으로 남은 흐름의 조건을 다시 묻는다.
- 합의한 뒤에 알았을 때(승인 시점): 승인 요청은 409 `PERMIT_MISMATCH`로 답하고, 그 흐름만 배정에서 뺀 뒤 양쪽 승인을 지운다. 대화는 `agreed` 그대로이고 `approvals[].streamIds`가 줄어든다. 풀려난 흐름은 다른 제안으로 옮겨 가거나 `needs_information`과 `handoff` 검사로 나간다.
- 그 대화에 허가 품목이 맞는 흐름이 하나도 남지 않을 때만 근거와 함께 `decline`으로 닫는다.

### 닫는 이유 (`threads[].statusReason`)

여러 흐름을 거절한 `decline`이면 거절한 흐름을 이유와 함께 모두 적는다: `"받지 않는 흐름: <이유 문장> / <흐름 이름들>: <이유>."`. 이유가 같은 흐름은 한데 묶고, 메시지의 `meta.reason`이 그 이유들과 다른 문장이면 맨 앞에 둔다. 흐름 하나만 거절했으면 이전처럼 `meta.reason` 그대로다. 메시지의 `meta.reason`과 `meta.excluded`는 바뀌지 않는다.
- `events[].detail.reportedBy`: 값이 `"vendor"`이면 업체 측 에이전트가 답과 함께 보낸 기록(도구 호출, 검증)이다. 회사 측은 정해 둔 항목만 옮기고 요약을 다시 쓴다. 실행됐다는 도구 호출은 그 도구의 calc 근거가 근거 저장소에 있을 때만 옮긴다.
- `events[].detail.arguments`(도구 호출): 흐름 ID(`streamId`, `streamIds`), `route`, `transportPaidBy`, 숫자가 없는 검색어(`query`)만 남는다. 모델이 덧붙인 다른 인자는 싣지 않는다.
- `session.status`: 합의한 대화가 모두 담당자에게 넘어가 승인할 대화가 남지 않으면 `awaiting_approval`에서 `idle`(`phase`는 `done`)로 바뀐다. 합의 없이 끝난 진단과 같은 상태다.
- `questions[]`: `unknown`, `answerType`(`number | choice | text`), `unit`, `choices`
- `unresolved[{streamId, field}]`: 질문 한도를 다 쓰고도 모르는 값
- `approvals[]`: `streamIds`
- `documents[]`: `threadId`, `streamIds`, `rendered`(자리표시자를 값으로 바꾼 글), `createdAt`
- **문서는 양쪽이 함께 본다. 배출 기업의 현행 처리 방법이나 그것에서 나온 값은 문서에 없다.** 이행 계획서에 있던 "탄소 변화(현행 혼합 소각 대비…)" 줄은 뺐고, 현행 처리에 관한 검사(`current_cost`, `current_assumed`)는 "반입 전 확인"에 넣지 않는다. 탄소 변화와 그 환산액은 배출 기업만 보는 `result.assignments[].carbon`, `carbonValue`에 있다.
- **업체가 보는 뷰 전체에 현행 처리 방법·현행 단가·그것에서 나온 값이 없다**(`tests/v3/test_fix2_vendor_view_current.py`). 업체 쪽 판단 도구가 현행 처리를 모른 채 가정값으로 낸 계산 근거의 ID(`calc:…:currentUnitCost`, `monthlyNow`, `difference`, `sensitivity*`, `carbonDifference`, `kauValue`, `assumption:current.*`)도 업체 뷰의 실행 기록에서 빼고 `hiddenEvidenceCount`에 센다. 두 에이전트의 claim 풀에도 이 값들은 들어가지 않는다(상대에게 인용할 수 없다).
- `events[].kind`: `thread`, `question`, `document`, `stage`를 더했다
- `events[].detail.hiddenEvidenceCount`: 실행 기록에 실린 근거 ID 가운데 보는 조직이 열 수 없는 것은 뷰를 만들 때 뺀다(상대 쪽 도구 호출의 `detail.evidence` 같은 곳). 뺀 것이 있을 때만 그 개수(정수)가 붙는다. 배출 기업이 보는 뷰와 업체가 보는 뷰 모두 같다. 저장된 기록은 바뀌지 않는다. 진단 뷰 전체에 보는 조직이 열 수 없는 근거 ID는 실리지 않는다(`tests/v3/test_view_evidence_scope.py`).

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

접두어는 `/api/v3`이다. 요청하는 조직을 정하는 방법은 설정 `V3_AUTH`로 고른다(아래 "요청한 조직"). 기본은 `X-Org-Id` 헤더다. 실제 계정과 조직(가입, 로그인, 조직 만들기, 초대, 체험하기)은 `bff` 모드에서만 열리고 경로는 9장에 따로 적는다.

### 요청한 조직 (인증 모드)

| `V3_AUTH` | 조직을 정하는 방법 | `GET /orgs` |
|---|---|---|
| `header` (기본) | `X-Org-Id` 헤더를 그대로 믿는다 | 조직을 밝히지 않아도 모든 조직을 준다(화면의 조직 고르기용) |
| `token` | `Authorization: Bearer <토큰>`. 토큰에 묶인 조직이 요청한 조직이고 `X-Org-Id`는 무시한다 | 요청한 조직 자신과 대화 상대만 준다(배출 기업은 자기 진단의 스레드에 있는 업체, 업체는 자기와 열린 대화의 배출 기업, 관리자는 모두) |
| `bff` | 프런트 서버(Next.js BFF)만 부른다. `Authorization: Bearer <V3_BFF_KEY>`, 사용자는 `X-User-Id`, 조직은 `X-Org-Id`. 사용자가 그 조직의 구성원인지 계정 저장소로 확인한다. `X-Demo-View: 1`이면 체험하기(시연 조직 읽기 전용) | `token`과 같다(자기 조직과 대화 상대만). 체험하기에서는 시연 조직 가운데 자기와 대화 상대만 |

- **header 모드는 시연 전용이며 공개 주소에 그대로 두면 조직 경계가 지켜지지 않는다.** 헤더만 바꾸면 다른 조직의 뷰를 받고 승인할 수 있다. 서비스 함수 아래의 조직 경계(뷰, 근거, 스레드)는 요청한 조직이 맞다는 전제에서만 지켜진다.
- token 모드의 토큰과 조직의 짝은 설정 `V3_ORG_TOKENS`로 받는다. JSON `{"조직 ID": "토큰"}`이고 값은 목록도 된다(교체 기간에 둘을 함께 둘 때). 토큰은 16자 이상이고 두 조직이 같은 토큰을 쓸 수 없다. Key Vault에서 주입하고 저장소에 넣지 않는다. token 모드인데 비어 있거나 모양이 틀리면 요청을 처리하지 않는다(`devserver`는 뜨지 않고, `api.py`는 500 `INTERNAL`로 답한다).
- 토큰 비교는 `hmac.compare_digest`로 하고 모든 항목을 끝까지 견준다. 토큰 → 조직 해석은 `app/v3/httpio.py`의 `Auth`, `org_tokens`, `match_token`에 있고 두 서버가 같이 쓴다.
- token 모드의 오류: 토큰이 없거나 `Bearer` 꼴이 아니면 401 `TOKEN_REQUIRED`, 토큰이 어느 조직의 것도 아니면 401 `INVALID_TOKEN`. `GET /health`만 토큰 없이 답한다. `POST /demo/reset`은 토큰이 없으면 서비스의 규칙(`V3_DEMO_OPEN`, 관리자 조직)대로 답한다.
- `devserver`도 같은 옵션을 받는다: `--auth header|token`, `--org-tokens <JSON 파일>`.
- `GET /admin/status`의 `auth`: `{"mode": "header | token", "demoOnly": true | false}`. 관리자 화면이 시연 전용 모드임을 보여 줄 때 쓴다.
- token 모드의 토큰은 FastAPI와 `devserver` 모두 `V3_ORG_TOKENS_FILE`(JSON을 담은 파일 경로)이 있으면 그 파일에서 먼저 읽고, 없으면 `V3_ORG_TOKENS`에서 읽는다.
- `GET /admin/status`의 `distance`: `{"provider": true | false, "kind": "KakaoDistance | AzureMapsDistance | null", "lastError": "마지막 실패 이유 | null"}`. `provider`가 `false`면 직선 거리에 우회 계수를 곱한 가정값으로 계산한다. `lastError`에 키는 실리지 않는다.
- Entra 인증에는 연결하지 않았다. token 모드는 조직 단위 식별이고 담당자 개인은 가리지 않는다.
- bff 모드(9장): 모든 요청에 BFF 키가 있어야 한다(`GET /health`만 키 없이 답한다). 키는 설정 `V3_BFF_KEY`(Key Vault `v3-bff-key`)로 받고 `hmac.compare_digest`로 견준다. 키가 비어 있으면 모든 요청을 503으로 거절한다. 사용자가 `X-Org-Id`의 구성원이 아니면 403(`X-Org-Id`를 바꿔 다른 조직이 될 수 없다). 그다음의 조직 경계는 위 서비스 함수의 규칙 그대로다. `devserver`는 bff 모드를 받지 않는다(FastAPI만).
- bff 모드에서 실제 업체의 공개 프로필(`GET /vendors/{orgId}`의 허가 품목과 모델 선택)은 그 업체와 스레드가 있는 배출 기업에게만 보인다. 다른 조직에는 404 `VENDOR_NOT_FOUND`다. 시연 업체의 공개 프로필은 전처럼 누구나 본다.
- `GET /admin/status`의 `auth.demoOnly`는 header 모드에서만 `true`다.

| 메서드와 경로 | 내용 |
|---|---|
| `GET /health` | `{"ok": true}`. 조직 헤더 없이 부른다 |
| `POST /demo/reset` | 시연 데이터를 처음 상태로 되돌린다. 관리자만(아래 "관리자 조직") |
| `GET /orgs` | 조직 목록. 조직 레코드의 `order`가 작은 것부터(시연 주인공 `org_hanbit`이 맨 앞, 운영 조직이 맨 뒤). `order`가 없는 조직은 그 뒤에 ID 순. token 모드에서는 자기 조직과 대화 상대만 |
| `GET /orgs/{orgId}/streams`, `PUT /orgs/{orgId}/streams/{streamId}` | 흐름 조회와 수정(현행 처리, 전처리 작업 시간·시간당 인건비, 공정 재투입 값 포함. 6장) |
| `GET /vendors/{orgId}`, `PUT /vendors/{orgId}` | 업체 프로필(허가, 매입 기준, 노선·차량, 가드레일, 모델 선택) |
| `POST /sessions` | `{emitterOrgId}`로 진단을 만든다. `emitterOrgId`가 없으면 요청한 조직이다 |
| `GET /sessions` | 내가 당사자인 진단의 목록(최근 것이 앞). `[{id, emitterOrgId, emitterName, status, createdAt, updatedAt, as}]`. 배출 기업은 자기 진단, 업체는 자기에게 열린 대화가 있는 진단, 운영자는 전부. `as`는 `emitter`·`vendor`·`admin` |
| `GET /sessions/{id}` | 진단 뷰 |
| `POST /sessions/{id}/run` | 에이전트를 다음 멈춤 지점까지 돌린다. 진단 뷰를 돌려준다 |
| `POST /sessions/{id}/answers` | `{questionId, value | "unknown"}` |
| `POST /sessions/{id}/approvals` | `{threadId}`. 요청한 조직 쪽 승인을 기록한다. 양쪽이 승인하면 confirm 메시지와 문서를 만든다 |
| `GET /evidence/{id}` | 근거 하나와 계산 사슬 `{evidence, chain: [evidence]}` |
| `GET /evidence?q=&kind=` | 근거 검색 |
| `GET /intake/csv/template`, `POST /intake/csv` | 측정값 CSV 양식과 업로드 |
| `POST /intake/media` | 사진·영상 업로드(multipart: `file`, `orgId`, `streamId`). 탐지 모델을 불러 재질 추정과 이물 비율을 model 근거로 등록한다. 아래 "사진·영상" |
| `POST /intake/documents` | 문서 업로드와 추출 |
| `POST /intake/documents/confirm` | `{orgId, documentName, fields}`. 담당자가 확인한 추출값(`confirmed: true`)만 doc 근거로 등록한다. 응답은 `{"evidence": [근거 ID]}` |
| `GET /admin/status`, `POST /admin/eval` | 색인·기준값 상태와 재학습 표, 사례 시험 실행(관리자만) |
| `POST /sessions/{id}/corrections` | `{streamId, field, before, after, note}`. 배출 기업 담당자가 고친 값을 기록한다 |
| `POST /sessions/{id}/outcomes` | `{threadId, streamId, actualGrade, actualUnitPriceKrwPerKg, actualKg, inspectedOn, note, documentName?}`. 업체가 확정 뒤 실제 결과를 기록한다 |
| `GET /admin/retrain/export`, `POST /admin/retrain/runs` | 재학습 행 내보내기, 재학습 실행 결과 등록(관리자만) |

오류는 `{"error": {"code": "...", "message": "한국어 문장"}}`과 맞는 상태 코드로 낸다.

### 오류 코드 전체 목록

요청을 읽다가 나는 오류와 요청한 조직을 정하다 나는 오류는 두 서버(`api.py`, `devserver.py`)가 `app/v3/httpio.py`의 같은 함수로 낸다. 코드와 상태 코드가 서버에 따라 달라지지 않는다. 아래가 `/api/v3`가 내는 `error.code`의 전부다(`tests/v3/test_fix2_contract_docs.py`가 코드와 이 표를 대조한다).

| 코드 | 상태 | 조건 |
|---|---|---|
| `INVALID_JSON` | 400 | 본문이 JSON이 아니다 |
| `INVALID_MULTIPART` | 400 | `multipart/form-data`에 경계(boundary)가 없다. `api.py`는 starlette가 읽지 못한 multipart 본문 전부에 이 코드로 답한다 |
| `ORG_REQUIRED` | 401 | 요청한 조직을 밝히지 않았다(header 모드에서 `X-Org-Id`가 없다. bff 모드에서도 같다) |
| `TOKEN_REQUIRED` | 401 | token 모드인데 `Authorization: Bearer` 헤더가 없거나 모양이 틀렸다 |
| `INVALID_TOKEN` | 401 | token 모드인데 토큰이 어느 조직의 것도 아니다 |
| `INVALID_BFF_KEY` | 401 | bff 모드인데 `Authorization: Bearer` 키가 없거나 맞지 않다 |
| `USER_REQUIRED` | 401 | bff 모드에서 사용자가 필요한 경로인데 `X-User-Id`가 없다 |
| `UNKNOWN_USER` | 401 | bff 모드에서 `X-User-Id`가 가입한 사용자가 아니다 |
| `INVALID_CREDENTIALS` | 401 | 로그인 이메일이나 비밀번호가 틀렸다. 어느 쪽이 틀렸는지 알려 주지 않는다(문장도 같다) |
| `UNKNOWN_ORG` | 401 | 모르는 조직이다 |
| `FORBIDDEN` | 403 | 그 조직이 할 수 없는 일이다(다른 조직의 자료, 관리자 전용 경로, 배출 기업만 하는 진행·답·교정 등) |
| `WRONG_ROLE` | 403 | 조직의 역할이 맞지 않는다(배출 기업만, 처리 업체만) |
| `NOT_A_MEMBER` | 403 | bff 모드에서 사용자가 그 조직(`X-Org-Id`나 주소의 조직)의 구성원이 아니다 |
| `OWNER_ONLY` | 403 | 조직 소유자(owner)만 할 수 있다(초대 만들기) |
| `DEMO_READ_ONLY` | 403 | 체험하기(`X-Demo-View: 1`)에서 GET이 아닌 요청을 보냈다 |
| `NOT_DEMO_ORG` | 403 | 체험하기의 `X-Org-Id`가 시연 조직이 아니다(실제 조직, 운영 조직, 없는 조직) |
| `NOT_FOUND` | 404 | 그런 주소가 없다(bff가 아닌 모드에서 9장의 경로를 부른 경우도) |
| `ORG_NOT_FOUND`, `VENDOR_NOT_FOUND`, `STREAM_NOT_FOUND`, `SESSION_NOT_FOUND`, `THREAD_NOT_FOUND`, `QUESTION_NOT_FOUND`, `EVIDENCE_NOT_FOUND` | 404 | 없거나 볼 수 없다(볼 수 없는 것은 있는지도 알려 주지 않는다) |
| `NOT_AGREED` | 409 | 합의된 조건이 없는 대화를 승인하려 했다 |
| `EMAIL_TAKEN` | 409 | 이미 가입한 이메일이다(대소문자와 앞뒤 공백은 가리지 않는다) |
| `APPROVAL_BLOCKED` | 409 | 승인이 막혀 있다. 이유는 `approvals[].blockedReason`과 `blockedCodes`에 있다(아래 "승인을 막는 사유 코드") |
| `PERMIT_MISMATCH` | 409 | 승인하려는 대화의 업체 허가 품목이 합의 뒤에 바뀌어 흐름과 맞지 않는다. 승인하지 않는다. 맞는 흐름이 남지 않으면 그 대화를 근거와 함께 `decline`으로 닫고, 일부만 맞지 않으면 그 흐름만 빼고 승인을 지운다(남은 조건으로 다시 승인한다) |
| `THREAD_NOT_CONFIRMED` | 409 | 확정되지 않은 대화에 실제 결과를 기록하려 했다 |
| `INVITE_INVALID` | 410 | 초대가 없거나, 만료되었거나(7일), 이미 쓰였다 |
| `TOO_LARGE` | 413 | 본문이 20MB를 넘는다 |
| `UNSUPPORTED_MEDIA` | 415 | 사진·영상이 아닌 파일이다. 탐지 모델이 연결되지 않았어도 형식 검사가 먼저다 |
| `INVALID_BODY` | 422 | 본문이 JSON 객체가 아니거나 필수 항목(`questionId`·`value`, `threadId` 등)이 없다 |
| `FILE_REQUIRED` | 422 | 올릴 파일이 없거나 비어 있다 |
| `INVALID_VALUE` | 422 | 값이 틀렸다(음수 물량, 범위를 벗어난 비율, 모르는 모델 설정, 날짜 꼴 등) |
| `INVALID_KIND` | 422 | 근거 종류나 문서 종류가 목록에 없다 |
| `INVALID_ANSWER` | 422 | 질문의 답이 그 항목에 맞지 않는다(빈 답 포함) |
| `INVALID_FIELD` | 422 | 교정할 수 없는 항목이다(`POST /sessions/{id}/corrections`의 `field`) |
| `NO_CHANGE` | 422 | 교정 전후 값이 같다 |
| `STREAM_NOT_IN_THREAD` | 422 | 그 대화에서 확정된 흐름이 아니다(실제 결과 기록) |
| `INVALID_MESSAGE` | 422 | 업체 측 에이전트가 받은 메시지를 처리할 수 없다 |
| `TOO_MANY_ATTEMPTS` | 429 | 같은 이메일로 15분 안에 로그인을 10번 틀렸다(서버 프로세스 메모리에서 센다) |
| `INTERNAL` | 500 | 예상하지 못한 오류. 내용은 응답에 싣지 않는다 |
| `DOCUMENT_FAILED`, `SHIELD_FAILED`, `DETECTOR_FAILED` | 502 | Document Intelligence, Prompt Shields, 탐지 엔드포인트 호출이 실패했거나 계약과 다른 답이 왔다 |
| `DOCUMENT_INTELLIGENCE_UNAVAILABLE` | 503 | Document Intelligence가 설정되지 않아 글이 아닌 문서를 읽을 수 없다 |
| `BFF_KEY_NOT_SET` | 503 | bff 모드인데 BFF 키 설정이 비어 있다. 모든 요청을 거절한다 |

업체 측 에이전트 서비스(`vendor_service.py`. `/api/v3`가 아니라 따로 뜨는 서비스다)만 내는 코드: `UNAUTHORIZED`(401, 서비스 키가 틀림), `WRONG_RECIPIENT`(403, 이 서비스의 업체에게 온 메시지가 아님), `SERVICE_KEY_NOT_SET`·`VENDOR_NOT_SET`(503, 설정이 빠짐). 로컬에서 따로 띄우는 표준 라이브러리 서버(`vendor_devserver.py`)는 같은 코드를 내고, 회사 측 서버가 저장 파일을 아직 쓰지 않았으면 `STORE_NOT_READY`(503)를 낸다. 나머지는 위 표와 같다.

### 승인을 막는 사유 코드

HTTP 오류 코드가 아니라 `approvals[].blockedCodes`(배열)와 `blockedReason`(문장 안)에 실리는 코드다. 막힌 대화를 승인하려 하면 409 `APPROVAL_BLOCKED`가 난다.

| 코드 | 조건 | 푸는 방법 |
|---|---|---|
| `EVIDENCE_CHANGED` | 그 대화의 메시지가 인용한 근거가 대화 뒤에 바뀌었거나 사라졌다(예: 같은 측정일로 CSV를 다시 올림) | 진단을 새로 한다 |
| `STREAM_CHANGED` | 합의한 뒤에 배정된 흐름의 자료(재질, 물량, 측정값 등)가 바뀌었다 | 자료를 되돌리거나 진단을 새로 한다 |
| `VENDOR_TERMS_CHANGED` | 합의한 뒤에 업체가 매입 기준표·허가 품목·가드레일을 바꿔 그 대화의 합의 조건(단가, 최소 물량, 오염도·수분 상한, 요일, 횟수)에 영향을 준다 | `POST /sessions/{id}/run`을 다시 부르면 같은 대화를 새 조건으로 이어 간다 |
| 근거 검증기의 거부 코드(4장) | 메시지를 다시 검증했더니 통과하지 못한다(근거가 만료됨, 접근이 닫힘 등) | 진단을 새로 한다 |

### 초안 거부 코드

메시지의 `validation.rejections[].code`와 실행 기록의 `detail.rejections[].code`, `detail.codes`에 실리는 코드다. HTTP 오류가 아니다. 화면에는 코드와 코드별 일반 문장만 나간다(4장 "거부 기록에는 값이 없다").

| 코드 | 조건 |
|---|---|
| 근거 검증기의 코드 10종(4장) | `NUMBER_OUTSIDE_PLACEHOLDER`, `UNKNOWN_PLACEHOLDER`, `UNUSED_CLAIM`, `CLAIM_WITHOUT_EVIDENCE`, `EVIDENCE_NOT_FOUND`, `EVIDENCE_FORBIDDEN`, `EVIDENCE_EXPIRED`, `VALUE_MISMATCH`, `UNIT_MISMATCH`, `QUOTE_MISMATCH` |
| `UNKNOWN_CLAIM_REF` | claim 풀에 없는 ID를 썼다 |
| `TYPE_NOT_ALLOWED`, `APPROVAL_REQUIRED`, `THREAD_RULE`, `UNKNOWN_STREAM` | 지금 보낼 수 없는 종류, 승인 전 확정, 대화 규칙 위반, 이 대화에 없는 흐름 |
| `PERMIT_MISMATCH` | 허가 품목이 맞지 않는 흐름에 조건을 내려 했다 |
| `UNBACKED_TERM` | 숫자 없이 조건을 뜻하는 낱말("무상", "반값", "주말", "공휴일", "매일", "무제한", "전량" 등)이 있는데 그에 해당하는 terms 항목이나 claim이 없다. 업체 측과 회사 측 모두에 건다. 낱말 표는 `app/v3/a2a/term_words.py` |
| `GUARDRAIL_PRICE_BELOW_FLOOR`, `GUARDRAIL_PRICE_ABOVE_CEILING`, `GUARDRAIL_DAY_NOT_ALLOWED`, `GUARDRAIL_DAY_NOT_DECLARED`, `GUARDRAIL_PICKUPS_BELOW_MINIMUM`, `GUARDRAIL_AUTO_DECLINE_CONDITION`, `GUARDRAIL_AUTO_DECLINE_STREAM` | 업체 가드레일 밖의 조건이다 |
| `GUARDRAIL_TERMS_MISSING`, `GUARDRAIL_TERMS_REF`, `GUARDRAIL_TERMS_INCOMPLETE` | 조건을 내는 메시지의 `terms`가 없거나 claim과 맞지 않는다 |
| `OTHER_EMITTER_NAMED`, `OTHER_EMITTER_STREAM`, `OTHER_EMITTER_EVIDENCE` | 다른 배출 기업의 이름·흐름·근거를 드러냈다 |
| `TOO_MANY_TOOL_ROUNDS`, `INVALID_OUTPUT`, `MODEL_ERROR` | 도구를 너무 많이 부름, 응답 형식이 틀림, 모델 호출 실패 |
| `REJECTED` | 위에 없는 코드는 글자 그대로 옮기지 않고 이것으로 적는다 |

이전에 `api.py`만 쓰던 `INVALID_REQUEST`(400)와 `FILE_TOO_LARGE`는 없어졌다. 업체 측 에이전트 서비스(`vendor_service.py`)도 같은 코드를 쓴다.

관리자 조직:

- 시연 데이터에 운영 조직 `org_admin`(`"Re:Source AI 운영"`, `role: "admin"`)이 있다. 흐름도 업체 프로필도 없다.
- `/admin/*`(`GET /admin/status`, `POST /admin/eval`, `GET /admin/retrain/export`, `POST /admin/retrain/runs`)는 `X-Org-Id`가 관리자 조직일 때만 답한다. 다른 조직은 403 `FORBIDDEN`, 헤더가 없으면 401 `ORG_REQUIRED`.
- `POST /demo/reset`도 관리자만 부른다. 예외 두 가지: `App(demo_open=True)`이면 누구나 부른다(`devserver`의 기본값. `--demo-closed`로 끈다. `api.py`는 설정 `V3_DEMO_OPEN`, 기본 `false`). 그리고 조직이 하나도 없는 빈 저장소의 첫 시드는 막지 않는다(관리자 조직이 아직 없다).
- 배출 기업과 업체는 관리자 화면을 볼 수 없다. 화면에서 조직을 `org_admin`으로 바꿔야 한다.
- bff 모드의 관리자는 `org_admin`의 구성원인 사용자다(`X-Org-Id: org_admin`). HTTP로는 관리자 조직에 들어갈 수 없고 운영자가 `python -m scripts.v3.accounts_admin grant-admin <이메일>`로 넣는다. 시연 되돌리기는 9장의 규칙(시연 조직만 되돌린다)을 따른다.

사진·영상(`POST /intake/media`):

- 그 흐름의 배출 기업만 부른다. `streamId`가 그 조직의 흐름이 아니면 404 `STREAM_NOT_FOUND`.
- **탐지 모델(Azure ML 관리형 온라인 엔드포인트)이 연결되지 않았으면** 파일을 쓰지 않고 200으로 `{"detectorUnavailable": true, "message": "탐지 모델이 연결되지 않았습니다", "streamId", "evidence": []}`를 돌려준다. 탐지 결과를 지어내지 않는다. 연결은 `V3_DETECTOR_ENDPOINT`(채점 주소), `V3_DETECTOR_API_KEY`, 선택으로 `V3_DETECTOR_DEPLOYMENT`로 한다.
- 연결돼 있으면 `app/v3/intake/detector.py`의 `DetectorClient`가 `{"input_data": {"filename", "media_type": "image | video", "content_base64"}}`를 보내고, 기존 추론 결과 계약(`app/ml/track.py`의 `run()` 결과: `items`, `primary_material`, `contamination`, `detections`, `mode`, `frames_analyzed`, `tracks_total` 등)으로 답을 받는다. 영상은 같은 계약의 `mode: "video"`와 `tracks_total`(ByteTrack 트랙 수)을 그대로 따른다.
- 응답:

```json
{"detectorUnavailable": false, "streamId": "st_film", "filename": "필름.jpg", "mode": "photos | video",
 "framesAnalyzed": 1, "tracksTotal": 0, "model": {"name": "resource-ai-detector", "version": "v5"},
 "material": {"class": "plastic_film", "label": "필름", "wasteItem": "포장재", "confidence": 0.82, "evidence": ["model:..."]},
 "contamination": {"claim": {"claim"}, "reliable": false, "note": "..."},
 "detections": [{"id", "label", "kind": "item | contaminant", "confidence", "box": [x, y, w, h], "ratio"}],
 "evidence": ["model:resource-ai-detector:v5:st_film-재질", "model:resource-ai-detector:v5:st_film-오염비율"],
 "stream": {"흐름 레코드"}, "note": "..."}
```

- 이물 비율(면적 기준 0~1)은 %로 바꿔 `model:resource-ai-detector:<버전>:<흐름>-오염비율` 근거가 되고 흐름의 `photo.contaminationPct`와 `photo.evidence`에 들어간다. 상태 모델이 돌지 않아 `contamination`이 없으면 `contamination`은 `null`이고 흐름의 값도 바꾸지 않는다.
- 재질 추정은 `model:...:<흐름>-재질` 근거가 되고 흐름의 `photo.materialEstimate`(`{class, label, wasteItem, confidence, evidence}`)에 들어간다. 사진으로는 수지 종류(PP·PE·ABS)를 가르지 못하므로 흐름의 `material`은 바꾸지 않는다. `material`은 탐지된 것이 없으면 `null`이다.
- 오류: 사진·영상이 아닌 파일은 415 `UNSUPPORTED_MEDIA`, 엔드포인트 호출 실패나 계약과 다른 응답은 502 `DETECTOR_FAILED`.
- 근거는 올린 조직만 본다. 대화가 열리면 다른 사진 추정 근거와 같이 업체에 열린다.

재학습 데이터 수집(7단계) 주소의 규칙:

- `POST /sessions/{id}/corrections`: 그 진단의 배출 기업만 부를 수 있다(업체는 403, 당사자가 아니면 404). `field`는 `box | material | origin | form | contaminant | grade | contaminationPct | moisturePct`다. human 근거(`human:<조직>:교정-<흐름>-<항목>-<순번>`)로 등록하고, 앞의 다섯 항목은 탐지 모델 재학습 데이터 후보가 된다. 기록만 남기며 흐름의 값은 바꾸지 않는다(흐름은 `PUT /orgs/{orgId}/streams/{streamId}`로 고친다). 응답은 `{"correction": 기록, "evidence": [근거 ID]}`.
- `POST /sessions/{id}/outcomes`: 그 스레드의 업체만, 스레드가 `confirmed`일 때만 받는다(아니면 `THREAD_NOT_CONFIRMED` 409). `streamId`는 그 스레드에서 확정된 흐름이어야 하고, 실제 등급이나 실제 단가 가운데 하나는 있어야 한다. `documentName`을 주면 doc 근거, 없으면 human 근거로 등록한다. 소유는 업체이고 배출 기업과 나눈다. 같은 흐름과 검수일로 다시 보내면 같은 근거의 새 판이 된다. 응답은 `{"outcome": 기록, "evidence": [근거 ID]}`.
- `GET /admin/retrain/export`: `{"gradeLabels": [등급 모델 학습 행, synthetic=false], "detectionCorrections": [탐지 교정], "gate": 게이트}`. 여러 조직의 기록이 섞여 있어 `role`이 `admin`인 조직만 받는다.
- `POST /admin/retrain/runs`: Azure ML 재학습 작업(`scripts/v3/ml/compare_and_gate.py`)이 낸 `gate.json`을 그대로 받는다. 받은 값을 적을 뿐 점수나 판정을 만들지 않는다. 관리자만.
- `POST /admin/eval`의 응답: 맨 위(`scenario`, `sessionStatus`, `checked`, `passed`, `validatorPassRate`, `rewritten`, `threads`, `resultStatus`)는 시연 시나리오 한 건의 결과이고, `cases`가 사례 세트(50건)의 결과다: `{"model", "summary": {caseCount, draftCount, reviewCounts, verdictMatched, verdictMatchRate, openCount, openCases, validatorChecked, validatorPassed, validatorPassRate, forbiddenViolationCount, forbiddenClean, ...}, "results": [{id, title, review, open, verdictMatch, mismatches, forbiddenViolations, validator}]}`.

업로드는 `multipart/form-data`의 `file`, `orgId`, (문서는) `kind`로 받는다. `kind`는 `permit | purchase_criteria | material_spec | msds | contract`다.

## 9. 계정과 조직 (`V3_AUTH=bff`)

실제 사용자와 회사를 위한 경로다. 부르는 쪽은 프런트 서버(Next.js BFF) 하나다. 브라우저는 BFF와만 말하고, BFF가 로그인 세션으로 사용자를 확인한 뒤 아래 헤더를 붙여 부른다. bff가 아닌 모드에서는 이 장의 경로가 모두 404 `NOT_FOUND`다. 구현은 `app/v3/accounts.py`(판단), `app/v3/accounts_api.py`(경로), `app/v3/account_store.py`·`app/v3/sql.py`(저장)다.

### 헤더

| 헤더 | 뜻 |
|---|---|
| `Authorization: Bearer <V3_BFF_KEY>` | 모든 요청(`GET /health` 빼고). 키가 틀리면 401 `INVALID_BFF_KEY`, 키 설정이 비어 있으면 503 `BFF_KEY_NOT_SET` |
| `X-User-Id` | 요청한 사용자(`usr_…`). 가입·로그인·시연 조직 목록 말고는 모두 필요하다 |
| `X-Org-Id` | 요청한 조직. 8장의 경로에 필요하다. 사용자가 그 조직의 구성원이어야 한다(아니면 403 `NOT_A_MEMBER`) |
| `X-Demo-View: 1` | 체험하기. 아래 "체험하기" |

### 경로

| 메서드와 경로 | 필요한 것 | 내용 |
|---|---|---|
| `POST /accounts/signup` | 키 | `{email, password, name}` → 201 `{user: {id, email, name}}`. 이메일은 앞뒤 공백을 떼고 소문자로 맞춘다. 비밀번호는 8자 이상 256자 이하. 이미 있으면 409 `EMAIL_TAKEN`, 꼴이 틀리면 422 `INVALID_VALUE` |
| `POST /accounts/login` | 키 | `{email, password}` → 200 `{user, memberships: [{orgId, orgName, kind, memberRole}]}`. 틀리면 401 `INVALID_CREDENTIALS`(이메일이 없을 때와 비밀번호가 틀릴 때 같은 답), 15분에 10번 틀리면 429 `TOO_MANY_ATTEMPTS` |
| `GET /accounts/me` | 키, `X-User-Id` | 로그인과 같은 모양 |
| `POST /orgs` | 키, `X-User-Id` | `{name, kind: "emitter" \| "vendor", bizNo?, region?, address?, location?: [경도, 위도]}` → 201 조직. 만든 사람이 owner다 |
| `GET /orgs/{orgId}/members` | 키, `X-User-Id`(그 조직의 구성원) | `[{userId, name, email, memberRole}]` |
| `POST /orgs/{orgId}/invites` | 키, `X-User-Id`(그 조직의 owner) | 201 `{token, expiresAt}`. 구성원이 아니면 403 `NOT_A_MEMBER`, owner가 아니면 403 `OWNER_ONLY` |
| `POST /invites/{token}/accept` | 키, `X-User-Id` | 200 `{orgId, orgName, kind, memberRole: "member"}`. 없거나 만료되었거나 이미 쓴 초대는 410 `INVITE_INVALID`. 이미 구성원이면 초대를 쓰고 있던 소속을 그대로 준다 |
| `GET /demo/orgs` | 키 | 체험하기로 볼 수 있는 시연 조직 `[{id, name, kind}]`(조직 목록의 `order` 순) |

- `kind`는 `emitter | vendor | admin`(조직 레코드의 `role`과 같다), `memberRole`은 `owner | member`다. `POST /orgs`로는 `admin`을 만들 수 없다.
- `POST /orgs`의 응답: `{id, name, kind, bizNo, region, address, location, demo: false, createdAt, memberRole: "owner"}`. `id`는 `org_` + 16진 난수 12자라 시연 조직 ID와 겹치지 않는다. `bizNo`는 숫자 10자리를 `000-00-00000`으로 맞춰 둔다. 조직 레코드 자체는 8장의 저장소(`GET /orgs`)에 들어가므로 8장의 모든 경로가 새 조직에도 그대로 돈다.
- 새 배출 기업은 흐름 없이 시작한다. 흐름은 `PUT /orgs/{orgId}/streams/{streamId}`(`name` 필수)로 만든다. **흐름 ID는 모든 조직에 걸쳐 하나다.** 다른 조직이 이미 쓴 ID면 404 `STREAM_NOT_FOUND`이므로 BFF가 `st_` + 난수처럼 겹치지 않는 ID를 만든다.
- 새 업체는 빈 프로필(`permits: [], criteria: [], fleet: {}, guardrails: {}`)로 시작한다. `PUT /vendors/{orgId}`와 문서 올리기로 채운다. 허가 품목이나 매입 기준이 하나라도 생겨야 업체 탐색에 나온다. 실제 배출 기업의 업체 탐색은 시연 업체와 이렇게 채운 가입 업체를 모두 본다. 시연 배출 기업의 진단은 시연 업체만 본다.
- 비밀번호는 `hashlib.scrypt`(n=2^14, r=8, p=1, 사용자마다 16바이트 소금)로 저장하고 응답과 기록에 싣지 않는다. 확인은 `hmac.compare_digest`로 하고 없는 이메일도 같은 계산을 한다. 로그인 실패 횟수는 서버 프로세스 메모리에서 이메일마다 센다(로그인에 성공하면 지운다).
- 초대 토큰은 32바이트 난수(URL에 쓸 수 있는 글)이고 sha256 값만 저장한다. 7일 뒤 만료되고 한 번만 쓸 수 있다. 토큰 원문은 만든 응답에만 한 번 나간다.
- 저장: 메모리 모드는 프로세스 안, SQL 모드는 `v3_users`, `v3_memberships`, `v3_invites`(마이그레이션 `0005_v3_accounts`). 조직 레코드는 `v3_records`(kind `orgs`)에 그대로 둔다.

### 체험하기 (`X-Demo-View: 1`)

- 시연 조직은 시드 조직 가운데 운영 조직을 뺀 다섯 곳(`org_hanbit`, `org_daon`, `org_saegil`, `org_namhae`, `org_miso`)이고 조직 레코드에 `demo: true`가 붙는다. 운영 조직 `org_admin`은 관리자 화면을 여는 조직이라 시연 조직이 아니다(`demo: false`).
- `X-Demo-View: 1`과 `X-Org-Id`(시연 조직)만 있으면 `X-User-Id` 없이 8장의 경로를 부를 수 있다. **GET만 받는다**(그 밖은 403 `DEMO_READ_ONLY`). `X-Org-Id`가 시연 조직이 아니면 403 `NOT_DEMO_ORG`. 이 헤더가 있으면 `X-User-Id`가 있어도 체험하기로 다룬다.
- 체험하기 요청의 서비스 함수는 시연 조직만 보이는 읽기 전용 저장소·근거 레지스트리(`app/v3/demo/scope.py`)로 돈다. 실제 조직, 그 흐름·진단, 실제 배출 기업이 시연 업체와 나눈 대화, 실제 조직이 가진 근거는 어떤 경로로도 보이지 않는다. 시연 업체의 허가증·매입 기준 근거(doc)는 실제 배출 기업에도 열려 있을 수 있는데, 체험하기에서는 `access`에서 실제 조직을 지우고 보여 준다. 저장소에 쓰려 하면 403 `DEMO_READ_ONLY`이고 뷰를 만들 때의 재확인(쓰기)도 하지 않는다.
- `/admin/*`는 시연 조직이 관리자가 아니므로 403 `FORBIDDEN`이다.

### 시연 되돌리기 (bff 모드)

- `POST /demo/reset`은 관리자(`org_admin`의 구성원)만 부른다(8장 "관리자 조직"과 같다. 조직이 하나도 없는 빈 저장소의 첫 시드는 키만 있으면 부를 수 있다).
- bff 모드에서는 **시연 조직의 자료만** 지우고 다시 올린다: 시드 조직(운영 조직 포함)의 조직·흐름·업체 프로필, 시연 배출 기업의 진단(스레드, 질문, 승인, 문서, 실행 기록), 시드 조직만 볼 수 있는 근거. 실제 조직의 조직·흐름·진단·근거, 사용자와 소속, ID 카운터는 그대로다. 시연 업체 근거 가운데 실제 조직에 열린 것은 지우지 않고 다시 올릴 때도 그 조직들을 열어 둔다(내용이 같으면 판도 그대로다).
- 다시 올린 뒤 시연 배출 기업 `org_hanbit`의 진단 한 건을 규칙 정책으로 승인 대기(`awaiting_approval`)까지 돌려 둔다. 질문에는 시연 답(`seed.DEMO_ANSWERS`: 포장 필름 수분 2.0%, 가상 값)으로, 그 밖에는 "모름"으로 답한다. 응답에 `demoSessionId`, `demoSessionStatus`가 더 붙는다.
- 운영자는 HTTP를 거치지 않고 `python -m scripts.v3.accounts_admin reset-demo`로도 되돌린다(관리자 구성원이 아직 없는 처음 배포). header·token 모드의 되돌리기는 전처럼 저장소 전체를 비운다.

## 10. 작업 규칙

- 커밋 작성자는 `Jeonsubb <glvlglvl01@naver.com>`이다. 커밋 메시지는 한국어로 쓰고 `feat:`, `fix:`, `test:`, `docs:` 접두어를 붙인다.
- 로컬에서는 네이티브 확장을 쓸 수 없다. 핵심 모듈과 그 테스트는 표준 라이브러리와 pytest만으로 돌아야 한다.
- 테스트 실행: `PYTHONPATH=<pydeps>:. python3 -m pytest -q tests/v3` 와 `python3 -m ruff check app/v3 tests/v3` (줄 길이 100).
- 자기 담당 경로 밖의 파일은 고치지 않는다. 계약을 바꿔야 하면 이 문서에 적고 보고한다.
- 비밀값을 코드, 테스트, 문서에 넣지 않는다. 시연 데이터의 회사와 수치는 모두 가상이라고 표시한다.
- 화면과 메시지 문구는 한국어로 쓴다. 이름 뒤 조사는 `app/v3/textutil.py`의 `josa(word, "은는")`(`"이가"`, `"을를"`, `"와과"`, `"으로"`)로 붙인다. "은(는)", "이(가)" 같은 표기를 쓰지 않는다.
