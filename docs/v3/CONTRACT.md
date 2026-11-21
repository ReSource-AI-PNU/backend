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
| `app/v3/live.py` | 실시간 진행: 진행 기록 쓰기(`Recorder`, `Turn`), 실행 자리(heartbeat), SSE 조각(8장 "실시간 진행") |
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

- **출처는 화면 이름으로 나간다.** 조직에게 근거를 내보낼 때(`search.shown`) `source`는 사람이 읽는 이름(`names.source_label`)이고, 저장된 글은 `sourceRaw`에 있다. 도구가 만든 근거(`calc`, 등급 `model`)에는 `tool`(기계 ID, 관리자·기술 정보용)과 `toolLabel`(예: `judge_route` → "경로 판정·매입 기준 대조", `optimize_assignment` → "최적 배정 계산", `cost_carbon` → "비용·탄소 계산")이 붙는다. 화면은 `source`와 `toolLabel`만 보이고 도구 ID를 쓰지 않는다. 실행 기록(`events[]`)의 도구 호출에도 `detail.toolLabel`이 붙는다. 저장된 근거는 바꾸지 않는다(내용이 바뀌면 판이 올라간다).

ID는 `app/v3/ids.py`의 함수로만 만든다.

### 단위

단위 문자열은 다음만 쓴다: `원`, `원/kg`, `원/t`, `원/월`, `원/회`, `원/km`, `원/tCO2e`, `kg`, `t`, `kg/월`, `t/월`, `%`, `km`, `tkm/월`, `m`, `m3`, `kg/m3`, `tCO2e`, `kgCO2e`, `tCO2e/월`, `kgCO2e/kg`, `kgCO2e/tkm`, `tCO2e/t`, `회`, `회/월`, `일`, `개`, 그리고 자원순환 효과(2026-11-19)의 `kgCO2e/t`, `GJ`, `GJ/월`, `GJ/t`, `MMBtu/short ton`, `t/t`, `kg/t`.

`원/km`(차량 운행 단가), `회`(회차 수), `원/tCO2e`(배출권 단가)는 환산 상대가 없는 단위다. 같은 단위끼리만 비교한다.

`units.convert(value, from_unit, to_unit) -> float`는 같은 차원끼리만 바꾸고(예: `원/t` ↔ `원/kg`, `t` ↔ `kg`, `tCO2e` ↔ `kgCO2e`, `kgCO2e/kg` ↔ `tCO2e/t` ↔ `kgCO2e/t`, `MMBtu/short ton` ↔ `GJ/t`(× 1.05505585262 ÷ 0.90718474), `kg/t` ↔ `t/t`, `m` ↔ `km`), 못 바꾸면 `UnitError`를 낸다. `units.same_value(a, unit_a, b, unit_b) -> bool`은 환산한 뒤 상대 오차 1e-6(절대 1e-9)으로 비교한다.

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
| ⑥ | `optimize_assignment` | 흐름과 업체 배정. 회사 측 월 순비용 − 내부 탄소 가격 × 탄소 변화를 최대화한다(2026-11-19). 탄소 변화는 처리 방식이 바뀌어 달라지는 배출과 업체로 실어 가는 운송 배출(환경성적표지 트럭 운송 계수 0.192 kgCO2e/ton·km × 월 발생량 × 공장(`origin`) → 업체 차고지 거리)의 합이다. `carbonWeight`(원/tCO2e)를 주지 않으면 배출권 시세(`kau.price`)를 쓰고, 0을 주면 탄소를 목적에서 뺀다. `exclude`(`[{streamId, vendorOrgId}]`)로 업체가 거절했거나 조건을 내지 않은 짝을 뺀다. 배정 후보마다 허가 근거 수준(`permit`)을 보고, 허가 업종이 처리 방식과 맞지 않거나 공공 허가 명부와 어긋나면 그 짝은 배정하지 않고(`output.permitExcluded`), 업체 입력뿐이면 조건부로 둔다. 완전 탐색으로 풀면 탄소를 가장 많이 줄이는 배정(`output.greenest`)과의 차이를 남긴다. **회사 기록 보정(2026-11-22):** `history`(`{approval, observations: [{streamId, vendorOrgId, rel, evidence}]}`)를 주면 `approval`이 그 배출 기업의 판정 반영 승인 human 근거(`meta.memoryApproval`)인지 확인하고 그 값을 보정 강도 k로 쓴다. 업체 선택지마다 계층형 축소 추정(같은 업체 검수 → 이 회사의 이 폐기물 검수 → 0, `app/v3/judge/personal.py`)으로 예상 단가 = 계약 단가 + 보정 비율 × \|계약 단가\|를 내고, 매각 수익·목적을 그 값으로 계산한다. 고른 배정은 `assignments[].personal`(`{rate, expectedUnitPrice, records, streamRecords}`, claim 키 `sNPersonalRate`·`sNExpectedUnitPrice`), 고르지 않은 후보는 `output.personalCandidates`에 남는다. 승인을 확인하지 못하면 보정하지 않고 확인 항목 `history_approval`(conditional)을 남긴다. 오케스트레이터는 배출 기업이 승인했을 때만 `history`를 넘기고(그 찾기의 검수는 뺀다), 결과 한 장의 `assignments[].personal`과 `alternatives[].personalUnitPrice`에 싣는다(업체 뷰에서는 null) |

OR-Tools를 쓸 수 있으면 CVRP와 CP-SAT를 쓰고, 없으면 같은 답을 내는 순수 파이썬 풀이(작은 문제의 완전 탐색)로 돈다. 두 경로의 결과가 같은지 테스트한다.

`route_extra_cost`와 `optimize_assignment`는 `output.solver`에 쓴 풀이를 남긴다: `"ortools" | "exact" | "greedy"`.

- OR-Tools의 답은 순수 파이썬 검사(용량, 빠진 지점, 최소 물량)로 다시 확인한 뒤에만 쓴다. 풀지 못했거나 검사를 통과하지 못하면 순수 풀이의 답을 쓴다.
- 작은 문제(정류장 8곳 이하, 배정 조합 20만 이하)는 완전 탐색이 정답이다. OR-Tools가 같은 답을 냈을 때만 `"ortools"`, 아니면 `"exact"`라고 남긴다. 어느 쪽이든 수치와 calc 근거 ID는 같다.
- 그보다 큰 문제는 OR-Tools의 답을 쓴다(노선은 탐욕 분할보다 짧을 때만. 아니면 `"greedy"`). OR-Tools가 없으면 노선은 탐욕 분할, 배정은 오류다.
- 풀이는 도구 입력으로 고를 수 없다.

등급 기준은 `app/v3/intake/grade_thresholds.v2.json` 하나다(근거 `assumption:등급기준@v2`. B·C 상한 일부는 APR·DSD·AEPW 공개 규격을 참고했고, `sources`와 칸별 `contaminationRef`·`moistureRef`에 적는다). `identify_stream`과 `intake.grade_model`이 같은 기준을 쓴다.

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

`priceMode`는 `fixed`(기본값) 또는 `negotiated`다. 고정 단가의 0원은 유효한 금액이다. 건별 협의는 `unitPriceKrwPerKg: null`로 저장하며, 담당자가 견적을 내기 전에는 자동 견적과 금액 합계에서 제외한다.

수동 허가 품목의 `entrySource`는 `manual`이다. 목록에서 고른 품목은 분류번호 `code`도 저장한다. 수동 품목의 `materials`가 비어 있으면 해당 품목을 물량 매칭에 사용하지 않는다. 기존 문서 품목의 빈 재질 목록은 그 품목의 모든 재질을 뜻한다. 허가 품목 또는 매입 기준이 있으면 문의 후보군에 포함되고, 실제 문의와 확정 여부는 물량별 조건과 허가증 원본 대조로 판단한다.

문서 추출의 표준 등급은 `A`, `B`, `C`, `등외`다. 담당자는 수동 행에 재질과 등급을 직접 적을 수 있다.

흐름의 `pretreatHoursPerMonth`·`laborCostKrwPerHour`·`reuse`는 순비용 계산에만 쓰는 배출 기업의 입력이다(없으면 `null`). `PUT /orgs/{orgId}/streams/{streamId}`로 넣고, 넣은 값은 `human` 근거로 등록된다. 업체에는 가지 않는다(업체가 받는 흐름 목록과 뷰에 없다).

- `pretreatHoursPerMonth`는 0 이상(시간/월), `laborCostKrwPerHour`는 0보다 큰 값(원/시간)이다. 작업 시간이 있을 때만 전처리 인건비를 계산하고, 시간당 인건비를 비우면 최저임금 기준값(`ref:최저임금:시간급@2026`)으로 계산한다. 작업 시간을 `null`로 보내면 둘 다 지운다.
- `reuse`는 `virginPriceKrwPerKg`(0보다 큰 값), `reusableKgPerMonth`(0보다 큰 값)가 함께 있어야 하고 `regrindCostKrwPerKg`는 비우면 0이다. `null`로 보내면 지운다. 틀린 값은 422 `INVALID_VALUE`다.
- 담당자 질문에 현행 처리비만 답했으면(`field: "currentCost"`) `currentTreatment.methodAssumed`가 `true`로 남는다. 처리 방법은 묻지 않았으므로 혼합 소각으로 보았다는 뜻이다.
- **지금 팔고 있는 경우(2026-11-19):** 재활용 업체에 팔고 돈을 받으면 현행 처리를 `{"method": "재활용 업체 판매", "sale": true, "salePriceKrwPerKg": 200, "evidence": ["human:..."]}`로 적는다(`unitCostKrwPerKg`는 없다). 계산은 처리비의 반대 부호(−판매 단가)로 해서 현행 월 손익이 `+판매 단가 × 월 발생량`이 되고, 배정의 "지금 방식 그대로"도 그 손익을 넘는 제안만 고른다. 판매 단가는 claim `currentUnitPrice`(현행 판매 단가, 원/kg, 양수)로 남고 `currentUnitCost`처럼 배출 기업만 본다. 탄소는 현행을 재활용으로 보아 견준다. 질문 답은 "판매"·"팔아요"처럼 판다는 말이 있으면 판매 단가로 읽는다(`current_cost.says_sale`. "업체가 받는다"만으로는 판매로 보지 않는다).

## 7. 화면용 진단 뷰

`service.get_view(session_id, org_id) -> dict`가 돌려주는 모양이고, 화면은 이것만 그린다. 숫자와 조항은 모두 claim 객체로 온다.

```json
{
  "session": {"id": "ses_1", "emitterOrgId": "org_hanbit", "status": "idle | running | awaiting_answers | awaiting_approval | confirmed", "version": 3},
  "viewer": {"orgId": "org_hanbit", "role": "emitter"},
  "stages": [{"stage": 1, "key": "identify", "label": "식별·분류", "status": "waiting | running | done | needs_check"}],
  "streams": [{"id": "st_sprue", "name": "...", "material": "PP", "origin": "PIR", "form": "...",
               "monthlyKg": {"claim"}, "grade": {"value": "A", "basis": "measured | photo_estimate", "evidence": ["..."]},
               "currentTreatment": {"method": "혼합 소각", "unitCost": {"claim"}, "sale": false, "unitPrice": null, "assumed": true},
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
             "assumptions": [{"id": "assumption:...", "label": "현행 혼합 소각 처리비 (폐플라스틱)", "value": 250, "unit": "원/kg"}], "sensitivity": [{"label": "현행이 위탁 재활용일 때", "difference": {"claim"}}],
             "outsideVendors": {"source": {"name", "url", "asOfRange", "note", "radiusKm", "treatment2024": {"name", "url", "asOf", "published"}},
                                "streams": [{"streamId", "codes": [{"code": "51-03-01", "name": "폐합성수지류(…)"}], "total", "confirmedTotal", "reason",
                                             "changed": [{"field": "material", "label": "재질", "before": "PP", "after": "알루미늄"}], "removed": false,
                                             "candidates": [{"id": "pv_…", "name", "address", "business", "match": "confirmed | check", "distance": {"claim"},
                                                             "matchedCodes": [], "codes": [], "permitDate", "phone", "agency", "asOf", "evidence": ["ref:허가업체명부:…"], "note",
                                                             "treatment2024": {"tonsPerYear": {"claim", "unit": "톤/년"}, "capacityTonsPerDay": {"claim", "unit": "톤/일"} | null, "sheet": "8-가. 재활용처리(중간)", "row", "returnedOn", "asOf": "2024-12-31", "evidence": ["ref:처리업체현황:…"]} | null}]}]} | null},
  "questions": [{"id": "q_1", "batch": 1, "streamId": "st_film", "field": "moisturePct", "text": "...", "answered": false}],
  "approvals": [{"threadId": "th_1", "emitter": false, "vendor": false, "blocked": false, "blockedReason": null}],
  "documents": [{"id": "doc_1", "kind": "request | plan", "title": "...", "text": "... {{c1}} ...", "claims": [{"claim"}], "validation": {"ok": true}}],
  "events": [{"seq": 1, "at": "...", "actor": "company_agent | vendor_agent | validator | tool | system", "kind": "message | tool_call | validation | handoff | approval",
              "threadId": "th_1", "summary": "...", "detail": {}}]
}
```

`{"claim"}`은 `{"id", "kind", "value", "unit", "evidence"}` 객체다. 값이 없으면 `null`을 넣고 화면은 "확인 필요"를 보여 준다.

- `session.status`의 `running`은 그 진단의 실행 자리가 살아 있다는 뜻이다(8장 "실시간 진행"). 뒤에서 도는 실행이 아직 진단을 고치기 전에도 `running`이고, 저장된 상태가 `running`이어도 실행 자리가 비었거나 멈췄으면(프로세스가 죽음) `idle`로 보인다. `GET /sessions`의 `status`도 같다.
- `events[].kind`에 `error`가 더해졌다. 진행 중 오류(`detail.error`: 오류 종류 이름이나 코드, `detail.runId`)와 멈춘 실행 자리를 이어받은 일(`detail.reason: "stale"`)이다. 업체가 보는 뷰에는 나오지 않는다.

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

- **담당자가 넣은 이름은 믿지 않는 자료다(`app/v3/untrusted.py`).** 흐름 이름, 혼입물 이름, 회사 이름에는 지시문, 링크, 연락처, 근거 없는 숫자·조항이 섞일 수 있다.
  - 흐름 이름은 다른 조직에 가지 않는다. 메시지, 문서, 닫는 이유, 실행 기록 문장, 상대와 나누는 calc 근거의 이름과 입력, 두 에이전트의 모델 입력에는 **보이는 이름**(`stream_label`)을 쓴다. 재질 + 종류(+ 종류와 다른 가공 형태)의 정해진 낱말로만 만든다. 종류는 담당자가 넣은 이름·공정에서 정해진 낱말(불량, 스프루·런너, 퍼지, 필름, 마대, 분쇄 등)을 찾아 고르고(찾은 낱말만 쓰고 이름 글은 옮기지 않는다), 없으면 형태(분쇄품, 압축품, 펠렛, 포장 필름), 출처(PIR이면 공정 스크랩), 품목 순으로 정한다. 예: "PP 사출 불량품", "PP 스프루·러너", "PP+ABS 퍼지 덩어리", "LDPE 포장 필름", "PP 불량품(분쇄)", "PP 공정 스크랩", 정해진 낱말이 없으면 "폐기물". 흐름 ID 같은 무작위 표시는 붙이지 않는다.
  - 같은 조직에 보이는 이름이 같은 흐름이 둘 이상이면 저장할 때(`put_stream`) 뒤에 차례 번호를 붙여 흐름의 `label`에 둔다(`numbered_label`. 처음 것은 번호 없이, 다음부터 "PP 불량품 2", "PP 불량품 3"). 한 번 정한 번호는 재질·형태·이름의 낱말이 바뀌지 않는 한 그대로다. 저장된 `label`은 지금 칸으로 만든 이름이거나 그 뒤에 번호만 붙은 것일 때만 쓰고, 다른 글이면 버리고 다시 만든다.
  - 진단 뷰의 `streams[].label`이 보이는 이름이다. 배출 기업의 뷰에서 `name`은 원래 이름이고, 업체의 뷰와 업체가 받는 흐름 목록(`GET /orgs/{orgId}/streams`)에서는 `name`도 보이는 이름이다. 판단 그래프의 흐름 노드도 같다.
  - 혼입물 이름은 정해진 낱말(종이 라벨, 금속 인서트, PVC 등)이 들어 있으면 그 낱말로, 아니면 정리한 글(숫자 낱말 없이 16자까지)로, 남는 것이 없으면 "기타 혼입물"로 보인다. 업체의 뷰 `streams[].contaminants`, 경로 판단의 `mixture` 검사 문장, 두 에이전트의 모델 입력이 이 이름을 쓴다.
  - 회사 이름은 정리한 글(`safe_text`)로 보인다: 괄호·대괄호 속 덧붙임(법인 표시 "(주)" 등은 남김), 링크, 이메일, 전화·주민·사업자 번호, 지시문 표지(`---`, `===`, 줄바꿈, `:`, `SYSTEM` 등) 뒤의 글, 연락처를 알리는 낱말(담당, 문의 등)과 직함이 붙은 사람 이름을 빼고 40자까지. `threads[].vendorName`, 문서의 보내는 곳·받는 곳, 모델 입력이 이 이름을 쓴다.
  - 근거 검증기가 이름에 든 숫자를 수치로 세지 않는 예외(`names`)는 보이는 이름에만 준다.
  - 업체의 자동 거절 조건 대조는 원래 글로 한다(판단은 그대로이고, 문장에는 보이는 이름이 들어간다).

- **claim의 `label`과 `role`(선택 필드).** `{"id", "kind", "value", "unit", "evidence", "label": "매입 단가", "role": "unitPrice"}`. 이름을 정하지 못한 claim에는 두 필드가 없다. 근거 검증기는 이 두 필드를 보지 않는다. 상대 조직이 보낸 `label`은 쓰지 않고 받는 쪽에서 `role`과 키로 다시 만든다(이름 칸에 숫자를 실어 보내지 못한다).

  | `role` | `label` | 비고 |
  |---|---|---|
  | `unitPrice` | 매입 단가 | 값이 0 이상인 단가 |
  | `treatmentFee` | 처리비 | 값이 음수인 단가. **값은 음수 그대로다.** 화면은 크기로 보여 준다 |
  | `currentUnitCost` | 현행 처리 단가 | 내는 돈을 양수로 적는다 |
  | `currentUnitPrice` | 현행 판매 단가 | 지금 팔고 있을 때 받는 돈(양수). 이때 `currentTreatment.unitCost`는 `null`, `sale`은 `true`다 |
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

⑥ 단가 협상(2026-11-19, `App(negotiate=True)`가 기본이고 고정 평가셋과 협상 전 흐름 시험은 끈다)은 코드가 쓰는 메시지지만 숫자 claim이 있다:

| `type` | 보내는 쪽 | 언제 |
|---|---|---|
| `question` | 배출 기업 | 최적안이 나온 뒤 합의하기 전에 대화마다 한 번. 배정된 폐기물마다 요청 단가(`negotiate_ask` 계산 근거: 제안 단가 + |제안 단가| × 요청 폭 가정값 `negotiation.askPct`)를 claim으로 싣고 `meta.priceAsks`(`[{streamId, claimId}]`)를 단다. `model.name`은 `단가 협상(요청 계산)` |
| `offer` · `conditional` | 업체 | `meta.priceAsks`를 받으면 모델을 거치지 않고 답한다. 단가는 `negotiate_reply` 계산 근거로 요청 단가와 가드레일 상한 가운데 업체에 덜 불리한 값이고 처음 제안보다 나빠지지 않는다(상한이 없으면 처음 제안 그대로). 상한 값은 계산 근거의 입력에 싣지 않는다. 앞서 낸 조건 문장과 요일은 그대로 싣는다. `model.name`은 `단가 협상(업체 범위 계산)` |

협상으로 단가가 바뀌면 회사 측은 그 매입 기준의 단가와 근거(협상 단가 근거를 맨 앞에)를 바꾼 업체 프로필로 비용 도구와 최적 배정을 다시 계산한다(`orchestrator._public`). 결과 한 장의 `assumptions`에 `negotiation.askPct`가 붙는다.

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
- `streams[]`: `orgId`, `label`(보이는 이름), `process`, `wasteItem`, `lot`, `contaminants`, `monthlyKgBasis`, `contamination`·`photoContamination`·`moisture`(claim), `contaminationBasis`·`moistureBasis`(그 값의 출처: `measured` 측정 | `answered` 담당자가 질문에 답한 값 | `null`), `unknownFields`, `grade.label`. 담당자 답의 claim ID는 `<흐름>_answeredContaminationPct`·`<흐름>_answeredMoisturePct`이고 이름은 "오염도(담당자 답)"·"수분(담당자 답)"이다("(측정)"이 아니다). 담당자 답도 매입 기준과 대조한다: 상한 안이면 조건부(반입 검수에서 확인), 상한을 넘으면 그 기준은 `unmet`이다. 업체가 보는 뷰에서는 `currentTreatment`가 `null`이고, `name`은 보이는 이름, `contaminants`는 보이는 혼입물 이름이며, `unknownFields`에 `currentCost`가 없다.
- `threads[]`: `emitterOrgId`, `assignedStreamIds`(최적안에서 이 업체에 배정된 흐름), `agreedSeq`, `vendorModel.configured`·`vendorModel.used`, `vendorAuthor`. `messages[]`에는 `author`, `model`과 `meta`(`asks`, `excluded`, `reason`, `terms`의 `items[{streamId, claimId}]`·`days`·`conditions`)가 붙는다. 조건의 값은 `claims`에만 있다.
- `graph.edges[]`: `kind`(`permit | criterion | assignment | excluded | regulation | document`). `excluded` 엣지의 `evidence`에는 허가 품목 때문에 뺀 경우 허가증 근거가 실린다.
- **업체가 보는 뷰의 `graph`.** 자기 매입 기준 노드(`type: "criterion"`)와, 흐름을 그 기준과 대조한 엣지(`kind: "criterion"`, 흐름 → 기준은 `met | conditional | unmet`과 첫 번째 확인할 것, 기준 → 업체는 "매입 기준표"), 최적안 배정 엣지(`kind: "assignment"`)가 들어간다. 같은 판단 도구(`judge_route`)의 결과에서 업체의 매입 기준·허가 품목과, 대화에서 업체에 열린 흐름 자료(오염도, 수분, 형태, 혼입물, 물량의 근거)를 대조한 검사만 쓴다. 배출 기업의 현행 처리와 비용·탄소에 관한 검사, 규정 엣지(`regulation`), 다른 업체의 기준은 넣지 않는다. 엣지의 `evidence`는 업체가 열 수 있는 근거뿐이다.
- `result`: `referencePrices[{key, material, form, label, asOf, price: claim, note}]`(가격조사 참고 시세. 손익 계산에는 쓰지 않는다), `carbonInObjective`, `carbonPrice`(claim, 배정에 쓴 내부 탄소 가격 원/tCO2e. 2026-11-19), `evidence`. `assignments[]`에는 `vendorName`, `threadId`, `carbonValue`(claim, KAU 환산), `currentAssumed`가 붙고, `checks[]`는 `{key, status, text, evidence}`에 업체가 내건 조건이면 `claims`가 붙는다(`text`의 자리표시자를 그 claims로 그린다). `alternatives[]`에는 `vendorName`, `unitPrice`(claim)가 붙는다. `sensitivity[]`는 흐름별이고 `streamId`가 붙는다.
- `result.circular`(2026-11-19, 배출 기업 뷰에만): 자원순환 효과와 네 시나리오. `scenarios[]`는 `A`(흐름별 최적안, 고른 배정) · `B`(묶음 없이 개별 처리: 업체로 가는 폐기물마다 혼자 고른 기준의 최소 물량을 채우는 배정 가운데 같은 목적이 가장 큰 것. 완전 탐색일 때만, 아니면 `available: false`) · `C`(현행 유지, 기준선) · `D`(전부 매립, 비교용) 순서다. 시나리오마다 `money`(현행 대비 월 손익 claim. C·D는 null), `circularKg`(공정 재투입·재활용 업체로 가는 물량), `flows[{route, kg}]`, `totals.{ghg|energy|virgin}`(`status`: `estimated`·`partial`(산정한 폐기물만 더한 부분합)·`hold`·`zero`, `total`, `countedKg`, `heldKg`, `heldStreams`), `virginByMaterial[{material, total}]`, `baselineEmission`(C만: 현행 처리 단계 배출, 국내 계수), `rows[]`가 있다. `rows[]`는 폐기물마다 `route`(`reuse`·`recycle`·`pyrolysis`·`landfill`·`keep`·`unknown`), `baseline`(`incineration`·`landfill`·`recycle`), 세 지표 `{status, method(literature|site), reason, value: claim, evidence}`, `unconfirmed[]`(숫자 없는 문장), `explain{effect, why, conditions, basis}`다. 세 지표는 단위(`tCO2e/월` 감축량은 양수가 감축, `GJ/월` 전 과정 에너지 순절감, `kg/월` 신재 원료 대체량)가 달라 합치지 않는다. 자료가 없으면 0이 아니라 `hold`다. 문헌 계수(근거 시드 `circular.json`, 기준값 키 `circular.<지표>.<재질>.<경로>[.<기준선>]`)는 소비 후 폐플라스틱(PCR)·단일 재질에만 쓰고, 계수에 들어 있는 수율·신재 대체·잔재 처리·평균 수거 운송을 다시 곱하거나 더하지 않는다. 이미 재활용하던 물량의 거래처만 바뀌면 `zero`다. `bundle{available, streams, kg}`는 A에서는 재활용 경로로 가지만 B에서는 못 가는 폐기물, `sources[]`는 쓴 ref 근거의 이름·출처·국가·연도·표·단위·기준선·적용 조건·주의다. 열분해는 공정 에너지·수율·열분해유 용도 근거가 없어 세 지표와 처리 단계 탄소를 모두 산정 보류한다. 탄소 환산액(`carbonValue`)은 배정 판단용 내부 탄소 가격 환산이며 받거나 팔 수 있는 돈이 아니다.
- 2026-11-19부터 `assignments[]`에 다음이 붙는다(배출 기업만. 업체 뷰에서는 `null`). `carbon`은 처리 방식 탄소 변화(`carbonProcess`)와 운송 배출(`carbonTransport` = 트럭 운송 계수 × 월 발생량(t) × 공장 → 업체 거리)의 합이고, `transportKm`은 공장 → 업체 차고지 거리(km)다. `carbonValue`는 같은 탄소에 내부 탄소 가격을 곱한 값이다. `permit`은 그 배정 경로(업체 · 폐기물 품목·재질 · 처리 방식)의 허가 확인 상태다(2026-11-19 개정, `judge/permits.py`): `{level, label, status, text, evidence, source, wasteItem, materials, process, need, business, check, document, ignored, todo}`. `level`은 `verified`(허가증 대조 완료, 충족: 이 배출 기업 담당자가 그 업체가 올린 허가증 원문에서 품목과 처리 방식을 대조해 맞다고 기록함. `check`에 문서·쪽·범위·누가·언제), `pending_review`(허가증 검토 대기, 조건부: 업체가 허가증을 올려 AI가 읽었지만 대조 기록이 없음. `document`에 AI가 읽은 품목·업종·상호·소재지와 쪽), `registered`(등록 품목 일치 · 허가증 원본 확인 필요, 조건부: 업체 입력, 차고지 근처 같은 상호의 공공 명부, 원문 없는 예전 문서 값뿐), `mismatch`(이 경로로 진행 불가, 불충족: 대조 결과 품목·처리 방식이 다르거나, 업체가 확인한 허가증 업종·공공 명부가 처리 방식과 어긋남. 배정하지 않는다). `source`는 실제 근거 수준(`check | document | registry | typed`), `todo`는 대조 전 `{submit, items}`(제출받을 문서, 확인할 항목), `ignored`는 쓰지 않은 허가증(상호·소재지가 다름)과 이유다. 다른 업체·다른 처리 방식·다른 배출 기업의 기록이나 다른 조직의 doc 근거로 상태를 채우지 않는다. 공공 명부는 업체 차고지가 있을 때만 대조하고 맞아도 `registered`다. 이전 결과의 `registry | document | self`는 원문 대조 기록이 없는 상태로 읽는다. 업체 뷰의 `check`에는 담당자 이름(`userName`)과 대조 근거 ID가 없다. 진행 불가 경로는 `assignments[].blockedRoutes[]`(`{vendorOrgId, vendorName, permit, note}`, 손익 없음)에 실리고 `alternatives[]`에는 넣지 않는다. `alternatives[].permit`은 그 업체로 보낼 때의 상태다. `approvals[]`에는 `permits`(배정된 흐름마다 `{streamId, ...permit}`, 결과표와 같은 값)와 `permitHold`(대조 완료가 아닌 경로가 있음)가 붙는다. `result.regulation[].items[]`의 허가 요건(`permit_holder`)은 배정 뒤 배정한 업체 하나로 다시 판정하고 그 상태를 `permit`에 싣는다. 업체가 허가증(`kind: permit`)을 올리면 응답에 `permitDocument`가 붙고 업체 프로필 `permitDocuments[]`(최근 다섯 건)에 검토 대기로 남는다(파일은 남기지 않는다). 승인을 기다리는 진단은 허가 확인 상태가 바뀌면(업로드, 대조, 허가 품목 변경) 최적안과 결과를 다시 계산한다. 같은 내용이 `checks[]`에 `permit_level`로 들어가고, 운송 배출을 계산하지 못하면 `transport_carbon` 검사가 붙는다. `alternatives[]`에는 그 업체에만 보낼 때의 탄소 변화(`carbon`, 운송 포함)가 붙는다. `result.greenest`는 탄소를 가장 많이 줄이는 배정과의 비교다: 같으면 `{same: true, changes: []}`, 다르면 `{same: false, changes: [{streamId, streamName, vendorOrgId, vendorName, route}], monthlyAfter, carbon, moneyGap, carbonGap}`(claim, 차이는 탄소 우선 배정 − 고른 배정). 업체 허가 품목에는 허가증에서 확인한 업종(`business`, 정해 둔 업종 이름만)이 붙을 수 있다.
- `result.assignments[].checks[]`의 `key`: 경로 판단에서 온 `contaminationPct`, `moisturePct`, `contamination_basis`, `moisture_basis`, `form`, `monthly_kg_basis`, `material_basis`, `mixture`, `permit`, `current_assumed`, `current_method`(현행 처리비만 답을 받아 처리 방법을 혼합 소각으로 보았을 때), 비용·탄소에서 온 `transport`, `carbon`, 업체가 내건 조건 `vendor_condition`, 그리고 `price_differs`, `unresolved`, `skipped`, `handoff`, 적재 검사 `min_volume`, `loading`, `loading_volume`이다.
  - **적재 검사(`check_loading`)는 업체가 정해진 배정마다 늘 들어간다.** `min_volume`(최소 물량)과 `loading`(무게·부피 적재), 밀도나 부피 용량을 몰라 부피를 확인하지 못했으면 `loading_volume`. 다른 검사와 달리 충족(`met`)한 것도 나간다. `claims`에 합친 월 물량·최소 물량(`min_volume`), 월 수거 횟수·한 번 수거량·차량 적재 한도(`loading`)가 붙는다. 그 업체에 실제로 배정된 흐름만 합친 값이다. 충족하지 못한 적재 검사가 있으면 그 배정은 `calculated`가 아니다.
  - `contamination_basis`, `moisture_basis`: 값은 매입 기준 상한 안이지만 측정값이 아니라 사진 추정이나 담당자 답이다(조건부. "반입 검수에서 확인").
  - `handoff`: 이 흐름을 맡기로 합의한 대화가 담당자에게 넘어가 배정을 뺐다(`status`는 `needs_information`, `vendorOrgId`는 `null`).
- `approvals[]`에는 승인할 수 있는 대화(`agreed`, `confirmed`)만 있다. 담당자에게 넘어간(`handoff`) 대화의 승인 칸은 나가지 않는다. 합의한 뒤에 배정된 흐름의 자료(재질, 물량, 측정값 등)가 바뀌면 `blocked`가 되고 `blockedReason`에 `STREAM_CHANGED`가 적힌다. 막힐 때 앞서 한 승인은 양쪽 모두 지워진다. `approvals[].clearedApprovals`(배열, 새 항목)에 그때 지운 승인 쪽(`emitter`, `vendor`)이 있고, 아무도 승인하지 않았으면 빈 배열이다. 화면은 이 배열이 비어 있지 않을 때만 "앞서 한 승인을 지웠다"고 알린다. 막힘이 풀리면 빈 배열이다.
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
- `questionBatch`(새 항목, 배출 기업에게만): 답을 기다리는 질문 묶음의 요약 `{batch, maxBatches, maxPerBatch, count, open, last, intro, note}`이고, 기다리는 질문이 없으면 `null`이다. 질문은 묶음당 최대 3개, 진단당 최대 2묶음이다. 화면의 머리 문장은 `intro`를 쓴다(첫 묶음: "찾기 전에 n가지를 여쭙겠습니다.", 둘째 묶음: "n가지만 더 여쭙겠습니다. 이번이 마지막 질문입니다."). 첫 묶음 뒤에도 질문이 올 수 있으므로 첫 묶음에 "…만"이라고 쓰지 않는다. `note`는 첫 묶음일 때 "답을 보고 한 번 더 여쭐 수 있습니다."이다.
- `unresolved[{streamId, field}]`: 질문 한도를 다 쓰고도 모르는 값
- `approvals[]`: `streamIds`, `review`(아래 "계약 검토")
- `documents[]`: `threadId`, `streamIds`, `rendered`(자리표시자를 값으로 바꾼 글), `createdAt`. `kind`는 `request`(수거·처리 요청서), `plan`(이행 계획서), `contract`(위탁 계약서 초안, 2026-11-19)다. 셋 다 양쪽 승인 뒤에만 만들고 근거 검증기를 거친다

### 계약 검토 (`approvals[].review`, 2026-11-19)

두 AI가 합의한 조건을 사람이 승인하기 전에 다시 대조하는 세 번째 에이전트 "계약 검토 담당"의 결과다(`app/v3/agents/reviewer.py`). 어느 편도 아니고 모델을 부르지 않는다(규칙으로 대조해 같은 입력이면 늘 같은 결과). 양쪽이 같은 값을 본다. 뷰를 만들 때마다 지금 결과 한 장과 업체 프로필로 다시 만든다.

```json
{"status": "blocked | needs_check | clear",
 "summary": "합의 조건을 항목마다 대조했고 막을 항목은 없습니다. 승인 전에 담당자가 확인할 것: …",
 "items": [{"key": "pickup", "title": "수거 가능 지역", "status": "block | check | ok", "text": "…", "pilot": true}],
 "reviewer": {"label": "계약 검토 담당", "kind": "rules"}}
```

- 항목(`key`, 이 순서): `evidence`(근거 대조. 승인 전 근거 검증이 막았으면 block), `terms_range`(합의 단가·요일이 업체가 정한 범위 안인지. 밖이면 block, 범위를 정하지 않았으면 check), `permit`(결과의 `permit_level`), `pickup`(결과의 `pickup_area`, 없으면 `pickup_route`. 둘 다 없으면 check), `volume`(`min_volume`·`loading`), `transport`(운반비 부담 주체를 모르면 check), `inspection`(측정하지 않은 값이나 업체가 건 검수 조건이 있으면 check), `law`(결과 `regulation`에서 이 대화의 폐기물. 불충족이면 block, 담당자 할 일이 있으면 check), `history`(같은 두 회사 사이의 지난 검수, `app/v3/memory.py`. 검수 기록이 없거나 배출 기업이 회사 기록 사용을 껐으면 항목이 없다. 합의한 등급이나 단가보다 낮게 나온 건이 절반 이상이고 3건 이상이면 check, 그 밖에는 ok. `evidence`에 검수 근거 ID를 싣는다), `pilot`(같은 두 회사가 다른 찾기에서 확정한 거래가 없으면 `pilot: true`. 계약서 초안에 한 달 시범 기간이 들어간다).
- 결과 확인 항목의 상태는 `met → ok`, `conditional`·`needs_information → check`, `unmet → block`으로 옮긴다.
- **block이 하나라도 있으면 양쪽 모두 승인할 수 없다**(409 `REVIEW_BLOCKED`). check는 화면에서 담당자가 확인했다고 표시해야 승인 단추가 켜진다(서버는 막지 않는다).
- 문장에는 숫자와 근거 자리표시자를 쓰지 않는다. 업체가 정한 하한·상한 단가 같은 상대에게 감춘 값도 쓰지 않는다.
- 합의한 뒤 결과 한 장을 만들 때 실시간 진행에 `kind: "review"` 기록(`actor: "contract_reviewer"`, `author: {"side": "reviewer", "kind": "system", "label": "계약 검토 담당"}`, `review: {status, items[{key, title, status, text}]}`)과 실행 기록 `events[].kind: "review"`를 남긴다. 결과가 앞서 남긴 것과 같으면 다시 남기지 않는다.
- **문서는 양쪽이 함께 본다. 배출 기업의 현행 처리 방법이나 그것에서 나온 값은 문서에 없다.** 이행 계획서에 있던 "탄소 변화(현행 혼합 소각 대비…)" 줄은 뺐고, 현행 처리에 관한 검사(`current_cost`, `current_assumed`)는 "반입 전 확인"에 넣지 않는다. 탄소 변화와 그 환산액은 배출 기업만 보는 `result.assignments[].carbon`, `carbonValue`에 있다.
- **업체가 보는 뷰 전체에 현행 처리 방법·현행 단가·그것에서 나온 값이 없다**(`tests/v3/test_fix2_vendor_view_current.py`). 업체 쪽 판단 도구가 현행 처리를 모른 채 가정값으로 낸 계산 근거의 ID(`calc:…:currentUnitCost`, `monthlyNow`, `difference`, `sensitivity*`, `carbonDifference`, `kauValue`, `assumption:current.*`)도 업체 뷰의 실행 기록에서 빼고 `hiddenEvidenceCount`에 센다. 두 에이전트의 claim 풀에도 이 값들은 들어가지 않는다(상대에게 인용할 수 없다).
- `events[].kind`: `thread`, `question`, `document`, `stage`를 더했다
- `events[].detail.hiddenEvidenceCount`: 실행 기록에 실린 근거 ID 가운데 보는 조직이 열 수 없는 것은 뷰를 만들 때 뺀다(상대 쪽 도구 호출의 `detail.evidence` 같은 곳). 뺀 것이 있을 때만 그 개수(정수)가 붙는다. 배출 기업이 보는 뷰와 업체가 보는 뷰 모두 같다. 저장된 기록은 바뀌지 않는다. 진단 뷰 전체에 보는 조직이 열 수 없는 근거 ID는 실리지 않는다(`tests/v3/test_view_evidence_scope.py`).

재학습 데이터 수집(7단계)에서 더한 항목:

- `feedback`: `{"corrections": [...], "outcomes": [...]}`. 이 진단에 쌓인 교정과 실제 결과다. 교정은 배출 기업만 보고, 실제 결과는 그 스레드의 업체와 배출 기업만 본다.
  - 교정: `{"type": "correction", "id": 근거 ID, "sessionId", "orgId", "streamId", "field", "before", "after", "note", "seq", "target": "detector | grade", "photoEvidence": [사진 근거 ID], "recordedOn"}`
  - 실제 결과: `{"type": "outcome", "id", "inspectionId", "revision", "supersedes", "requestId", "reason", "legacy", "sessionId", "threadId", "vendorOrgId", "emitterOrgId", "streamId", "inspectedOn", "actualGrade", "actualUnitPriceKrwPerKg", "actualKg", "note", "weighTicket", "documentName", "evidenceKind": "human | doc", "evidence": {"grade", "unitPrice", "kg"}, "features": {"material", "contaminationPct", "moisturePct", "basis"}, "criterionId", "tablePrice": claim, "offeredPrice": claim, "deviation": claim | null, "createdOn", "createdAt", "recordedOn", "recordedAt", "history": [앞 판]}`. 검수(계량) 한 건이 한 줄이고 `id`는 `inspectionId`와 같다(정정해도 바뀌지 않는다). 정정한 건은 최신 판이 오고 `history`에 앞 판들(`revision`, `reason`, 그때의 값과 근거 ID)이 실린다. `legacy`가 참이면 검수일 키로 만든 2026-11-19 전 기록이고 `inspectionId`는 그 키(`실제결과-<진단>-<흐름>-<검수일>`)다. `deviation`은 실제 단가에서 매입 기준표 단가를 뺀 값이고 calc 근거(`calc:price_deviation:...`)에 연결된다.
- `events[].kind`: `feedback`(교정이나 실제 결과를 기록함)을 더했다. 업체가 보는 뷰에는 나가지 않는다.
- `result.assignments[]`: 모르는 값 때문에 업체를 정하지 못한 흐름은 `status`가 `needs_information`이고 `checks`에 `unresolved`가 붙는다(현행 유지를 "확정"으로 보이지 않게 한다).

관리자 화면(`GET /admin/status`)의 `retrain`(재학습 표):

```json
{"gradeLabels": {"count": 0, "usable": 0, "incomplete": 0, "note": "..."},
 "outcomes": {"count": 0},
 "corrections": {"count": 0, "byField": {"material": 1}, "detectorCandidates": 0},
 "gate": {"allowed": false, "reason": "...", "minRealLabels": 30, "minAccuracy": 0.9, "realLabelCount": 0,
          "evaluated": {"model": "등급 기준값 규칙 v2", "n": 0, "accuracy": null, "label": "실제 등급 기준"}},
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
- token 모드의 오류: 토큰이 없거나 `Bearer` 꼴이 아니면 401 `TOKEN_REQUIRED`, 토큰이 어느 조직의 것도 아니면 401 `INVALID_TOKEN`. `GET /health`와 `GET /trust`만 토큰 없이 답한다. `POST /demo/reset`은 토큰이 없으면 서비스의 규칙(`V3_DEMO_OPEN`, 관리자 조직)대로 답한다.
- `devserver`도 같은 옵션을 받는다: `--auth header|token`, `--org-tokens <JSON 파일>`.
- `GET /admin/status`의 `auth`: `{"mode": "header | token", "demoOnly": true | false}`. 관리자 화면이 시연 전용 모드임을 보여 줄 때 쓴다.
- token 모드의 토큰은 FastAPI와 `devserver` 모두 `V3_ORG_TOKENS_FILE`(JSON을 담은 파일 경로)이 있으면 그 파일에서 먼저 읽고, 없으면 `V3_ORG_TOKENS`에서 읽는다.
- `GET /admin/status`의 `distance`: `{"provider": true | false, "kind": "KakaoDistance | AzureMapsDistance | null", "lastError": "마지막 실패 이유 | null"}`. `provider`가 `false`면 직선 거리에 우회 계수를 곱한 가정값으로 계산한다. `lastError`에 키는 실리지 않는다.
- Entra 인증에는 연결하지 않았다. token 모드는 조직 단위 식별이고 담당자 개인은 가리지 않는다.
- bff 모드(9장): 모든 요청에 BFF 키가 있어야 한다(`GET /health`와 `GET /trust`만 키 없이 답한다). 키는 설정 `V3_BFF_KEY`(Key Vault `v3-bff-key`)로 받고 `hmac.compare_digest`로 견준다. 키가 비어 있으면 모든 요청을 503으로 거절한다. 사용자가 `X-Org-Id`의 구성원이 아니면 403(`X-Org-Id`를 바꿔 다른 조직이 될 수 없다). 그다음의 조직 경계는 위 서비스 함수의 규칙 그대로다. `devserver`는 bff 모드를 받지 않는다(FastAPI만).
- bff 모드에서 실제 업체의 공개 프로필(`GET /vendors/{orgId}`의 허가 품목과 모델 선택)은 그 업체와 스레드가 있는 배출 기업에게만 보인다. 다른 조직에는 404 `VENDOR_NOT_FOUND`다. 시연 업체의 공개 프로필은 전처럼 누구나 본다.
- `GET /admin/status`의 `auth.demoOnly`는 header 모드에서만 `true`다.

| 메서드와 경로 | 내용 |
|---|---|
| `GET /survey` | 내 범위 단가 후보·거래별 문의·공유 사진. 업체 운행비는 업체만 읽음 |
| `POST /survey/quotes` | streamId, vendorOrgId, criterionId로 범위 문의. 같은 조건의 재전송은 기존 문의 |
| `POST /survey/quotes/{id}/{action}` | action=narrow 또는 accept. revision 검사, 범위 확대 금지, 담당자 확인 뒤 단일가 제안·상대 수락. 기존 거래 승인은 별도 |
| `POST /survey/weights` | month=YYYY-MM와 계량 CSV. 월별 합산, 재전송 멱등, 품질 수치 불필요 |
| `POST /survey/photos` | streamId와 사진. 위치 메타데이터 제거 후 비공개 보관 |
| `GET /survey/photos/{id}` | 소유자 또는 선택된 문의 상대만 사진 열람 |
| `POST /survey/photos/{id}` | 소유자가 sharedWith 목록으로 공유·취소 |
| `GET /health` | `{"ok": true}`. 조직 헤더 없이 부른다 |
| `GET /trust` | 공개 신뢰 페이지가 읽는 최신 평가 요약(`docs/v3/eval/latest.json`, 평가 설계 17장). 조직 데이터가 없어 `GET /health`처럼 인증 없이 답한다. 파일을 읽지 못하면 503 `TRUST_UNAVAILABLE` |
| `POST /demo/reset` | 시연 데이터를 처음 상태로 되돌린다. 관리자만(아래 "관리자 조직") |
| `GET /orgs` | 조직 목록. 조직 레코드의 `order`가 작은 것부터(시연 주인공 `org_hanbit`이 맨 앞, 운영 조직이 맨 뒤). `order`가 없는 조직은 그 뒤에 ID 순. token 모드에서는 자기 조직과 대화 상대만 |
| `GET /orgs/{orgId}/streams`, `PUT /orgs/{orgId}/streams/{streamId}` | 흐름 조회와 수정(현행 처리, 전처리 작업 시간·시간당 인건비, 공정 재투입 값 포함. 6장). 현행 처리는 `{method, unitCostKrwPerKg}` 대신 `{method, billing: {kind: "per_trip", krwPerTrip, tripsPerMonth} | {kind: "monthly", krwPerMonth}}`로도 받는다(차량·횟수 단위나 월 정액 계약). 지금 팔고 있으면 `{method?, sale: true, salePriceKrwPerKg}`(0 이상, 원/kg. 방법을 비우면 "재활용 업체 판매")로 받는다. 월 발생량으로 kg당 처리비를 계산해 calc 근거로 남기고, 월 발생량이 없으면 422 `INVALID_VALUE`. 질문 답(`currentCost`)도 "회당 30만원, 월 2회", "월 60만원", "1년에 720만원"을 같은 방식으로 읽는다(`app/v3/current_cost.py`) |
| `GET /orgs/{orgId}/report?from=&to=` | 재활용·탄소 실적 리포트 `{orgId, period, generatedOn, records, totals, planned}`. 배출 기업 본인과 운영자만(업체 403 `WRONG_ROLE`). `records`는 기간(YYYY-MM-DD, 검수일 기준) 안 검수 건마다(정정한 건은 최신 판 하나, `inspectionId`·`revision`·`weighTicket`·`legacy`·`history`·`correctionReason`을 싣는다) 실제 중량·단가(업체 기록 근거)와 금액·탄소 변화(calc), `totals`는 그 합(반출 무게, 매각 수익, 처리비, 탄소), `planned`는 양쪽이 확정한 합의의 한 달 예상(결과 한 장의 배정). 숫자는 모두 claim이다(`app/v3/report.py`). 탄소 변화는 배출계수를 적용한 추정값이고, 검수를 기록할 때 고정한 기준(`records[].carbonBasis`)만 쓴다. 아래 "검수 기록의 탄소 기준"을 본다 |
| `GET /orgs/{orgId}/memory`, `PUT /orgs/{orgId}/memory` | 회사별 기록(`app/v3/memory.py`). 그 회사 본인과 운영자만. 처리 업체의 GET은 `{orgId, role: "vendor", dataOrigin, minRecords, summary: {emitters, inspections, below, deals}, emitters: [{emitterOrgId, emitterName, deals, inspections, inspectionRows(streamName 포함)}]}`(업체가 당사자인 검수와 확정 거래만, 판정에 쓰지 않음)이고 PUT은 403 `WRONG_ROLE`이다. 배출 기업의 GET은 `{orgId, role: "emitter", enabled, dataOrigin, minRecords, summary, streams, deals}`다. `streams[]`는 기록이 있는 폐기물마다 `{streamId, name, inspections, inspectionRows, answers, corrections}`이고, `inspectionRows[]`는 검수 한 건을 합의 때의 등급(고른 매입 기준의 등급)·단가(업체 제안, 없으면 기준표)와 견준 `grade`·`price`(`lower | same | higher | null`)다. `inspections.status`는 `none | few | as_agreed | below`이고 `minRecords`(3)건 미만이면 `few`(참고만), 합의보다 낮게 나온 건이 절반 이상이면 `below`다. `answers[]`는 지난 찾기의 질문 답(폐기물·항목마다 최근 하나, `unknown`·`stillUnknown`·`answeredOn`)이다. 실제 회사는 실제 기록만, 시연 회사는 시연 기록만 본다. 평균을 내거나 판정 숫자를 바꾸지 않는다. 운영 상태(`GET /admin/status`)의 `retrain.personalStrength`는 실제 회사 사이의 검수로 낸 보정 강도 제안(`personal.suggest_strength`, 자동 적용 안 함)이다. PUT `{enabled?: bool, applyToJudgment?: bool}`: `enabled`는 계약 검토(`history` 항목)와 회사 측 AI 맥락에 쓸지 정하고 기록은 지우지 않는다(끄면 판정 반영도 멈춘다). `applyToJudgment: true`는 담당자의 판정 반영 승인을 human 근거(`meta.memoryApproval`, 값 = 보정 강도 3건)로 남기고 `org.memory.applied`에 둔다. 기록 사용이 꺼져 있으면 409 `MEMORY_DISABLED`, 둘 다 없거나 참·거짓이 아니면 422 `INVALID_VALUE`. GET의 `calibration`은 `{applied, strength, proposedStrength, streams: [{streamId, name, records, streamRatePct, vendors: [{vendorOrgId, vendorName, records, ratePct}]}]}`(보정 미리 보기, `app/v3/judge/personal.py`). 계약 검토는 같은 두 회사 사이의 검수만 쓴다 |
| `GET /vendors/{orgId}`, `PUT /vendors/{orgId}` | 업체 프로필(허가, 매입 기준, 노선·차량, 운반 조건, 가드레일, 모델 선택). 노선·차량(`fleet`)의 모양과 검사는 9장 "노선·차량", 운반 조건(`transport`, 노선 밖 공장 수거 포함)은 9장 "운반 조건". `appendPermits: [{code 또는 wasteItem, materials}]`는 수동 품목을 기존 목록에 추가한다. `permits` 전체 교체와 함께 요청할 수 없다 |
| `GET /vendors/{orgId}/permit-catalog` | 업체 본인의 품목 선택용 분류 목록 `[{code, name, wasteItem}]`. `wasteItem: null`인 분류는 직접 입력을 안내한다 |
| `GET /vendors/{orgId}/onboarding`, `POST /vendors/{orgId}/onboarding/messages`, `POST /vendors/{orgId}/onboarding/confirm`, `DELETE /vendors/{orgId}/onboarding` | 업체 온보딩 대화(2026-11-22, `app/v3/onboarding/`). 업체 자신(또는 운영자)만, 다른 조직은 403. 응답 `{orgId, model, messages: [{id, role, text, at, tools: [{name, summary}]}], draft: [{key, section, label, value, text, source: {kind: registry | conversation | agent, label, quote}}], checks: [{key, label, done, note}], discoverable}`. 메시지가 없으면 GET이 저장하지 않은 인사(`id: "greeting"`, 회사 이름으로 찾은 공공 허가 명부 후보)를 준다. `messages`는 `{text}`(비면 422 `EMPTY_MESSAGE`)를 받아 업체 측 모델(업체가 고른 모델, `llm_for("vendor")`)이 정해진 JSON으로 값과 담당자 말 인용만 내고, 초안 쓰기와 답 문장은 코드가 한다. 대화로 넣은 값은 인용이 담당자 메시지 안에 있고 숫자가 인용 안의 숫자일 때만 초안이 된다. 처리비·매입 부호가 말과 어긋나면 넣지 않는다. 허가 품목·차고지는 공공 명부 줄에서만 온다. 매입 기준(`criteria`, 재질·등급·형태·kg당 단가·최소 물량·이물·수분·수거비)도 대화로 받아 초안 한 항목(줄 목록)이 되고, 재질·단가·숫자가 담당자 말에 있어야 하며, 저장은 직접 추가와 같은 `confirm_criteria` 경로(값마다 담당자 입력 근거)다. `confirm`은 `{keys}`(고른 초안 키, 없으면 422 `NOTHING_TO_CONFIRM`)를 `PUT /vendors/{orgId}`와 같은 검사로 저장하고 `{profile, onboarding}`을 준다. 허가 품목은 이미 있는 품목을 지우지 않고 더한다. 모델이 없으면(규칙 정책) 대화로 채울 수 없다고 답하고, 모델 호출이 실패하면 그 사실만 답한다(담당자 메시지와 초안은 남는다). 같은 도구는 MCP 서버 `python -m app.v3.onboarding.mcp`로도 열린다(Bearer 키 하나가 업체 조직 하나, 저장 도구 없음) |
| `GET /vendors/{orgId}/mcp-keys`, `POST /vendors/{orgId}/mcp-keys`, `DELETE /vendors/{orgId}/mcp-keys/{keyId}` | 업체가 자기 AI 에이전트를 MCP로 연결하는 키(2026-11-22, `app/v3/onboarding/keys.py`). 업체 자신만. GET `{path: "/mcp/vendor", limit: 5, keys: [{id, label, prefix, createdAt, lastUsedAt}]}`, POST `{label}`(1~40자, 아니면 422 `INVALID_VALUE`, 다섯 개를 넘으면 409 `KEY_LIMIT`) → `{path, key, token}`(원문 `token`은 이때 한 번만, 저장은 sha256), DELETE는 목록을 돌려준다(없으면 404 `KEY_NOT_FOUND`). 키로 붙는 입구는 `/api/v3` 밖의 `/mcp/vendor`(POST, MCP streamable HTTP, 상태 없는 JSON 응답, `Authorization: Bearer <키>`, 틀리면 401 `UNAUTHORIZED`, GET은 405)이고 온보딩 도구(명부 찾기·명부 값·말한 값·매입 기준·현황)만 연다. 키가 조직을 정하고, 저장 도구는 없다. 에이전트가 초안을 고치면 온보딩 대화에 그 사실이 한 줄 남는다. `GET /vendors/{orgId}`는 키 해시를 내보내지 않는다 |
| `POST /sessions` | `{emitterOrgId}`로 진단을 만든다. `emitterOrgId`가 없으면 요청한 조직이다 |
| `GET /sessions` | 내가 당사자인 진단의 목록(최근 것이 앞). `[{id, emitterOrgId, emitterName, status, createdAt, updatedAt, as}]`. 배출 기업은 자기 진단, 업체는 자기에게 열린 대화가 있는 진단, 운영자는 전부. `as`는 `emitter`·`vendor`·`admin` |
| `GET /sessions/{id}` | 진단 뷰 |
| `GET /sessions/{id}/quote-request?streamId=&vendorId=` | 가입하지 않은 업체(결과 한 장 `outsideVendors`의 후보)에 보낼 견적 요청서 초안 `{kind: "quote_request", title, streamId, vendorId, demo, item: {name, material, form, monthlyKg}, facts: [{key, label, value, basis, status: known | estimated | unknown}], changes: [{field, label, before, after}], recipient: {id, name, business, address, phone, agency, asOf, evidence}, text, claims, rendered, parts, validation}`. 배출 기업만(업체 403 `FORBIDDEN`), 후보가 아니면 404 `NOT_FOUND`. 저장하지 않고 그때 만들며, 읽기라서 체험하기에서도 열린다. 업체에 연락하거나 견적을 확정하거나 한 달 손익을 바꾸지 않는다. 후보는 찾은 때의 흐름 자료로 고른 것이라, 만들기 전에 지금 자료로 그 업체가 여전히 후보인지 다시 본다(분류번호·상호·가입 여부·반경. `public_vendors.recheck`). 흐름이 지워졌거나 바뀐 자료로는 후보가 아니면(예: PP를 알루미늄·금속 스크랩으로 고침) 초안을 만들지 않고 409 `OUTSIDE_STALE`로 바뀐 칸과 다시 찾으라는 문장을 돌려준다. 바뀌었어도 후보가 맞으면 지금 자료로 만들고 `changes`에 적는다. 결과 한 장의 `outsideVendors.streams[]`에도 찾은 뒤 바뀐 칸(`changed`)과 지운 흐름(`removed`)이 붙는다(배출 기업만, 저장한 결과는 그대로). 본문의 숫자는 흐름의 claim이고 근거 검증기를 거친다. 값마다 출처(계량값·측정값·담당자 입력·담당자 답·사진 추정·추정·미확인)를 괄호로 적고, 거래 방식(매입·무상 인수·유상 처리), kg당 단가, 방문 수거, 최소 수거량, 운반비 포함·별도와 부담 주체, 공장이 낼 비용을 넣은 예상 정산을 묻는다. 시연 조직(`synthetic`)이나 체험하기면 `demo: true`이고 본문 첫 줄이 "※ 시연용 가상 자료입니다. 실제 업체에 보내지 마세요."다. `parts`는 화면 미리보기 조각이고 이으면 `rendered`와 같다. 주소·전화는 본문이 아니라 `recipient`에 있다 |
| `GET /sessions/{id}/adaptive` | 배출 기업 본인만 조건 변경 알림과 이전·새 결과를 읽는다. `{change: null 또는 {sessionId, sourceSessionId, reason, state, status, before, after}}`. state는 queued/running/ready/needs_input/failed. 업체는 403 |
| `POST /sessions/{id}/scenarios/preview` | 배출 기업 본인만 `{streamId, monthlyKg, maxPickupsPerMonth: 정수 또는 null}`로 격리된 규칙·계산 도구 비교. 원본은 쓰지 않는다. `{token, options, result, questions, excluded, evidence, basis}`. 금액·업체 확정이 아닌 예상 비교 |
| `POST /sessions/{id}/scenarios/apply` | 비교 입력과 token을 보내 월 발생량을 저장하고 새 재협상을 큐에 넣는다. `{sessionId}`. 자료·업체·기준일이 바뀌면 409 STALE_PREVIEW, 같은 token 재전송은 같은 찾기. 새 찾기는 양쪽 승인이 필요하며 기존 거래·문서·검수는 보존 |
| `GET /sessions/{id}/complex-plan` | 같은 단지 자원순환 후보(결과 한 장 `complexPartners`)의 2D 평면도 `{complex, bbox: [서, 남, 동, 북] \| null, scale, self: 자리 \| null, places: {후보 ID: 자리}, buildings: [], buildingLayer: {provider: "vworld", url, layer, key, domain, pageSize, maxPages} \| null, missing: [후보 ID], notes: {buildings, locations}, sources}`. 자리는 `{location: [경도, 위도], buildingId: null, source: registry \| kakao \| vworld \| org}`. 배출 기업만(업체 403 `FORBIDDEN`), 결과에 같은 단지 후보가 없으면 404 `NOT_FOUND`. 허가 명부 좌표가 없는 후보는 카카오 주소 검색(실패하면 VWorld 지오코더)으로 부를 때마다 위치를 찾고 저장하지 않는다. 건물 도형은 VWorld가 해외 IP(운영 VM)를 끊어 서버가 받지 않고, 브라우저가 `buildingLayer`(발급 도메인에 묶인 웹용 VWorld 열쇠 포함)로 JSONP를 불러 받는다(`buildings`는 예전 화면과 맞추려고 남긴 빈 칸). 열쇠(`KAKAO_REST_API_KEY`, `VWORLD_API_KEY`, `VWORLD_DOMAIN`)가 없으면 그 부분을 빼고 `notes`에 이유를 적는다 |
| `POST /sessions/{id}/run` | 에이전트를 다음 멈춤 지점까지 돌린다. 진단 뷰를 돌려준다. `?mode=async`면 뒤에서 돌리고 202 `{sessionId, status: "running", runId, after}`를 곧바로 준다(아래 "실시간 진행") |
| `GET /sessions/{id}/stream` | 진행 기록을 SSE(`text/event-stream`)로 보낸다. 두 에이전트의 작성 중·근거 확인·거부·다시 쓰기·보냄, 진단 상태, 담당자 질문(아래 "실시간 진행") |
| `POST /sessions/{id}/answers` | `{questionId, value | "unknown"}`. 수치 항목은 숫자나 글("2 퍼센트", "2프로 정도", "650 킬로", "0.65톤", "약 2%", "kg당 250원", "톤당 25만원")로 받아 항목의 단위로 바꾼다(톤→kg, 1년→한 달, 원/톤→원/kg). 숫자가 둘 이상("1~2%"), 범위를 뜻하는 말("이하", "넘게"), 다른 단위(% 자리에 kg), 한 달로 바꿀 수 없는 기간(주, 하루)은 `INVALID_ANSWER`이고 메시지가 고쳐 쓸 꼴을 알려 준다. "모름", "몰라요", "모르겠어요"는 `unknown`이다(`agents/amounts.py`) |
| `POST /sessions/{id}/approvals` | `{threadId}`. 요청한 조직 쪽 승인을 기록한다. 양쪽이 승인하면 confirm 메시지와 문서를 만든다. 배정된 흐름 가운데 허가증 대조 완료(`verified`)가 아닌 경로가 있으면 배출 기업의 승인(과 확정이 될 승인)은 409 `PERMIT_UNVERIFIED`다(업체는 먼저 승인할 수 있다) |
| `POST /sessions/{id}/permit-checks` | `{streamId, vendorOrgId, documentId, page, itemFound, processFits, itemQuote?, processQuote?}`. 배출 기업 담당자만, 승인을 기다리는 진단에서만. 그 업체가 올린 허가증(`permitDocuments[].id`, 상호·소재지가 그 업체와 맞는 것)의 원문 `page`쪽에서 이 흐름의 품목·재질과 업체 처리 방식을 대조한 결과를 기록한다. 기록은 업체 프로필 `permitChecks[]`에 쌓이고(`checkedBy: {orgId, orgName, userName}`, `checkedAt`, 근거 `human:<배출 기업>:허가증대조-…`, 배출 기업만 본다), 그 배출 기업의 판단에만 쓰인다. 둘 다 맞으면 그 경로는 `verified`, 아니면 `mismatch`가 되어 그 흐름을 대화에서 빼고 다시 배정한다. 응답은 진단 뷰. 오류: 403 `PERMIT_CHECK_FORBIDDEN`, 409 `NOT_AWAITING_APPROVAL`·`ALREADY_CONFIRMED`·`PERMIT_NOT_MATCHED`·`PERMIT_DOCUMENT_IGNORED`(다른 업체·사업장의 허가증), 404 `ROUTE_NOT_FOUND`·`PERMIT_DOCUMENT_NOT_FOUND`, 422 `INVALID_PAGE`·`INVALID_VALUE` |
| `GET /evidence/{id}` | 근거 하나와 계산 사슬 `{evidence, chain: [evidence]}` |
| `GET /evidence?q=&kind=` | 근거 검색 `[{evidence, score}]`. 로컬 순위는 보는 조직이 열 수 있는 근거만으로 만든 BM25F 색인(근거 이름·원문·출처·ID 필드 가중, 조사 떼기·한글 2-gram·조 번호, 분야 동의어 `evidence/synonyms.ko.json`)이고 `score`는 질의마다 0~1로 맞춘 값이다. Azure AI Search를 설정하면 그 순위와 로컬 순위를 RRF(k=60)로 합치고 `score`는 RRF 점수다 |
| `GET /intake/csv/template`, `POST /intake/csv` | 측정값 CSV 양식과 업로드 |
| `POST /intake/media` | 사진·영상 업로드(multipart: `file`, `orgId`, `streamId`). 탐지 모델을 불러 재질 추정과 이물 비율을 model 근거로 등록한다. 아래 "사진·영상" |
| `POST /intake/scan` | 보관장 3D 스캔 올리기(multipart: `frames` 파일 12~96장(JPEG·PNG, 장마다 3MB 이하, 합계 20MB 이하), `orgId`, `source`(`video` \| `photos`), `durationSec`). 배출 기업만. 브라우저가 영상에서 고른 장면을 재구성 서버(`V3_RECON_URL`, H100)에 맡기고 202로 진행 중인 `ScanView`를 준다. 재구성 서버가 없거나 닿지 않으면 503 `SCAN_UNAVAILABLE`, 같은 회사의 스캔이 이미 분석 중이면 409 `SCAN_IN_PROGRESS`. 아래 "보관장 3D 스캔" |
| `GET /orgs/{orgId}/scans`, `GET /orgs/{orgId}/scans/{scanId}` | 스캔 목록 `{canScan, scans: [ScanView]}`(최근 5개)과 스캔 하나. 진행 중이면 이때 재구성 서버에 물어 갱신한다(체험하기는 읽기만). `canScan`은 재구성 서버가 2초 안에 답했는지다 |
| `GET /orgs/{orgId}/scans/{scanId}/points` | 점군(`application/octet-stream`, 머리 16바이트 `RSPC`·판 1·점 수 N·0 + N × 10바이트: int16 x·y·z mm, uint8 r·g·b, uint8 더미 번호). 끝나지 않았으면 404 `POINTS_NOT_READY` |
| `PUT /orgs/{orgId}/scans/{scanId}/piles/{pileId}`, `PUT /orgs/{orgId}/scans/{scanId}/layout` | 더미의 폐기물 `{streamId}`(null이면 지정 해제, 계량값도 지움)과 계량한 무게 `{weighedKg}`(0 초과 100,000 이하, 폐기물을 먼저 골라야 한다: 422 `STREAM_REQUIRED`. 계량 kg ÷ 그 더미 부피가 10~1,500 kg/m³ 밖이면 422 `INVALID_VALUE`), 상차 지점 `{pickup: {x, y} \| null}`. 더미 고치기는 `{excluded: true}`("더미 아님": 벽 자국·합쳐진 더미를 합계·무게·정리안에서 빼고 그 더미의 폐기물·계량값을 지운다)와 `{excluded: false}`(다시 넣기)도 받는다. 끝나지 않은 스캔은 409 `SCAN_NOT_READY`, 없는 더미는 404 `PILE_NOT_FOUND`. 응답은 `ScanView` |
| `POST /intake/documents` | 문서 업로드와 추출. 매입 기준표(`kind: purchase_criteria`)는 판독기가 연결돼 있으면(`V3_LLM=foundry`) 모델이 쪽 그림과 글자 인식 결과(글·표 구조)를 함께 읽어 행 단위 초안 `draft`를 준다: `{id, outcome: ready \| needs_review \| not_found \| model_failed, rows, common, notes, reviewCount, reader, failure}`. 판독기가 없으면 표 머리글로 읽되(`reader.by: "rules"`) 숫자 읽기·단위 환산·확인 필요 판정은 같은 코드가 한다. 읽은 행이 있으면 서버가 초안의 값·원문·쪽을 보관하고 `draft.id`를 준다. 행의 값마다 문서 원문·쪽·단위 환산 메모·확인 사유가 붙는다. 범위 단가·협의·단위 미상·지급 방향 불명·회당 물량은 값을 비우고 확인 사유를 단다. 모델이 문서 안의 AI 대상 지시문을 찾으면 Prompt Shields 탐지와 같이 `blocked: true`(`shield.detectedBy: "reader"`)다. 초안은 `POST /intake/criteria/confirm`으로 저장하기 전에는 근거도 프로필도 아니다 |
| `POST /intake/criteria/confirm` | `{orgId, documentName, draftId, rows}`. 업체만. 매입 기준 검토 화면에서 담당자가 확인·고친 행을 저장한다. 행은 `{draftRow(초안 행 ID, 직접 더한 줄은 null), material, grade, form, priceMode, unitPriceKrwPerKg(매입 +, 처리비 −), minMonthlyKg, maxContaminationPct, maxMoisturePct, collectionFee: included \| separate \| null, collectionFeePerPickupKrw}`이고, 재질과 단가 방식(고정 단가 필수 및 kg당 1만 원 이내, 건별 협의는 금액 비움)·물량과 상한 범위·재질·등급·형태 중복을 검사해 틀리면 422 `INVALID_CRITERIA`(몇 번째 줄의 무엇인지)다. 서버는 `draftId`로 보관한 초안을 찾아(없거나 다른 조직이면 404 `DRAFT_NOT_FOUND`) 행마다 대조한다: 초안과 같은 값은 doc 근거(발췌는 보관한 문서 원문·환산·쪽), 다른 값·초안이 비워 둔 값(범위·협의·단위 미상·회당 물량)을 채운 값·재질·등급·형태를 바꾼 행의 값은 human 근거다. 화면이 보낸 `source`·`edited`는 근거 판단에 쓰지 않는다. `draftRow`가 없는 행은 직접 적은 줄(human)이다. 업체 프로필의 매입 기준을 재질·등급·형태로 맞춰 고치거나 더하고, 수거비 조건이 모든 행에서 같으면 운반 조건(`transport`)으로 쓴다. 응답은 `confirm`과 같은 `{evidence, profile}`이고 `profile`에 `transportUpdated`, `transport`가 더해진다 |
| `POST /intake/documents/confirm` | `{orgId, documentName, fields}`. 담당자가 확인한 추출값(`confirmed: true`)만 doc 근거로 등록한다. 응답은 `{"evidence": [근거 ID], "profile": 요약 \| null}`. 업체가 확인한 허가증·매입 기준표 값은 업체 프로필의 허가 품목·매입 기준에도 들어간다(추출 항목의 `target`으로 어느 품목·기준의 값인지 안다. 화면은 받은 `fields`를 그대로 돌려보낸다). 폐기물 종류가 같은 허가 품목, 재질·등급·형태가 같은 매입 기준은 고치고 근거를 붙이며 새로 만들지 않는다(다시 확인해도 겹치지 않는다). 단가를 확인하지 않은 새 기준은 만들지 않는다. `profile`은 `{permitsAdded, permitsUpdated, criteriaAdded, criteriaUpdated, permits, criteria, skipped}`이고 업체가 아니면 `null`이다 |
| `GET /admin/status`, `POST /admin/eval` | 색인·기준값 상태와 재학습 표, 사례 시험 실행(관리자만) |
| `POST /sessions/{id}/corrections` | `{streamId, field, before, after, note}`. 배출 기업 담당자가 고친 값을 기록한다 |
| `POST /sessions/{id}/outcomes` | `{requestId?, threadId, streamId, actualGrade, actualUnitPriceKrwPerKg, actualKg, inspectedOn, note, weighTicket?, documentName?}`. 업체가 확정 뒤 새 반입 검수 한 건을 등록한다. 같은 `requestId`로 다시 오면 한 건으로 처리한다 |
| `PUT /sessions/{id}/outcomes/{inspectionId}` | `{baseRevision, requestId, reason, actualGrade, actualUnitPriceKrwPerKg, actualKg, inspectedOn, note, weighTicket?, documentName?}`. 이미 기록한 검수 한 건을 정정한다(새 판) |
| `GET /admin/retrain/export`, `POST /admin/retrain/runs` | 재학습 행 내보내기, 재학습 실행 결과 등록(관리자만) |

오류는 `{"error": {"code": "...", "message": "한국어 문장"}}`과 맞는 상태 코드로 낸다.

### 실시간 진행 (`?mode=async`, 스트림)

두 에이전트가 주고받는 대화를 화면이 바로 보여 주게 한다. 실행은 뒤에서 돌고 화면은 진행 기록을 SSE로 받는다. 구현은 `app/v3/live.py`(기록 쓰기, 실행 자리, SSE 조각), `service.start_run`·`execute_run`·`stream_events`, `api.py`와 `devserver.py`의 같은 두 경로다.

**뒤에서 돌리기.** `POST /sessions/{id}/run?mode=async` → 202 `{"sessionId": "ses_1", "status": "running", "runId": "run_3", "after": 41}`.

- 동기 실행과 같이 배출 기업(과 관리자)만 부른다(업체는 403 `FORBIDDEN`, 체험하기는 403 `DEMO_READ_ONLY`). 질의가 없거나 `mode=sync`면 전처럼 이 요청 안에서 돌리고 진단 뷰를 준다. 다른 `mode`는 422 `INVALID_VALUE`.
- `after`는 이 실행이 남길 첫 기록 바로 앞의 seq다. 스트림을 `?after=<after>`로 열면 이 실행의 기록만 받는다(0이면 처음부터).
- 실행 중 오류: events에 `kind: "error"`(`detail.error`는 오류 종류 이름이나 코드. 내용은 싣지 않는다)와 진행 기록의 안내 한 줄(`status`)을 남기고, `running`으로 남은 진단을 `idle`(답할 질문이 있으면 `awaiting_answers`)로 돌린다. 동기 실행의 오류는 전처럼 오류 응답(500 `INTERNAL` 등)이고 같은 기록이 남는다.

**실행 자리(한 진단에 진행 하나).**

- run(동기·비동기), 답(`POST /sessions/{id}/answers`), 승인(`POST /sessions/{id}/approvals`)은 시작할 때 그 진단의 실행 자리를 차지하고 끝나면 비운다. 자리가 살아 있으면 409 `RUN_IN_PROGRESS`다.
- 자리는 저장소에 있다(메모리, SQL은 `v3_records`의 kind `runs`). 도는 동안 10초마다 살아 있다고 적고(heartbeat), 60초 동안 적지 않은 자리는 멈춘 것(프로세스가 죽거나 다시 뜸)으로 본다. 멈춘 자리는 다음 진행이 이어받고 events에 `kind: "error"`, `detail.reason: "stale"`을 남긴다. 진행기는 저장된 단계부터 이어 간다.

**스트림.** `GET /sessions/{id}/stream` → `text/event-stream`.

- 처음 읽을 때 권한을 확인한다. 볼 수 없으면 SSE가 아니라 보통의 오류(404 `SESSION_NOT_FOUND`, 401 등)다. 체험하기(`X-Demo-View: 1`, GET)로 시연 진단을 볼 수 있고 실제 조직의 진단은 보이지 않는다.
- 응답 헤더 `Cache-Control: no-cache`, `X-Accel-Buffering: no`. 첫 조각은 `retry: 3000`이다(프록시가 응답 머리를 곧바로 넘기게 한다).
- 기록 하나는 `id: <seq>`, `event: activity | status | question`, `data: <JSON 한 줄>`이다. seq는 진단마다 1부터 늘어나는 진행 기록 번호다. 보는 조직에게 걸러진 기록이 있으면 번호를 건너뛴다.
- 15초 동안 보낼 것이 없으면 주석 줄 `: keep-alive`를 보낸다.
- 실행 자리가 비었고(실행이 끝남) 기록을 다 보냈으면 `event: done`, `data: {"status": "awaiting_approval", "lastSeq": 158}`(id 없음)를 보내고 닫는다. 실행 중이면 열어 둔다. 실행 중이 아닌 진단을 열면 지난 기록을 보내고 곧바로 `done`이다. 그래서 화면은 run을 부른 뒤(202) 스트림을 연다.
- 이어 받기: `Last-Event-ID` 헤더와 `?after=<seq>` 가운데 큰 값보다 큰 seq만 보낸다. 기록은 저장소에 남으므로 다시 붙어도 빠뜨리거나 두 번 보내지 않는다. `after`가 0 이상의 정수가 아니면 422 `INVALID_VALUE`, 읽을 수 없는 `Last-Event-ID`는 쓰지 않는다.
- 도중에 볼 수 없게 되면(시연 되돌리기로 지워짐 등) `event: error`, `data: {"code", "message"}`를 보내고 닫는다.

`event: activity`:

```json
{"seq": 64, "at": "2026-11-18T05:12:09+00:00", "stage": 4,
 "actor": "company_agent | vendor_agent | validator | tool | system",
 "kind": "drafting | validating | rejected | rewriting | sent | handoff | tool | question | status | review",
 "threadId": "th_1", "vendorOrgId": "org_saegil", "vendorName": "(주)새길자원",
 "author": {"side": "company", "kind": "agent", "label": "회사 측 AI"},
 "text": "회사 측 AI가 메시지를 쓰고 있습니다.", "attempt": 1}
```

| `kind` | `actor` | 언제 | 더 붙는 필드 |
|---|---|---|---|
| `drafting` | `company_agent`, `vendor_agent` | 에이전트가 첫 초안을 쓰기 시작했다(모델을 부르기 전) | `attempt: 1` |
| `validating` | `validator` | 보내기 전에 초안의 숫자·조항을 근거와 대조한다. 회사 측이 받은 업체 답을 다시 확인할 때도 나온다(작성자는 업체, 문장은 "회사 측에서 …") | `attempt`(있을 때) |
| `rejected` | `validator` | 근거 확인에서 초안을 돌려보냈다 | `reasons`(화면용 사유 분류), `codes`(관리자 조직만), `attempt` |
| `rewriting` | `company_agent`, `vendor_agent` | 거부 사유를 고쳐 다시 쓴다 | `attempt: 2` |
| `sent` | `company_agent`, `vendor_agent` | 메시지가 대화에 붙었다(보냄) | `message`, `messageType`, `evidenceCount`(검증을 통과한 claim 수) |
| `handoff` | `company_agent`, `vendor_agent` | 담당자에게 넘기는 메시지가 붙었다 | `message`, `messageType` |
| `tool` | `tool` | 판단 도구를 불렀다 | `tool: {"name", "label", "status"}` |
| `question` | `system` | 담당자 질문 묶음을 냈다(배출 기업만) | |
| `status` | `system` | 단계 시작, 대화 열기, 조건 정리, 합의, 승인, 확정, 문서, 멈춤, 오류 안내, 대화의 끝 상태 | 대화의 끝 상태면 `threadStatus`, `threadOutcome` |
| `review` | `contract_reviewer` | 계약 검토 담당이 합의한 대화의 조건을 대조했다(양쪽이 본다) | `review: {status, items}`, `author.side: "reviewer"` |

- `author`는 그 일이 붙는 말풍선의 쪽이다(7장 "작성자 표시"와 같은 값). 코드가 쓴 메시지(허가 품목 decline, handoff)는 `kind: "system"`, `label: "자동 안내"`다. 진행기의 안내(`status`, `question`)는 `null`이다.
- `text`는 화면에 그대로 보일 한국어 한 줄이다. 숫자, 근거 ID, 내부 코드를 넣지 않는다(거부 사유도 분류만 쓴다). 값은 `message`의 claims에 있다.
- `message`는 같은 조직이 `GET /sessions/{id}`로 받는 `threads[].messages[]`의 그 메시지와 같다(같은 함수로 만든다). 보는 조직이 그 대화를 볼 수 없으면(관리자 조직) `null`이다.
- `reasons`(거부 사유 분류, 코드는 아래 "초안 거부 코드"): 근거에 없는 숫자(`NUMBER_OUTSIDE_PLACEHOLDER`), 근거에 없는 값(`UNKNOWN_PLACEHOLDER`, `UNKNOWN_CLAIM_REF`, `CLAIM_WITHOUT_EVIDENCE`, `EVIDENCE_NOT_FOUND`), 본문과 값이 맞지 않음(`UNUSED_CLAIM`), 근거와 다른 값(`VALUE_MISMATCH`, `UNIT_MISMATCH`), 원문과 다른 조항 인용, 볼 수 없는 근거, 기한이 지난 근거, 지금 보낼 수 없는 종류(`TYPE_NOT_ALLOWED`, `APPROVAL_REQUIRED`), 대화 규칙에 맞지 않음, 대화에 없는 폐기물, 허가 품목이 맞지 않음, 근거 없는 조건 낱말, 업체 기준 밖의 조건(`GUARDRAIL_*`), 다른 회사 정보(`OTHER_EMITTER_*`), 응답 형식 오류, 모델 호출 실패. 모르는 코드는 "검사를 통과하지 못함". 표는 `live.REASONS`다.
- `tool.label`은 도구의 한국어 이름(`live.TOOL_LABELS`), `status`는 `calculated | conditional | needs_information | error`다.
- **대화의 끝 상태.** 실행이 멈출 때(승인 대기, 끝남, 답 대기) 열린 대화마다 `kind: "status"` 기록 하나가 `threadId`와 함께 나온다(두 당사자가 함께 본다). `threadOutcome`은 `agreed`(승인 대기) | `confirmed` | `declined` | `handoff` | `not_selected`(조건을 받았지만 최적안에 들지 않음) | `ended`(조건을 다 받지 못하고 끝남) | `waiting`(배출 기업 담당자 답 대기)이고 `text`가 그 문장이다. 화면은 대화마다 마지막 끝 상태로 "조건을 묻는 중" 표시를 바꾼다. 지난번과 같은 끝 상태는 다시 나오지 않는다. 진단 뷰 `threads[]`에도 같은 값이 `outcome`, `outcomeText`(진행 중이거나 열지 않은 대화는 `null`)로 있다.

`event: status`: `{"seq", "at", "status", "phase", "stages": [7장의 stages와 같은 모양]}`. 진단 상태, 진행 단계(`phase`), 단계별 상태가 바뀔 때 나온다.

`event: question`: `{"seq", "at", "stage", "questions": [7장의 questions와 같은 모양]}`. 담당자 질문 묶음이다. 배출 기업(과 관리자 조직)만 받는다.

보는 조직에 따른 거르기는 진단 뷰와 같다.

- 배출 기업(과 관리자 조직): 모든 기록.
- 업체: 자기와 열린 대화(`not_opened`가 아닌)의 기록 가운데 두 당사자가 함께 보는 것(`sent`, `handoff`, 대화 열기·합의·승인·확정·문서 안내)과 자기 쪽이 한 일(자기 에이전트의 작성 중·근거 확인·거부·다시 쓰기·도구)만 받는다. 회사 측의 작성·검증·도구, 회사 측의 재확인, 다른 업체의 대화, 담당자 질문, 대화 밖의 안내는 받지 않는다. `status`는 받는다(진단 뷰의 `session`·`stages`와 같다).
- 거부 코드(`codes`)는 관리자 조직에만 나간다.

저장과 실시간:

- 진행기는 그 일을 할 때 곧바로 기록을 저장소에 쓴다. 메모리 저장은 프로세스 안 목록, SQL은 `v3_activity` 테이블(마이그레이션 `0006_v3_activity`)이고 기록 하나가 트랜잭션 하나라 곧바로 커밋된다. 스트림은 저장소에서 읽으므로 실행이 끝나기 전에도 보이고 이어 받기가 정확하다.
- 같은 프로세스에서는 기록을 쓸 때 기다리는 스트림을 깨운다(0.1초 안에 나간다). 다른 프로세스가 쓴 기록도 1초마다 저장소를 다시 읽어 잡는다.
- 작성 중·근거 확인·거부·다시 쓰기는 `draft.run_draft`가 모델을 부르기 전과 검사하기 전에 알린다(`draft.LIVE`, `live.Turn`). 실제 모델이면 차례마다 몇 초씩 걸리므로 작성 중이 그만큼 보이고, 규칙 정책(시연)이면 곧바로 지나가도 같은 순서(작성 중 → 근거 확인 → 보냄)가 남는다.
- 업체 측 에이전트가 다른 프로세스에 있으면(`V3_VENDOR_AGENTS`) 업체 측 작성 중은 회사 측이 부를 때 남기고, 업체 측의 거부·다시 쓰기·도구는 답을 받은 뒤 업체 측이 보낸 기록(정해 둔 값만 옮긴 것)으로 채운다.

배포에서 주의할 것:

- 프록시(Caddy, nginx)와 프런트 서버(Next.js BFF)는 `text/event-stream` 응답을 모아 두지 말고 그대로 흘려보내야 한다. 백엔드는 위 헤더를 보낸다. 브라우저의 `EventSource`는 헤더를 붙일 수 없으므로 BFF가 같은 출처의 주소로 받아 `Authorization`, `X-User-Id`, `X-Org-Id`(체험하기면 `X-Demo-View`)와 `Last-Event-ID`를 붙여 넘긴다.
- 뒤에서 도는 실행은 요청을 받은 프로세스의 스레드다. 배포는 컨테이너 하나에 uvicorn 프로세스 하나다. 프로세스를 여럿 띄워도 실행 자리와 기록이 PostgreSQL에 있어 409와 스트림은 맞게 돌지만, 다른 프로세스가 쓴 기록은 1초 간격으로 보인다. 프로세스가 죽으면 그 실행은 이어지지 않는다(60초 뒤 멈춘 것으로 보고 다시 진행할 수 있다).
- 메모리 저장(로컬, 테스트)에서 동기 실행은 끝날 때까지 다른 요청을 기다리게 하므로(전과 같다) 스트림도 끝에 한꺼번에 받는다. 실시간으로 보려면 `mode=async`를 쓴다(뒤에서 도는 실행은 저장소를 부를 때만 잠근다).

### 보관장 3D 스캔

`app/v3/scan/`. 배출 기업이 휴대폰으로 보관장을 한 바퀴 찍으면 브라우저가 장면 24~80장(긴 변 896px)을 골라 보내고, 재구성 서버(`infra/h100/recon`, H100)가 m 단위 바닥 좌표계(원점은 보인 바닥의 중심, z 위쪽)로 더미를 나눠 부피를 잰다.

- `ScanView`: `{id, orgId, createdAt, source, frameCount, status: processing | done | failed, progress: {stage: geometry | scale | floor | piles, ratio} | null, failure: {message} | null, summary: {pileCount(뺀 더미 제외), excludedCount, volume(claim m³), weight: {low, high}(claim kg) | null, weightPending, floorAreaM2, floorArea(claim m², model 근거)} | null, piles: [...], layout, floor: {polygon, areaM2}, camera: {path}, bounds, pointsCount, quality: {frames, scaleSpreadPct, notes}}`.
- `piles[]`: `{id, excluded, name("더미 n"), center, footprint(꼭짓점 넷), sizeM, yawDeg, heightM, volume(claim, model 근거), volumeRangeM3, coverage, thumbnail(JPEG data URL), streamId, streamName, density: {basis: weighed | stream | ref, lowKgM3, highKgM3, label, evidence} | null, weight: {low, high} | null, weighedKg(claim, human 근거) | null, note}`. 무게 = 부피 범위 × 밀도(calc 근거). 밀도는 계량값(그 스캔에서 같은 폐기물로 지정한 더미의 계량 kg 합 ÷ 부피 합) > 폐기물의 `bulkDensityKgPerM3`(human 근거) > 형태가 필름이면 US EPA 2016 환산표 LDPE 필름 흩어짐 20.8 ~ 압축 89.0 kg/m³(ref 근거) 순서이고, 없으면 `weight: null`(산정 보류, `summary.weightPending`에 센다).
- `layout`: `{pickup: {x, y, source: start | manual}, rules: {gapM 1.0, wallM 0.3, pickupClearM 1.5, evidence(assumption)}, current: 지표, proposal: {piles: [{id, center, footprint, yawDeg, zone}], zones: [{streamId, name, polygon}], metrics: 지표, moved, kept: [옮길 자리가 없어 제자리에 둔 더미 ID]} | null, unavailable: 문장 | null}`. 지표는 `{mixedPairs, minMixedGapM, weightedDistanceM, footprintM2, evidence: {mixedPairs, minMixedGapM, weightedDistanceM: calc 근거 ID | null}}`이다. `footprintM2`는 더미 자국 전체의 볼록 껍질 넓이(퍼진 정도)라 정리안에서 커질 수 있어 화면에 쓰지 않는다. 정리안은 `scan/layout.py`가 규칙으로 정한다(모델이 그리지 않는다): 다른 폐기물 사이 gapM 이상, 벽에서 wallM 안쪽, 상차 지점 반경 비우기, 같은 폐기물끼리 0.2m, 0°/90°만, 부피 합이 큰 폐기물부터 상차 지점 가까이 탐욕 배치, 지정하지 않은 더미는 제자리. 폐기물을 지정했어도 옮길 자리가 없는 더미는 제자리에 두고(`kept`) 나머지를 다시 놓으며, 모두 제자리가 되면 정리안이 없다. 실제 크기를 휴대폰 높이 가정으로 맞췄으면(`scale.method`에 camera-height) 부피 근거가 가정값 근거 `assumption:보관장스캔-휴대폰높이@v1`(1.4 m)을 잇는다.
- 계산 근거(calc)는 `meta.inputEvidence`로 쓴 값(부피 model 근거, 밀도 근거, 배치 가정값)을 잇는다(`GET /evidence/{id}`의 사슬). 근거 출처에는 내부 ID 대신 찍은 날을 쓰고 모델 이름은 넣지 않는다. 재구성 결과가 계약과 다르거나 저장하다 실패하면 그 스캔을 `failed`(분석 쪽 문제 문장, 다시 찍으라고 하지 않음)로 닫는다.
- 계산한 값(무게·합계·정리안)과 그 근거는 쓰는 경로(분석 끝남, 더미 고치기, 상차 지점)에서 등록해 저장한다. 조회는 쓰지 않는다(체험하기 읽기 전용). 실제 조직은 폐기물의 이름·형태·밀도가 바뀌었으면 조회 때 다시 계산한다.
- 체험하기의 시연 배출 기업에는 합성 보관장 장면을 재구성 서버로 돌린 결과(`app/v3/demo/scan/`)를 고정 ID `scan_demo_hanbit`로 넣는다(파일이 있을 때만).

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
| `INVALID_CREDENTIALS` | 401 | 로그인 이메일이나 비밀번호가 틀렸다. 어느 쪽이 틀렸는지 알려 주지 않는다(문장도 같다). 비밀번호 바꾸기와 탈퇴(9장)에서 다시 확인한 지금 비밀번호가 틀려도 같은 코드다 |
| `UNKNOWN_ORG` | 401 | 모르는 조직이다 |
| `FORBIDDEN` | 403 | 그 조직이 할 수 없는 일이다(다른 조직의 자료, 관리자 전용 경로, 배출 기업만 하는 진행·답·교정 등) |
| `WRONG_ROLE` | 403 | 조직의 역할이 맞지 않는다(배출 기업만, 처리 업체만) |
| `NOT_A_MEMBER` | 403 | bff 모드에서 사용자가 그 조직(`X-Org-Id`나 주소의 조직)의 구성원이 아니다 |
| `OWNER_ONLY` | 403 | 조직 소유자(owner)만 할 수 있다(초대 만들기, 조직 정보 고치기, 다른 구성원 빼기, 소유자 지정. 9장) |
| `DEMO_READ_ONLY` | 403 | 체험하기(`X-Demo-View: 1`)에서 GET이 아닌 요청을 보냈다. 시연 조직이나 운영 조직의 정보를 고치려 했을 때도(9장 조직 고치기) |
| `NOT_DEMO_ORG` | 403 | 체험하기의 `X-Org-Id`가 시연 조직이 아니다(실제 조직, 운영 조직, 없는 조직) |
| `NOT_FOUND` | 404 | 그런 주소가 없다(bff가 아닌 모드에서 9장의 경로를 부른 경우도) |
| `ORG_NOT_FOUND`, `VENDOR_NOT_FOUND`, `STREAM_NOT_FOUND`, `SESSION_NOT_FOUND`, `THREAD_NOT_FOUND`, `QUESTION_NOT_FOUND`, `EVIDENCE_NOT_FOUND`, `OUTCOME_NOT_FOUND` | 404 | 없거나 볼 수 없다(볼 수 없는 것은 있는지도 알려 주지 않는다) |
| `MEMBER_NOT_FOUND` | 404 | 빼거나 소유자로 지정하려는 사용자가 그 조직의 구성원이 아니다(9장) |
| `ORG_EXISTS` | 409 | 협상 테스트 회사에 배정한 조직 ID를 이미 다른 조직이 쓰고 있다 |
| `NOT_AGREED` | 409 | 합의된 조건이 없는 대화를 승인하려 했다 |
| `KEY_LIMIT` | 409 | 업체 MCP 연결 키를 다섯 개 넘게 만들려 했다 |
| `KEY_NOT_FOUND` | 404 | 끊으려는 MCP 연결 키가 그 업체의 키가 아니다 |
| `UNAUTHORIZED` | 401 | `/mcp/vendor`(MCP 입구)의 업체 연결 키가 없거나 맞지 않다 |
| `EMPTY_MESSAGE`, `NOTHING_TO_CONFIRM` | 422 | 업체 온보딩 대화에 빈 말을 보냈다, 또는 저장할 초안 항목을 하나도 고르지 않았다(고른 키가 초안에 없다) |
| `EMAIL_TAKEN` | 409 | 이미 가입한 이메일이다(대소문자와 앞뒤 공백은 가리지 않는다) |
| `APPROVAL_BLOCKED` | 409 | 승인이 막혀 있다. 이유는 `approvals[].blockedReason`과 `blockedCodes`에 있다(아래 "승인을 막는 사유 코드") |
| `REVIEW_BLOCKED` | 409 | 계약 검토 담당(`app/v3/agents/reviewer.py`)이 막은 항목이 있어 승인할 수 없다. 막은 항목과 이유는 `approvals[].review.items[]`(`status: "block"`)에 있다. 양쪽 모두 같다 |
| `MEMORY_DISABLED` | 409 | 회사 기록 사용(`enabled`)이 꺼져 있는데 판정 반영(`applyToJudgment: true`)을 승인하려 했다(`PUT /orgs/{orgId}/memory`). 기록 사용을 먼저 켠다 |
| `PERMIT_UNVERIFIED` | 409 | 허가증 원문 대조가 끝나지 않은 경로(검토 대기, 원본 확인 필요)가 있어 확정할 수 없다. 메시지에 폐기물 → 업체(상태)와 할 일이 있다. 승인 칸은 막지 않는다(`approvals[].permitHold`) |
| `PERMIT_CHECK_FORBIDDEN` | 403 | 허가증 원문 대조는 그 진단의 배출 기업 담당자만 기록할 수 있다 |
| `ROUTE_NOT_FOUND`, `PERMIT_DOCUMENT_NOT_FOUND` | 404 | 그 진단에서 그 업체와 그 흐름으로 대화한 경로가 없다, 또는 그 업체가 올린 허가증이 아니다 |
| `NOT_AWAITING_APPROVAL`, `ALREADY_CONFIRMED`, `PERMIT_NOT_MATCHED`, `PERMIT_DOCUMENT_IGNORED` | 409 | 허가증 원문 대조를 기록할 수 없다: 승인을 기다리는 진단이 아님, 이미 확정한 경로, 업체 허가 품목에 그 흐름이 없음, 상호·소재지가 다른 업체·사업장의 허가증 |
| `INVALID_PAGE` | 422 | 대조한 쪽이 1 이상의 정수가 아니거나 허가증에 없는 쪽이다 |
| `PERMIT_MISMATCH` | 409 | 승인하려는 대화의 업체 허가 품목이 합의 뒤에 바뀌어 흐름과 맞지 않는다. 승인하지 않는다. 맞는 흐름이 남지 않으면 그 대화를 근거와 함께 `decline`으로 닫고, 일부만 맞지 않으면 그 흐름만 빼고 승인을 지운다(남은 조건으로 다시 승인한다) |
| `THREAD_NOT_CONFIRMED` | 409 | 확정되지 않은 대화에 실제 결과를 기록하려 했다 |
| `REQUEST_ID_REUSED` | 409 | 실제 결과 등록·정정의 `requestId`가 이미 다른 내용으로 쓰였다(재전송이면 내용이 같아야 한다. 새 반입이나 새 정정이면 새 번호를 쓴다) |
| `REVISION_CONFLICT` | 409 | 정정의 `baseRevision`이 그 검수의 최신 판이 아니다(그 사이 다른 정정이 들어왔다). 새로 불러온 뒤 다시 정정한다 |
| `DUPLICATE_TICKET` | 409 | 같은 업체가 같은 검수일에 같은 계량표 번호(`weighTicket`)로 다른 검수를 이미 기록했다 |
| `STALE_PREVIEW` | 409 | 비교 후 자료·업체 조건·기준일이 바뀌었거나 새 대안으로 대체됐다. 다시 비교한 token으로 적용해야 한다 |
| `VERSION_CONFLICT` | 409 | 문의가 먼저 바뀜. 다시 불러온 뒤 확인 |
| `RUN_IN_PROGRESS` | 409 | 그 진단의 진행(run, 답, 승인)이 이미 돌고 있다(위 "실시간 진행"의 실행 자리). 탈퇴로 지울 조직이 들어간 진단이 진행 중일 때도(9장 "탈퇴"). 아무것도 지우지 않는다 |
| `LAST_OWNER` | 409 | 조직의 마지막 owner를 빼거나 마지막 owner가 나가려 했다. 먼저 다른 구성원을 owner로 지정한다(9장) |
| `TRANSFER_OWNERSHIP_FIRST` | 409 | 탈퇴하려는 사용자가 다른 구성원이 있는 조직의 유일한 owner다. 먼저 다른 구성원을 owner로 지정한다. 아무것도 지우지 않는다(9장 "탈퇴") |
| `OUTSIDE_STALE` | 409 | 견적 요청서를 만들려는 가입하지 않은 업체가 지금 흐름 자료로는 후보가 아니다(후보를 찾은 뒤 재질·품목을 바꿨거나 흐름을 지웠다, 업체가 가입했다 등). 초안을 만들지 않는다. 문장에 바뀐 칸과 다시 찾으라는 말이 있다 |
| `INVITE_INVALID` | 410 | 초대가 없거나, 만료되었거나(7일), 이미 쓰였다 |
| `SCAN_NOT_FOUND`, `PILE_NOT_FOUND`, `POINTS_NOT_READY` | 404 | 보관장 3D 스캔·더미가 없거나 볼 수 없다. 점군은 분석이 끝나야 있다 |
| `SCAN_NOT_READY`, `SCAN_IN_PROGRESS`, `PILE_EXCLUDED` | 409 | 분석이 끝나지 않은 스캔을 고치려 했다. 같은 회사의 스캔이 이미 분석 중이다(GPU를 업체 측 AI와 함께 쓴다). "더미 아님"으로 뺀 더미의 폐기물·계량값을 고치려 했다(다시 넣은 뒤 고친다) |
| `TOO_FEW_FRAMES`, `BAD_IMAGE`, `STREAM_REQUIRED` | 422 | 스캔 장면이 12장보다 적다. 재구성 서버가 사진을 읽지 못했다. 계량값을 적기 전에 더미의 폐기물을 고르지 않았다 |
| `TOO_LARGE` | 413 | 본문이 20MB를 넘는다 |
| `UNSUPPORTED_MEDIA` | 415 | 사진·영상이 아닌 파일이다. 탐지 모델이 연결되지 않았어도 형식 검사가 먼저다 |
| `INVALID_BODY` | 422 | 본문이 JSON 객체가 아니거나 필수 항목(`questionId`·`value`, `threadId` 등)이 없다 |
| `FILE_REQUIRED` | 422 | 올릴 파일이 없거나 비어 있다 |
| `INVALID_VALUE` | 422 | 값이 틀렸다(음수 물량, 범위를 벗어난 비율, 모르는 모델 설정, 날짜 꼴, run의 `mode`, 스트림의 `after` 등) |
| `INVALID_KIND` | 422 | 근거 종류나 문서 종류가 목록에 없다 |
| `INVALID_CRITERIA` | 422 | 매입 기준표 검토 화면에서 저장한 행이 틀렸다(재질 코드, 단가 없음·범위 밖, 물량·상한 범위 밖, 재질·등급·형태 중복). 문장에 몇 번째 줄의 무엇인지 적는다 |
| `INVALID_ANSWER` | 422 | 질문의 답이 그 항목에 맞지 않는다(빈 답 포함) |
| `INVALID_FIELD` | 422 | 교정할 수 없는 항목이다(`POST /sessions/{id}/corrections`의 `field`) |
| `NO_CHANGE` | 422 | 교정 전후 값이 같다(실제 결과 정정도 같다) |
| `INVALID_REQUEST_ID` | 422 | 실제 결과 등록·정정의 `requestId` 꼴이 틀렸다(영문·숫자·`_.:-` 8~100자). 정정에는 꼭 있어야 한다(새 반입 등록은 없으면 서버가 만든다) |
| `USE_CORRECTION` | 422 | 새 반입 등록(`POST /sessions/{id}/outcomes`)에 `inspectionId`를 보냈다. 이미 있는 검수를 고치는 것은 `PUT /sessions/{id}/outcomes/{inspectionId}`다 |
| `STREAM_NOT_IN_THREAD` | 422 | 그 대화에서 확정된 흐름이 아니다(실제 결과 기록) |
| `INVALID_MESSAGE` | 422 | 업체 측 에이전트가 받은 메시지를 처리할 수 없다 |
| `TOO_MANY_ATTEMPTS` | 429 | 같은 이메일로 15분 안에 로그인을 10번 틀렸다(서버 프로세스 메모리에서 센다). 비밀번호 바꾸기·탈퇴에서 지금 비밀번호를 틀린 횟수도 같은 칸에 센다 |
| `INTERNAL` | 500 | 예상하지 못한 오류. 내용은 응답에 싣지 않는다 |
| `DOCUMENT_FAILED`, `SHIELD_FAILED`, `DETECTOR_FAILED` | 502 | Document Intelligence, Prompt Shields, 탐지 엔드포인트 호출이 실패했거나 계약과 다른 답이 왔다 |
| `DOCUMENT_UNREADABLE` | 422 | Document Intelligence가 파일 자체를 열지 못했다(깨진 PDF 등). 다른 파일로 다시 올리거나 직접 적는다 |
| `DRAFT_NOT_FOUND` | 404 | 매입 기준표 저장이 가리킨 판독 초안이 없거나 다른 조직의 것이다 |
| `DOCUMENT_INTELLIGENCE_UNAVAILABLE` | 503 | Document Intelligence가 설정되지 않아 글이 아닌 문서를 읽을 수 없다 |
| `BFF_KEY_NOT_SET` | 503 | bff 모드인데 BFF 키 설정이 비어 있다. 모든 요청을 거절한다 |
| `SCAN_UNAVAILABLE` | 503 | 보관장 3D 스캔 재구성 서버(`V3_RECON_URL`)가 설정되지 않았거나 닿지 않는다 |
| `TRUST_UNAVAILABLE` | 503 | `GET /trust`가 최신 평가 요약 파일을 읽지 못했다 |

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
- `POST /sessions/{id}/outcomes`(새 반입 등록): 그 스레드의 업체만, 스레드가 `confirmed`일 때만 받는다(아니면 `THREAD_NOT_CONFIRMED` 409). `streamId`는 그 스레드에서 확정된 흐름이어야 하고, 실제 등급이나 실제 단가 가운데 하나는 있어야 한다. `documentName`을 주면 doc 근거, 없으면 human 근거로 등록한다. 소유는 업체이고 배출 기업과 나눈다. 같은 날 같은 흐름을 여러 번 받았으면 건마다 따로 보낸다(검수일로 합치거나 덮지 않는다).
  - `requestId`(영문·숫자·`_.:-` 8~100자)는 화면이 등록 한 번마다 만드는 번호다. 화면은 늘 보낸다. 보내지 않으면(2026-11-19 전 화면과의 하위 호환) 서버가 만든 번호(`auto-…`)로 새 반입 한 건이 되고, 번호가 매번 달라 재전송을 한 건으로 묶지 못한다. 검수 ID는 `insp-` + (업체, 진단, `requestId`)의 해시다. 같은 번호와 같은 내용으로 다시 오면(네트워크 재시도, 두 번 누름) 새 건을 만들지 않고 이미 만든 건의 최신 판을 `replayed: true`로 돌려준다. 같은 번호에 다른 내용이면 409 `REQUEST_ID_REUSED`, 번호의 꼴이 틀리면 422 `INVALID_REQUEST_ID`, 본문에 `inspectionId`가 있으면 422 `USE_CORRECTION`(정정은 아래 PUT)이다.
  - `weighTicket`(선택, 40자까지)은 계량표 번호다. 같은 업체가 같은 검수일에 같은 번호로 다른 검수를 이미 기록했으면 409 `DUPLICATE_TICKET`이다(같은 계량표를 손으로 두 번 적은 경우).
  - 근거 ID는 판마다 따로다: `human:<업체>:검수-<검수 ID>-r<판>-<등급|단가|중량>`(문서면 `doc:<업체>:<문서>:…`). 응답은 `{"outcome": 기록, "evidence": [근거 ID], "replayed": bool}`.
- `PUT /sessions/{id}/outcomes/{inspectionId}`(정정): 그 검수를 기록한 업체만(배출 기업 403, 당사자가 아니거나 없는 검수면 404 `OUTCOME_NOT_FOUND`), 스레드가 `confirmed`일 때만. 본문은 고친 뒤의 값 전체와 `baseRevision`(담당자가 보고 고친 판), `reason`(정정 사유, 필수, 200자까지), `requestId`다. 앞 판의 근거는 덮지 않고 새 판(`revision` + 1)과 새 근거를 만든다. `baseRevision`이 최신 판이 아니면 409 `REVISION_CONFLICT`, 값이 같으면 422 `NO_CHANGE`, `threadId`·`streamId`를 바꾸려 하면 422다. 같은 `requestId`로 다시 오면 `replayed: true`로 이미 만든 판을 돌려준다. 실적 리포트와 재학습(등급 학습 행, 기준값 보정 후보)은 검수 건마다 최신 판 하나만 센다.
  - 검수일 키로 만든 이전 기록(`legacy`)도 그 키를 `inspectionId`로 써서 정정한다. 새 판 근거는 `human:<업체>:<이전 키>-r<판>-…`이고 이전 근거 ID는 그대로 열린다.
- 검수 기록의 탄소 기준(`app/v3/outcome_carbon.py`): 새 반입 등록과 정정 본문에 `actualProcess`(업체가 실제로 적용한 처리 방식 `재활용 | 열분해 | 소각 | 매립`, 선택. 다른 값은 422 `INVALID_VALUE`)를 받고 기록에 그대로 싣는다(값 비교·재전송 판정에 든다). 기록의 `appliedProcess: {"value", "source": "vendor_input | vendor_profile | price | unknown", "assumed"}`는 탄소 계산에 쓴 처리 방식이다: 적었으면 그 값, 처음 기록에서 적지 않았으면 기록 당시 업체 정보, 그것도 없으면 값을 받고 사 간 경우만 재활용으로 본다. 정정에서 적지 않으면 앞 판의 것을 잇는다(오늘의 업체 정보로 바꾸지 않는다). 처음 기록할 때 비교한 현행 처리(배출 기업 자료), 적용한 처리 방식, 두 배출계수의 값·단위·판·기준일, 계산 범위, kg당 배출 차이(kgCO2e/kg)를 calc 근거 `calc:outcome_carbon:<(검수 ID, 업체, 판)의 지문>:perKg` 하나에 고정한다. 현행 처리는 업체에 열지 않는 값이라 이 근거는 배출 기업만 보고 업체와 나누는 기록에는 싣지 않는다. 정정에서는 기준을 앞 판에서 물려받고, 처리 방식이 바뀐 판만 그 판 번호로 새로 고정한다(바뀐 처리의 계수만 정정한 날 기준).
- 실적 리포트의 `records[]`에는 `carbonBasis: {"status": "recorded | not_computed | reconstructed | unverified", "statusLabel", "capturedOn", "capturedBy", "revision", "baseline": {"method", "assumed", "source", "sourceLabel"}, "applied": {"process", "source", "sourceLabel", "assumed"}, "factors": {"baseline", "applied"}, "perKg", "perKgUnit", "scope", "reason", "notes", "deal"}`, `measuredBy`(`doc | human`), `carbonNote`(탄소를 내지 않은 이유. 계산했으면 null)가 붙고, `process`·`currentMethod`·`currentMethodAssumed`는 이 기준의 값이다(지금 업체 정보·폐기물 자료가 아니다). 탄소 변화 = 실제 중량 × `perKg` ÷ 1000이고 `status`가 `recorded`·`reconstructed`일 때만 값이 있고 합계에 들어간다. 리포트 맨 위의 `carbonScope`는 계산 범위(지금은 `process_only`: 수거 운송 배출과 신재 대체 효과를 넣지 않는다)이고, `totals.carbonStatus`는 상태별 건수다.
- 이 기준이 생기기 전의 검수 기록은 확정한 합의의 배정 계산 근거를 지금 계수로 다시 계산해 같을 때만 `reconstructed`(합의 당시 기준)로, 아니면 `unverified`로 처음 읽을 때 고정한다. `unverified`는 탄소를 계산하지 않고 `reason`을 적는다. 배포 뒤 `python -m scripts.v3.backfill_outcome_carbon [--apply]`로 미리 고정할 수 있다. 한때 쓴 형식(`calc:outcome_carbon_basis:…`, 검수 기록마다 하나)으로 남은 기록 당시 기준이 있으면 재구성하지 않고 그 값을 `recorded`로 옮긴다.
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
| `POST /accounts/password` | 키, `X-User-Id` | `{currentPassword, newPassword}` → 204. 아래 "비밀번호 바꾸기" |
| `DELETE /accounts/me` | 키, `X-User-Id` | 탈퇴. 본문 `{password}` → 204. 아래 "탈퇴" |
| `POST /orgs` | 키, `X-User-Id` | `{name, kind: "emitter" \| "vendor", bizNo?, region?, address?, location?: [경도, 위도]}` → 201 조직. 만든 사람이 owner다 |
| `POST /orgs/negotiation-test` | 키, `X-User-Id` | 본문 없이 호출 → 201 협상 테스트 회사. 이미 만들었으면 같은 회사를 돌려준다 |
| `PATCH /orgs/{orgId}` | 키, `X-User-Id`(그 조직의 owner) | `{name?, bizNo?, region?, address?, location?}` → 200 `POST /orgs`의 응답과 같은 모양(`memberRole` 포함). 아래 "조직 고치기" |
| `GET /orgs/{orgId}/members` | 키, `X-User-Id`(그 조직의 구성원) | `[{userId, name, email, memberRole}]` |
| `DELETE /orgs/{orgId}/members/{userId}` | 키, `X-User-Id`(owner, 또는 자기 자신) | 204(본문 없음). owner는 누구든 빼고 member는 자기만 뺀다(나가기). 아래 "구성원과 소유자" |
| `POST /orgs/{orgId}/owners/{userId}` | 키, `X-User-Id`(그 조직의 owner) | 그 구성원을 owner로 지정한다. 200 `{userId, name, email, memberRole: "owner"}`(구성원 목록의 한 줄). 이미 owner면 그대로 |
| `POST /orgs/{orgId}/invites` | 키, `X-User-Id`(그 조직의 owner) | 201 `{token, expiresAt}`. 구성원이 아니면 403 `NOT_A_MEMBER`, owner가 아니면 403 `OWNER_ONLY` |
| `GET /invites/{token}` | 키 | 초대 화면(로그인 전)이 초대한 조직을 보인다. 200 `{orgName, kind, expiresAt, valid: true}`. 조직 ID, 만든 사람, 구성원은 싣지 않는다. 초대를 쓰지 않는다. 없거나 만료되었거나 이미 쓴 초대는 410 `INVITE_INVALID`(어느 쪽인지 밝히지 않는다) |
| `POST /invites/{token}/accept` | 키, `X-User-Id` | 200 `{orgId, orgName, kind, memberRole: "member"}`. 없거나 만료되었거나 이미 쓴 초대는 410 `INVITE_INVALID`. 이미 구성원이면 초대를 쓰고 있던 소속을 그대로 준다 |
| `GET /demo/orgs` | 키 | 체험하기로 볼 수 있는 시연 조직 `[{id, name, kind}]`(조직 목록의 `order` 순) |

- `kind`는 `emitter | vendor | admin`(조직 레코드의 `role`과 같다), `memberRole`은 `owner | member`다. `POST /orgs`로는 `admin`을 만들 수 없다.
- `POST /orgs`의 응답: `{id, name, kind, bizNo, region, address, location, demo: false, createdAt, memberRole: "owner"}`. `id`는 `org_` + 16진 난수 12자라 시연 조직 ID와 겹치지 않는다. `bizNo`는 숫자 10자리를 `000-00-00000`으로 맞춰 둔다. 조직 레코드 자체는 8장의 저장소(`GET /orgs`)에 들어가므로 8장의 모든 경로가 새 조직에도 그대로 돈다.
- 새 배출 기업은 흐름 없이 시작한다. 흐름은 `PUT /orgs/{orgId}/streams/{streamId}`(`name` 필수)로 만든다. **흐름 ID는 모든 조직에 걸쳐 하나다.** 다른 조직이 이미 쓴 ID면 404 `STREAM_NOT_FOUND`이므로 BFF가 `st_` + 난수처럼 겹치지 않는 ID를 만든다.
- 새 업체는 빈 프로필(`permits: [], criteria: [], fleet: {}, guardrails: {}`)로 시작한다. `PUT /vendors/{orgId}`와 문서 올리기로 채운다. 허가 품목이나 매입 기준이 하나라도 생겨야 업체 탐색에 나온다. 실제 배출 기업은 프로필을 채운 실제 업체를 찾고, 체험하기의 시연 배출 기업은 시연 업체만 찾는다.
- 비밀번호는 `hashlib.scrypt`(n=2^14, r=8, p=1, 사용자마다 16바이트 소금)로 저장하고 응답과 기록에 싣지 않는다. 확인은 `hmac.compare_digest`로 하고 없는 이메일도 같은 계산을 한다. 로그인 실패 횟수는 서버 프로세스 메모리에서 이메일마다 센다(로그인에 성공하면 지운다).
- 초대 토큰은 32바이트 난수(URL에 쓸 수 있는 글)이고 sha256 값만 저장한다. 7일 뒤 만료되고 한 번만 쓸 수 있다. 토큰 원문은 만든 응답에만 한 번 나간다.
- 저장: 메모리 모드는 프로세스 안, SQL 모드는 `v3_users`, `v3_memberships`, `v3_invites`(마이그레이션 `0005_v3_accounts`). 조직 레코드는 `v3_records`(kind `orgs`)에 그대로 둔다. 조직 고치기·구성원 빼기·비밀번호 바꾸기·탈퇴는 같은 테이블의 행을 고치거나 지우므로 새 마이그레이션이 없다.

### 협상 테스트 회사 (`POST /orgs/negotiation-test`)

로그인한 사용자의 테스트 배출 기업을 열고, 처음 부르면 사용자를 owner로 등록한다. 응답은 조직 정보이고 소속을 조회하면 그 회사의 `simulation: true`가 함께 나온다. 테스트 회사는 PP, PE, LDPE, ABS, PS, PET, PP+ABS의 견적을 가상 업체 세 곳에 요청한다. 조건이 맞아 견적을 낸 업체와 각각 한 차례 단가를 조정한 뒤 수거비를 포함해 비교한다. 각 회사의 폐기물과 대화는 그 회사 소속에서 볼 수 있다. 가상 업체는 일반 매칭과 체험하기 검색에서 제외한다.

매입 조건의 범위는 [한국환경공단 재활용가능자원 가격조사](https://www.data.go.kr/data/3076421/fileData.do), [ReMA PE 필름 등급 기준](https://www.isrispecs.org/wp-content/uploads/2026/01/2026-January-ReMA_ISRI_Specifications.pdf), [Cyclomer LDPE 인수 조건](https://cyclomer.co.uk/we-buy/ldpe-film)을 참고했다. 제시 단가, 최소 물량 및 운반비는 테스트용 가정이다.

### 조직 고치기 (`PATCH /orgs/{orgId}`)

- owner만 부른다(구성원이 아니면 403 `NOT_A_MEMBER`, member면 403 `OWNER_ONLY`). 시연 조직과 운영 조직(시드 조직. 시연 되돌리기가 다시 올린다)은 owner여도 403 `DEMO_READ_ONLY`다.
- 보낸 항목만 바꾼다. 검사는 `POST /orgs`와 같다: `name`은 1~100자(비울 수 없다), `bizNo`는 숫자 10자리(`000-00-00000`으로 맞춘다), `region`·`address`는 200자 이하, `location`은 `[경도, 위도]`. `bizNo`, `region`, `address`, `location`은 `null`이나 빈 글이면 비운다. 틀리면 422 `INVALID_VALUE`이고 아무것도 바꾸지 않는다.
- `kind`는 바꿀 수 없다(지금 값과 다르면 422 `INVALID_VALUE`, 같으면 무시한다). 모르는 항목은 무시한다.
- 바뀐 이름과 위치는 다음 진행부터 쓰인다. 이미 나간 메시지의 글은 바뀌지 않는다.

### 구성원과 소유자

- `DELETE /orgs/{orgId}/members/{userId}`: owner는 누구든 빼고, member는 자기만 뺀다(남을 빼려 하면 403 `OWNER_ONLY`). 그 사용자가 구성원이 아니면 404 `MEMBER_NOT_FOUND`. 빼려는 사람이 그 조직의 마지막 owner면 409 `LAST_OWNER`(자기가 나가는 경우도). 빠진 사람이 그 조직에 만들어 두고 아직 쓰지 않은 초대는 함께 지운다.
- `POST /orgs/{orgId}/owners/{userId}`: owner가 다른 구성원을 owner로 지정한다. owner는 여럿일 수 있다. owner를 member로 내리는 경로는 없다(그 사람을 빼고 다시 초대한다).
- 두 owner가 동시에 서로를 빼면 owner가 남지 않을 수 있다(확인과 지우기가 한 트랜잭션이 아니다). 그때는 운영자가 `grant-admin`과 같은 방법으로 넣는다.

### 비밀번호 바꾸기 (`POST /accounts/password`)

- `{currentPassword, newPassword}` → 204. 지금 비밀번호가 틀리면 401 `INVALID_CREDENTIALS`, 새 비밀번호가 8자 미만·256자 초과이거나 지금과 같으면 422 `INVALID_VALUE`.
- 틀린 횟수는 로그인과 같은 제한(이메일마다 15분에 10번, `app.login_limiter`)에 센다. 넘으면 429 `TOO_MANY_ATTEMPTS`이고 그동안 로그인도 막힌다. 맞으면 그 이메일의 실패 기록을 지운다.
- 새 비밀번호는 새 소금으로 같은 scrypt 값으로 저장한다. 백엔드에는 로그인 세션이 없으므로, 다른 기기의 로그인 세션을 끊는 일은 BFF가 한다.

### 탈퇴 (`DELETE /accounts/me`)

본문 `{password}`로 지금 비밀번호를 다시 확인한다(틀리면 401 `INVALID_CREDENTIALS`, 횟수 제한은 위와 같다). 그다음 사용자가 속한 조직마다 정한다.

| 사용자의 자리 | 하는 일 |
|---|---|
| member, 또는 다른 owner가 있는 조직 | 소속만 지운다. 조직과 자료는 그대로다 |
| 혼자 owner이고 다른 구성원이 있는 조직 | 409 `TRANSFER_OWNERSHIP_FIRST`(조직 이름을 문장에 싣는다). 아무것도 지우지 않는다. 먼저 `POST /orgs/{orgId}/owners/{userId}`로 넘긴다 |
| 혼자 owner이고 다른 구성원이 없는 조직 | 조직을 함께 지운다(아래 표). 시연 조직과 운영 조직은 지우지 않고 소속만 지운다 |

지울 조직이 들어간 진단(그 조직이 배출 기업인 진단, 그 업체와 대화가 있는 진단)이 지금 진행 중이면 409 `RUN_IN_PROGRESS`이고 아무것도 지우지 않는다. 막히지 않으면 조직을 먼저 지우고 사용자를 마지막에 지운다. SQL 저장에서 지우기는 여러 트랜잭션으로 나뉜다. 도중에 실패하면(500) 사용자는 남아 있으므로 다시 부르면 남은 것을 지운다(같은 일을 다시 해도 결과가 같다).

**지우는 것과 남기는 것.**

| 자료 | 처리 | 까닭 |
|---|---|---|
| 사용자 레코드(이메일, 이름, 비밀번호 scrypt 값), 그 사람의 모든 소속 | 지운다 | 개인 정보다. 같은 이메일로 다시 가입하면 새 사용자다 |
| 그 사람이 만들었거나 쓴 초대 | 지운다 | |
| 로그인 실패 기록(프로세스 메모리) | 지운다 | |
| 지우는 조직의 흐름(폐기물), 업체 프로필 | 지운다 | 다만 다른 배출 기업과 **확정한** 대화가 있는 업체의 프로필은 `departed: true`로 남기고 업체 탐색에서 뺀다. 상대의 확정 기록(결과 한 장, 이행 계획서, 최적안 다시 계산)이 그 매입 기준·차량 값에 기댄다 |
| 지우는 배출 기업의 진단과 그 안의 모든 것(업체와의 대화와 업체 측 메시지, 질문, 승인, 문서, 실행 기록, 실시간 진행 기록) | 지운다 | 업체 화면에서도 그 진단이 사라진다 |
| 지우는 업체와 다른 배출 기업 사이의 대화 | 남긴다. 진행 중·합의한 대화는 코드가 쓴 `handoff`로 닫는다(`statusReason`: "상대 업체가 서비스를 떠나 이 대화를 마쳤습니다.", 승인과 배정을 지우고 최적안을 다시 계산한다). 열지 않은 대화는 같은 이유로 열지 않는다. 확정한 대화와 이미 닫힌 대화는 그대로다 | 상대 배출 기업의 기록이다. 이미 주고받은 메시지 글에 그 업체 이름이 남을 수 있다 |
| 지우는 조직만 볼 수 있는 근거(측정값, 올린 문서, 사진 추정, 교정 기록, 업체 입력값, 그 조직이 부른 판단 도구의 계산 등) | 지운다 | 남긴 업체 프로필이 가리키는 근거는 남긴다 |
| 다른 조직에도 열린 근거(대화가 열려 상대에게 보인 흐름 값·계산, 허가증·매입 기준, 업체의 실제 결과 기록 등) | 남긴다(내용과 접근 목록 그대로) | 상대가 받은 메시지와 문서가 인용한 기록이다. 접근 목록에 남은 지운 조직 ID는 구성원을 들일 수 없어 아무도 쓰지 못한다 |
| 조직 레코드 | 개인·사업자 정보가 없는 자리로 바꾼다: `{id, name: "탈퇴한 배출 기업" \| "탈퇴한 업체", role, demo: false, deleted: true, deletedAt}`. 사업자등록번호, 지역, 주소, 위치, 원래 이름은 지운다 | 남은 대화와 근거가 그 ID를 가리키고, ID를 다시 쓰지 않게 한다. 조직 목록(`GET /orgs`)에는 그 조직과 대화가 남은 상대에게만 나온다. 구성원을 들일 수 없다(초대도 운영자 명령도 막는다) |
| 그 조직의 소속과 초대 | 지운다 | |

- 운영자는 `python -m scripts.v3.accounts_admin delete-user <이메일>`로 같은 규칙으로 지운다(비밀번호를 묻지 않는다). `--force-org`를 주면 혼자 owner인 조직을 다른 구성원이 있어도 지운다(그 구성원들의 소속도 지운다). `list-users --like <찾기 글>`은 이메일이나 이름으로 사용자를 찾는다(대소문자 무시, `*`·`%`는 아무 글자열, 없으면 포함 검색. 비밀번호 값은 내보내지 않는다). 둘 다 백엔드 컨테이너 안에서 서버와 같은 설정으로 돈다.

### 노선·차량 (`PUT /vendors/{orgId}`의 `fleet`)

실제 업체도 시연 업체와 같은 `fleet`을 `PUT /vendors/{orgId}`(업체 자신만)로 넣는다. 보낸 항목만 바꾸고 `null`이면 그 항목을 지운다. `fleet: null`이면 바꾸지 않는다. 검사는 `app/v3/fleet.py`이고 틀리면 422 `INVALID_VALUE`(문장이 어느 항목인지 말한다)이며 프로필을 바꾸지 않는다.

```json
{"fleet": {
  "depot": [128.97, 35.09],
  "capacityKg": 3000, "capacityM3": 15, "costPerKm": 900,
  "vehicleCount": 3,
  "serviceAreas": ["부산 강서구", "경남 김해시"], "maxPickupKm": 40,
  "routes": [
    {"name": "강서 화목 노선", "days": ["화", "목"], "regions": ["부산 강서구"], "complexes": ["녹산국가산업단지"]}
  ]
}}
```

| 항목 | 검사 | 계산에 쓰는 곳 |
|---|---|---|
| `depot` | `[경도, 위도]`, 경도 -180~180, 위도 -90~90 | 업체 탐색의 거리와 순위, 운송비 어림, 노선 추가 비용(차고지) |
| `capacityKg` | 0보다 크고 100,000 이하 | 적재 검사(한 번 수거량 ≤ 차량 한 대의 적재 중량), 노선 추가 비용 |
| `capacityM3` | 0보다 크고 1,000 이하 | 부피 적재 검사 |
| `costPerKm` | 0 이상 1,000,000 이하 | 운송비 어림, 노선 추가 비용. 업체만 보는 값이다(상대에게 나누는 계산 근거에서 뺀다) |
| `vehicleCount` | 1 이상 1,000 이하의 정수 | 아직 쓰지 않는다(저장·표시) |
| `serviceAreas` | 수거 가능 지역. 50자 이하 글 30개 이하(시·도, 시·군·구, 산업단지 이름. 예: "부산", "경남 김해시", "녹산국가산업단지"). 겹침을 지운다 | 결과의 `pickup_area` 확인 항목과 계약 검토(2026-11-19) |
| `maxPickupKm` | 차고지에서 수거하러 가는 최대 거리. 0보다 크고 1,000 이하 | 결과의 `pickup_area` 확인 항목과 계약 검토. 값은 상대에게 보이지 않는다 |
| `routes` | 30개 이하. 항목마다 `name`(1~50자, 노선끼리 겹치지 않음), `days`(`월`~`일`에서 하나 이상. 요일 순으로 맞추고 겹침을 지운다), `regions`(수거 지역, 50자 이하 글 30개 이하), `complexes`(산업단지, 100자 이하 글 30개 이하). `regions`나 `complexes`가 하나는 있어야 한다. 다른 항목은 받지 않는다 | 아직 쓰지 않는다(저장·표시) |

- `evidence`는 서버가 정한다. 보내도 버린다(GET으로 받은 `fleet`을 그대로 돌려보내도 된다). 그 밖의 모르는 항목은 422다.
- `capacityKg`, `capacityM3`, `costPerKm`은 저장할 때 업체 입력 근거(human)가 되어 `fleet.evidence`에 들어간다(값이 같으면 있던 근거를 그대로 쓴다).
- 운행 요일은 노선마다 적지만, 업체 측 AI가 제안에 쓰는 수거 요일의 한계는 전처럼 `guardrails.days`다.
- 지금 판단 도구는 `depot`, `capacityKg`, `capacityM3`, `costPerKm`만 쓴다. 노선 추가 비용(`route_extra_cost`)의 기존 정류장은 `routes`가 아니라 그 업체가 다른 배출 기업과 합의·확정한 대화의 공장 위치다. `vehicleCount`, `routes`, `serviceAreas`, `maxPickupKm`은 판단 도구 입력(`vendor.public_profile`)에 싣지 않는다(계산 근거 ID가 바뀌지 않는다). 업체 탐색은 허가 품목·매입 기준과 차고지 거리로 고르고 수거 지역으로 거르지 않는다.
- **수거 가능 지역 확인(`result.assignments[].checks[]`의 `pickup_area`, 2026-11-19).** 업체가 `serviceAreas`나 `maxPickupKm`을 밝혔을 때만 나간다. 공장 조직의 `region` 글에 수거 가능 지역의 낱말(시·도만 적었으면 그 시·도, 시·군·구나 산업단지를 적었으면 그 낱말이 모두. "국가산업단지"·"산단" 같은 꼬리는 뗀다)이 들어 있거나 공장 → 업체 거리(`transportKm`)가 `maxPickupKm` 이하이면 `met`, 거리를 몰라 대조하지 못했으면 `conditional`, 둘 다 밖이면 `unmet`이다. 시·도를 함께 적었으면 공장 지역의 시·도와 같아야 한다(부산 강서구와 서울 강서구를 가른다). `unmet`은 계약 검토에서 막힘이 된다. 배정은 바꾸지 않는다.

### 운반 조건 (`PUT /vendors/{orgId}`의 `transport`, 2026-11-19)

업체가 업체 정보에서 노선 수거의 운반비와 **수거 노선 밖 공장의 별도 수거 조건**을 정한다(업체 자신만). 검사는 `app/v3/transport_input.py`이고 틀리면 422 `INVALID_VALUE`(문장이 어느 항목인지 말한다)이며 프로필을 바꾸지 않는다. `transport: null`이면 바꾸지 않고, `offRoute: null`이면 노선 밖 조건만 지운다. 최상위에 `basis`·`paidBy`·금액이 없으면 노선 수거 조건은 그대로 둔다.

```json
{"transport": {
  "paidBy": "vendor",
  "offRoute": {"status": "visit", "basis": "separate", "feePerPickupKrw": 30000, "pickupsPerMonth": 2}
}}
```

| 항목 | 검사 |
|---|---|
| `basis` | `included`(단가에 포함, 업체 부담) · `separate`(따로 받음, 공장 부담) · `undecided`(미정). 없으면 `paidBy`로 정한다(`vendor` → 포함, `emitter` → 따로) |
| `feePerPickupKrw` · `feePerMonthKrw` | `separate`일 때 하나만, 0보다 크고 1,000만 이하. 둘 다 없으면 금액 미정(금액 확인 필요)이다 |
| `pickupsPerMonth` | 회당 금액일 때만, 0보다 크고 100 이하. 모르면 비운다(월 운반비를 정하지 않는다) |
| `offRoute.status` | `visit`(노선 밖 공장도 따로 수거하러 감) · `unavailable`(수거 불가. 운반비 항목을 받지 않는다) |

- 화면의 네 가지 답: 수거 불가 = `{status: unavailable}`, 수거 가능·추가비 없음 = `{status: visit, basis: included}`, 수거 가능·추가비 있음 = `{status: visit, basis: separate, 금액, 횟수}`, 비용 미정 = `{status: visit, basis: undecided}`. 이 조건을 판단에 쓰는 규칙은 `app/v3/judge/pickup.py`다.
- 저장하면 담당자 입력 근거(human)가 생긴다: `evidence` = [금액, 횟수, 조건 문장], `feeEvidence` = [금액(원/회·원/월)], `pickupsEvidence` = [횟수(회/월)]. 출처는 "{업체 이름} 담당자 입력(업체 정보 · 노선 밖 공장 수거 조건)", 기준일은 저장한 날, 원문은 고른 조건 문장이다. 같은 조건을 다시 보내면 근거 ID와 판이 그대로이고, 조건을 바꾸면 같은 ID의 판이 오른다(그 근거를 인용한 대화는 바뀐 것을 안다). 이미 대화 상대에게 열린 근거는 고쳐 써도 계속 열려 있다.
- 저장 뒤에는 다른 업체 정보 수정과 같이 승인을 기다리는 진단을 다시 확인한다(합의 뒤 수거·운반 조건이 바뀌면 승인이 막히고 앞선 승인이 지워진다).
- `evidence`, `feeEvidence`, `pickupsEvidence`는 서버가 정한다. 보내도 버린다(GET으로 받은 `transport`를 그대로 돌려보내도 된다). 매입 기준표 저장(`POST /intake/criteria/confirm`)은 노선 수거 조건만 바꾸고 `offRoute`는 그대로 둔다.

### 체험하기 (`X-Demo-View: 1`)

- 시연 조직은 시드 조직 가운데 운영 조직을 뺀 다섯 곳(`org_hanbit`, `org_daon`, `org_saegil`, `org_namhae`, `org_miso`)이고 조직 레코드에 `demo: true`가 붙는다. 운영 조직 `org_admin`은 관리자 화면을 여는 조직이라 시연 조직이 아니다(`demo: false`).
- `X-Demo-View: 1`과 `X-Org-Id`(시연 조직)만 있으면 `X-User-Id` 없이 8장의 경로를 부를 수 있다. **GET만 받는다**(그 밖은 403 `DEMO_READ_ONLY`). `X-Org-Id`가 시연 조직이 아니면 403 `NOT_DEMO_ORG`. 이 헤더가 있으면 `X-User-Id`가 있어도 체험하기로 다룬다.
- 체험하기 요청의 서비스 함수는 시연 조직만 보이는 읽기 전용 저장소·근거 레지스트리(`app/v3/demo/scope.py`)로 돈다. 실제 조직, 그 흐름·진단, 실제 배출 기업이 시연 업체와 나눈 대화, 실제 조직이 가진 근거는 어떤 경로로도 보이지 않는다. 시연 업체의 허가증·매입 기준 근거(doc)는 실제 배출 기업에도 열려 있을 수 있는데, 체험하기에서는 `access`에서 실제 조직을 지우고 보여 준다. 저장소에 쓰려 하면 403 `DEMO_READ_ONLY`이고 뷰를 만들 때의 재확인(쓰기)도 하지 않는다.
- `/admin/*`는 시연 조직이 관리자가 아니므로 403 `FORBIDDEN`이다.

### 시연 되돌리기 (bff 모드)

- `POST /demo/reset`은 관리자(`org_admin`의 구성원)만 부른다(8장 "관리자 조직"과 같다. 조직이 하나도 없는 빈 저장소의 첫 시드는 키만 있으면 부를 수 있다).
- bff 모드에서는 **시연 조직의 자료만** 지우고 다시 올린다: 시드 조직(운영 조직 포함)의 조직·흐름·업체 프로필, 시연 배출 기업의 진단(스레드, 질문, 승인, 문서, 실행 기록), 시드 조직만 볼 수 있는 근거. 실제 조직의 조직·흐름·진단·근거, 사용자와 소속, ID 카운터는 그대로다. 시연 업체 근거 가운데 실제 조직에 열린 것은 지우지 않고 다시 올릴 때도 그 조직들을 열어 둔다(내용이 같으면 판도 그대로다).
- 다시 올린 뒤 시연 배출 기업 `org_hanbit`의 진단 한 건을 배포의 모델 설정(회사 측·업체 측 모두 실제 모델, Ollama를 고른 시연 업체는 Ollama)으로 승인 대기(`awaiting_approval`)까지 돌려 둔다. 모델이 없거나 오류가 나거나 승인 대기까지 가지 못하면 규칙 정책으로 다시 돈다. 질문에는 시연 답(`seed.DEMO_ANSWERS`: 포장 필름 수분 2.0%, 가상 값)으로, 그 밖에는 "모름"으로 답한다. 응답에 `demoSessionId`, `demoSessionStatus`가 더 붙는다.
- 운영자는 HTTP를 거치지 않고 `python -m scripts.v3.accounts_admin reset-demo`로도 되돌린다(관리자 구성원이 아직 없는 처음 배포). header·token 모드의 되돌리기는 전처럼 저장소 전체를 비운다.

## 10. 작업 규칙

- 커밋 작성자는 `Jeonsubb <glvlglvl01@naver.com>`이다. 커밋 메시지는 한국어로 쓰고 `feat:`, `fix:`, `test:`, `docs:` 접두어를 붙인다.
- 로컬에서는 네이티브 확장을 쓸 수 없다. 핵심 모듈과 그 테스트는 표준 라이브러리와 pytest만으로 돌아야 한다.
- 테스트 실행: `PYTHONPATH=<pydeps>:. python3 -m pytest -q tests/v3` 와 `python3 -m ruff check app/v3 tests/v3` (줄 길이 100).
- 자기 담당 경로 밖의 파일은 고치지 않는다. 계약을 바꿔야 하면 이 문서에 적고 보고한다.
- 비밀값을 코드, 테스트, 문서에 넣지 않는다. 시연 데이터의 회사와 수치는 모두 가상이라고 표시한다.
- 화면과 메시지 문구는 한국어로 쓴다. 이름 뒤 조사는 `app/v3/textutil.py`의 `josa(word, "은는")`(`"이가"`, `"을를"`, `"와과"`, `"으로"`)로 붙인다. "은(는)", "이(가)" 같은 표기를 쓰지 않는다.

### 조건 변경 감지와 재탐색 (2026-11-22)

업체 프로필 수정 및 매입 기준표 확인 저장이 최신 승인 대기·확정 진단의 상대 업체 조건을 바꾸면 새 진단을 만든다. 원래 진단에는 replacementSessionId를, 새 work.adaptive에는 출발 진단·이전 결과·진행 상태를 보관한다. 업체 변경으로 영향받은 이전 합의는 기존 근거·조건 검증으로 승인을 막는다(담당자가 기존 대화를 수동 재협상하는 경로는 유지한다). 조건 비교를 적용해 대체한 이전 안은 새 찾기에서 승인해야 한다. 확정된 거래의 서류·검수는 변경하지 않는다. HTTP 요청 뒤 큐를 실행하며 서버 재시작 뒤에는 다음 요청에서 남은 큐를 재개한다. 실행 자리로 중복 실행을 막고 자동 재시도는 세 번으로 제한한다. 신규 질문은 임의로 답하지 않는다.

미리보기는 별도 메모리 저장소에서 기존 판단 도구와 규칙 정책으로 계산한다. 실제 적용 후에는 설정된 AI로 새 대화를 진행하므로 예상 결과와 다를 수 있다. 최대 수거 횟수는 업체가 밝힌 수거 횟수·최소 횟수로 후보를 제한하며, 업체 횟수를 임의로 낮추지 않는다. 자료가 없는 업체는 제외한다.

## 10. 설문 후속 입력 (2026-11-22)

업체 매입 기준은 기존 unitPriceKrwPerKg 또는 공개 범위 priceLow·priceHigh를 가진다. 범위를 등록하면 단일가는 null이다. 범위는 후보 안내용이고 임의의 중간값으로 배정하지 않는다. 가격 범위를 유지한 문서 행도 담당자 확인 후 저장할 수 있다. 공개 범위 근거는 다른 조직이 읽을 수 있지만 내부 협상 한도는 공개하지 않는다.

수거 정책 collectionPolicy는 quantity, distance_vehicle, combined. distance_vehicle은 최소 kg 기준을 적용하지 않지만 기존 수거 가능 지역·거리와 적재량은 확인한다.

거래별 문의는 JSON survey_records에 저장한다. narrow는 이전 범위 안에서만 가능하며 담당자 사유·확인 항목·이력을 남긴다. 단일가 제안 전 재질·품질·운반 조건을 확인하고 배출 기업이 accept해야 해당 폐기물·업체의 단가로 기존 진단에 쓰인다. 자료나 업체 조건이 바뀌면 재사용하지 않는다. 실제 거래의 양쪽 승인·허가 검토는 별도로 유지한다.

계량 기록에는 오염도·수분이 필요 없다. 선택한 월의 기록만 합산한다. 계량번호가 없으면 같은 폐기물·날짜·중량은 같은 기록으로 묶이므로 다른 반입은 계량번호로 구분한다. 사진은 JPEG로 다시 인코딩해 메타데이터를 제거하고 소유자가 선택한 문의 업체만 열람한다. 앱 밖으로 자동 발송하지 않는다. 노선 비교는 앱에서 확정한 방문만 기준으로 하며 실제 전체 노선 최적화를 의미하지 않는다.
