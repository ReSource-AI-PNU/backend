# 두 에이전트와 진행기

회사 측 에이전트와 업체 측 에이전트가 어떻게 대화하고, 진단 한 건이 어떤 순서로 진행되는지 적는다. 설계안 v3 4.3, 4.4를 읽지 못한 상태에서 구현 지시문을 바탕으로 정한 것이다.

## 1. 모델이 숫자를 만들지 못하게 하는 구조

에이전트가 메시지를 쓸 때 모델은 값을 직접 적지 않는다.

1. 코드가 **claim 풀**을 만든다. 풀에는 이번 턴에 쓸 수 있는 claim만 들어간다: 판단 도구가 돌려준 claims, 흐름과 업체 프로필의 값(근거가 붙은 것), 상대가 보낸 메시지의 claims.
2. 모델은 다음 JSON만 낸다.

```json
{"type": "offer", "stage": 5, "streamIds": ["st_sprue"],
 "text": "분쇄 상태로 받으면 단가는 {{unitPrice}}입니다.",
 "claimRefs": ["unitPrice"],
 "toolCalls": [{"name": "judge_route", "arguments": {}}]}
```

3. 코드가 `claimRefs`를 풀에서 찾아 claims를 채운다. 풀에 없는 ID를 쓰면 그 자체로 거부다.
4. 근거 검증기가 메시지를 검사한다. 거부되면 거부 사유를 모델에게 돌려주고 한 번 다시 쓰게 한다. 두 번째도 거부되면 `handoff` 메시지를 보내고 스레드를 사람에게 넘긴다.
5. 통과한 메시지만 스레드에 붙고 상대에게 전달된다. 모든 시도는 실행 기록에 남는다.

모델이 정하는 것은 무엇을 물을지, 어떤 도구를 부를지, 결과를 어떻게 설명할지다.

## 2. 모델 구현 세 가지

`app/v3/agents/llm.py`의 `LLM` 프로토콜을 따른다.

| 구현 | 쓰임 |
|---|---|
| `FoundryChat` | 회사 측 에이전트와 업체 측 기본 모델 |
| `OllamaChat` | 업체가 오픈 모델을 고른 경우 |
| `RulePolicy` | 네트워크 없이 도는 결정적 정책. 로컬 시연과 테스트에 쓴다. 화면에는 모델 이름 대신 "규칙 정책(모델 미연결)"이라고 표시한다 |

`RulePolicy`가 낸 결과를 모델이 낸 것처럼 보여 주지 않는다. 진단 뷰의 `vendorModel`과 실행 기록에 실제로 쓴 구현 이름이 그대로 남는다.

## 3. 진단 한 건의 순서

`orchestrator.run(session_id)`는 멈춤 지점까지 진행하고 돌아온다. 다시 부르면 이어서 진행한다.

| 단계 | 하는 일 | 멈춤 |
|---|---|---|
| ① 식별·분류 | 흐름마다 `identify_stream`. 측정값과 사진 추정이 다르거나 필요한 값이 없으면 담당자 질문을 만든다 | 질문이 있으면 `awaiting_answers` |
| ② 가능성 판단 | 흐름마다 `judge_route`. 조건부의 "확인할 것"은 업체에 보낼 질문으로 쌓아 둔다 | |
| ③ 규정 검토 | `check_requirements`. 조항 인용은 원문 부분 문자열만 쓴다. 조항 근거가 없으면 "확인 필요" | |
| ④ 업종·기업 탐색 | `find_vendors`와 `match_permit`. 허가 품목이 맞는 업체와만 스레드를 연다. 맞지 않는 업체는 `not_opened`와 이유, 허가증 근거를 남긴다. 연 스레드마다 `inquiry`를 보낸다 | |
| ④~⑤ 대화 | 업체 측 에이전트가 `question`, `offer`, `conditional`, `decline` 중 하나로 답한다. 회사 측은 근거로 답하거나(`answer`), 조건을 다시 제안한다(`counter`). 스레드마다 12턴을 넘기면 `handoff` | 담당자만 아는 값이면 `awaiting_answers` |
| ⑤ 비용·탄소 비교 | 받은 제안마다 `cost_carbon`과 `check_loading`, 업체 쪽은 `route_extra_cost` | |
| ⑥ 최적안 제안 | `optimize_assignment`로 흐름별 업체를 정하고 결과 한 장을 만든다. 선택되지 않은 제안은 대안으로 남긴다 | `awaiting_approval` |
| 승인 | 양쪽 담당자가 스레드를 승인하면 `confirm`을 보내고 요청서와 이행 계획서를 만든다. 문서도 검증기를 통과해야 한다 | `confirmed` |

담당자 질문은 한 묶음에 3개까지, 진단 한 건에 2묶음까지다. "모름"은 유효한 답이고 같은 질문을 다시 하지 않는다. 질문을 다 쓰고도 모르는 값은 "확인 필요"로 남기고 진행한다.

## 4. 스레드를 닫는 경우

| 경우 | 메시지 | 스레드 상태 |
|---|---|---|
| 허가 품목이 맞지 않음(열기 전) | 없음 | `not_opened` |
| 허가 품목이 맞지 않음을 연 뒤에 알게 됨 | `decline` (근거 포함) | `declined` |
| 업체 가드레일의 자동 거절 조건에 해당 | `decline` | `declined` |
| 12턴 초과 | `handoff` | `handoff` |
| 검증기 두 번 거부 | `handoff` | `handoff` |
| 업로드 문서에서 지시문 주입 탐지 | `handoff` | `handoff` |
| 양쪽 승인 | `confirm` | `confirmed` |

업체 측 에이전트는 가드레일(하한 단가, 가능 요일, 월 최소 횟수, 자동 거절 조건) 밖의 조건을 내지 못한다. 가드레일 검사는 검증기와 별도로, 메시지가 나가기 전에 코드가 한다.

업체 측 에이전트는 여러 배출 기업의 물량을 자기 노선에 묶어 계산할 수 있지만, 한 배출 기업에게 다른 배출 기업의 이름이나 물량을 알려 주지 않는다. `policy.check_disclosure`가 나가는 메시지를 검사한다.

## 5. 업체 측 에이전트의 위치

업체 측 에이전트는 별도 서비스로 띄울 수 있다. 회사 측은 `VendorGateway` 인터페이스로만 부른다.

```python
class VendorGateway(Protocol):
    def deliver(self, vendor_org: str, message: dict, context: dict) -> dict: ...   # 답 메시지(dict)
```

- `LocalVendorGateway`: 같은 프로세스에서 업체 측 에이전트를 부른다(로컬 시연, 테스트).
- `HttpVendorGateway`: `POST {base}/a2a/messages`로 보낸다. 본문은 `{"message": {...}, "context": {...}}`, 답은 `{"message": {...}}`. 서비스 키는 `Authorization: Bearer` 헤더로 보낸다.

받은 답 메시지는 회사 측에서 다시 검증기를 통과시킨다. 보내는 쪽이 검증했다는 말을 믿지 않는다.

## 6. 서비스 함수

`app/v3/service.py`는 프레임워크와 무관한 함수 모음이고, `api.py`(FastAPI)와 `devserver.py`(표준 라이브러리 HTTP 서버)가 같은 함수를 부른다.

```python
class App:                      # 저장소, 레지스트리, 모델, 게이트웨이, 시계를 묶는다
    def __init__(self, store, registry, *, llm_factory, vendor_gateway=None, today=None): ...

def reset_demo(app) -> dict
def list_orgs(app) -> list[dict]
def list_streams(app, org_id, viewer) -> list[dict]
def update_stream(app, org_id, stream_id, patch, viewer) -> dict
def get_vendor(app, org_id, viewer) -> dict
def update_vendor(app, org_id, patch, viewer) -> dict
def create_session(app, emitter_org_id, viewer) -> dict            # 진단 뷰
def get_view(app, session_id, viewer) -> dict                       # 계약 7장
def run_session(app, session_id, viewer) -> dict                    # 멈춤 지점까지
def answer_question(app, session_id, question_id, value, viewer) -> dict
def approve(app, session_id, thread_id, viewer) -> dict
def get_evidence(app, evidence_id, viewer) -> dict                  # {"evidence", "chain"}
def search_evidence(app, query, kinds, viewer) -> list[dict]
def csv_template(app) -> bytes
def upload_csv(app, org_id, filename, data, viewer) -> dict
def upload_document(app, org_id, kind, filename, data, viewer) -> dict
def admin_status(app, viewer) -> dict
def run_eval(app, viewer) -> dict
```

`viewer`는 요청한 조직 ID다. 권한이 없으면 `ServiceError(code, message, status)`를 낸다. 업체가 보는 진단 뷰에는 자기 스레드와 그 스레드의 흐름만 들어간다.

## 7. 실행 기록

모든 메시지, 도구 호출, 검증 결과, handoff, 승인은 `store`의 events에 남는다. 화면의 대화와 실행 기록은 이 기록을 그대로 보여 준다. OpenTelemetry로 내보내는 것은 `api.py` 쪽에서 같은 이벤트를 span으로 옮겨 한다.
