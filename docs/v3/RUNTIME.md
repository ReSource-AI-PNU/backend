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

구현하면서 정한 것(`app/v3/agents/draft.py`, `company.py`, `vendor.py`):

- **도구 입력은 ID뿐이다.** `toolCalls[].arguments`는 `{"streamId": "..."}` 또는 `{"streamIds": ["..."]}`만 받고, 코드가 흐름과 업체 레코드로 바꿔 판단 도구를 부른다. 모델이 지어낸 흐름을 도구에 넣어 근거를 만들 수 없다.
- **조건(terms)도 claim ID로 적는다.** 업체 측이 `offer`, `counter`, `conditional`을 낼 때는 `"terms": {"items": [{"streamId", "unitPriceRef"}], "days": [...], "pickupsPerMonthRef": "...", "conditions": [...]}`를 함께 낸다. 단가는 본문에 쓴 claim ID로 가리키고, 코드가 값으로 바꿔 가드레일을 검사한다. 본문에 쓴 단가 claim은 모두 `terms.items`에 있어야 한다.
- **메시지가 아닌 결정 두 가지.** 회사 측은 `type`에 `accept`(상대 조건을 그대로 받는다. 메시지는 나가지 않는다)와 `ask_user`(담당자만 아는 값이다. `userQuestions`에 `{"streamId", "field"}`를 적는다)를 쓸 수 있다. `field`는 `agents/questions.py`의 목록에서만 고르고, 질문 문구는 코드가 쓴다.
- **현행 처리 단가는 업체와 나누는 풀에 넣지 않는다.** 판단 도구가 현행 단가로 계산한 근거는 계산한 조직만 본다.
- **업체가 보낸 기록은 줄여서 남긴다.** 업체 측의 거부 사유는 코드와 일반 문장만 실행 기록에 남는다(가드레일 수치나 다른 배출 기업의 근거 ID가 새지 않게 한다).

## 2. 모델 구현 세 가지

`app/v3/agents/llm.py`의 `LLM` 프로토콜을 따른다.

| 구현 | 쓰임 |
|---|---|
| `FoundryChat` | 회사 측 에이전트와 업체 측 기본 모델 |
| `OllamaChat` | 업체가 오픈 모델을 고른 경우 |
| `RulePolicy` | 네트워크 없이 도는 결정적 정책. 로컬 시연과 테스트에 쓴다. 화면에는 모델 이름 대신 "규칙 정책(모델 미연결)"이라고 표시한다 |
| `MafChat`, `RegisteredAgentChat` (`agents/maf.py`) | Microsoft Agent Framework로 같은 Foundry 모델을 부른다. `V3_AGENT_FRAMEWORK=chat`이면 `FoundryChatClient`, `agent`이면 Foundry Agent Service에 등록한 Prompt Agent(`scripts/v3/register_foundry_agents.py`)다. 설정이 없으면 `FoundryChat` 그대로다 |

Agent Framework를 쓸 때도 고리는 같다. 모델은 JSON만 내고, 판단 도구는 모델이 직접 부르지 않는다(JSON의 `toolCalls`를 코드가 실행한다). 판단 도구 MCP 서버의 도구는 호출자가 `org_id`를 정하므로 모델에게 그대로 주지 않는다.

`RulePolicy`가 낸 결과를 모델이 낸 것처럼 보여 주지 않는다. 진단 뷰의 `vendorModel`과 실행 기록에 실제로 쓴 구현 이름이 그대로 남는다.

`vendorModel`은 `{"provider", "name", "configured": 업체가 고른 설정, "used": 실제로 답을 썼는지}`다. 규칙 정책으로 돌면 `provider`는 `rule`, `name`은 `규칙 정책(모델 미연결)`이고, 업체가 고른 `foundry | ollama` 설정은 `configured`에 따로 남는다. 메시지마다 `model`에도 같은 값이 붙고, 도구 호출 기록의 `calledBy`는 `orchestrator`(진행기가 정해진 순서로 부름) 또는 `model:<provider>`(에이전트가 고름)다.

`RulePolicy`도 모델과 똑같이 입력(JSON)을 받아 `type`, `text`, `claimRefs`, `toolCalls`를 내고, 같은 검증을 거친다.

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

실제 실행 순서(구현): 업체 매입 기준과 대조하려면 허가 품목이 맞는 업체가 먼저 정해져야 해서, 도구는 ① `identify_stream` → ④ `find_vendors` → ③ `match_permit` → (스레드 개설, 근거 공유) → ② `judge_route` → ③ `check_requirements` → ④ `inquiry` 순서로 불린다. 실행 기록의 `stage`는 도구가 속한 단계 번호다.

스레드는 업체마다 하나다. 허가 품목이 맞는 흐름은 한 스레드에 모으고, 맞지 않는 흐름이 있으면 그 업체의 `not_opened` 스레드를 하나 더 만들어 이유와 허가증 근거를 남긴다. 업체는 한 스레드 안에서 흐름별로 조건을 낸다. 자동 거절 조건에 걸린 흐름은 메시지의 `excluded`에 이유와 함께 적고, 받을 흐름이 하나도 없을 때만 `decline`으로 스레드를 닫는다. 최적안에 들어간 스레드만 `agreed`가 되고, `confirm`과 문서에는 그 스레드에 배정된 흐름만 들어간다. 선택되지 않은 제안은 `open` 상태로 남아 결과 한 장의 대안으로 보인다.

**조건이 붙는 답은 `conditional`이다.** 업체 측이 내는 `offer`는 조건 없이 그대로 받을 수 있는 제안일 때만 쓴다. 최소 물량에 못 미쳐 "기존 노선에 묶어 수거"하는 조건, 차량 용량을 넘어 "수거 횟수를 늘려 나눠 싣는" 조건, 부피 적재를 확인하지 못한 조건이 붙으면 `conditional`로 보낸다(`terms.conditions`가 비어 있지 않은 `offer`는 없다).

**측정값이 아닌 품질 값.** 오염도가 사진 추정이거나 수분이 담당자 답이면, 값이 매입 기준 상한 안이어도 `judge_route`는 충족을 확정하지 않는다. 대조 결과(`contaminationPct`, `moisturePct`)는 `met`이지만 `contamination_basis`, `moisture_basis` 검사가 `conditional`로 붙어 판정은 조건부가 되고, 업체는 "반입 검수에서 … 매입 기준 상한 이하로 확인될 것"을 조건으로 건다. 추정 물량(`monthly_kg_basis`)과 같은 원칙이다. 상한을 넘는 값은 출처와 관계없이 `unmet`이다.

**적재 검사.** 최적안이 정해지면 업체마다 실제로 배정된 흐름만 합쳐 `check_loading`을 다시 부르고, 그 결과(최소 물량, 무게·부피 적재)를 결과 한 장 각 배정의 `checks`에 넣는다.

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

**합의한 대화가 담당자에게 넘어갔을 때(`_reassign`).** 최적안에 들어가 `agreed`였던 대화가 `handoff`로 닫히면(업로드 문서의 지시문 주입, 확정 메시지 검증 실패 등):

1. 그 대화의 배정을 결과 한 장에서 빼고, 승인 기록을 지운다. 뷰의 `approvals`에도 나가지 않는다.
2. 그 업체를 후보에서 빼고 `optimize_assignment`를 다시 부른다. 이미 합의했거나 확정한 다른 대화의 배정은 옮기지 않는다.
3. 풀려난 흐름을 받을 다른 제안이 있으면 그 대화가 합의 상태가 되고(이미 합의한 대화면 배정된 흐름이 늘고, 앞서 한 승인은 지워져 다시 받는다), 없으면 그 흐름은 `needs_information`과 `handoff` 검사로 나간다(현행 유지를 "확정"으로 보이지 않게 한다).
4. 승인을 기다리는 대화가 남아 있으면 진단은 `awaiting_approval` 그대로, 남은 대화가 모두 확정이면 `confirmed`, 하나도 없으면 `idle`(`phase: done`)이 된다.

여러 대화가 한꺼번에 넘어가면(배출 기업이 올린 문서) 진단마다 한 번만 다시 계산한다. 넘어간 대화나 거절로 닫힌 대화의 제안은 결과 한 장의 대안에도 쓰지 않는다.

업체 측 에이전트는 가드레일(하한 단가, 가능 요일, 월 최소 횟수, 자동 거절 조건) 밖의 조건을 내지 못한다. 가드레일 검사는 검증기와 별도로, 메시지가 나가기 전에 코드가 한다.

근거 확인 실패와 승인: 합의한 스레드의 메시지는 승인 직전에 다시 검증한다. 검증에 실패하거나, 메시지가 인용한 근거가 대화 뒤에 바뀌었으면(`EVIDENCE_CHANGED`) `block_approval`로 승인을 막고 이유를 남긴다. 근거가 다시 맞으면 풀린다.

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

`context`는 `{"sessionId", "today", "thread": 스레드(메시지 포함), "streams": 그 스레드의 흐름(현행 처리 단가 제외), "emitter": {"id", "name", "location"}}`다. 답 메시지의 `meta`에는 `validation`(시도 횟수와 줄인 거부 사유), `model`(실제로 쓴 구현), `terms`, `excluded`, `trace`(업체 측 도구 호출과 검증 기록)가 붙고, 진행기가 `trace`를 실행 기록으로 옮긴다. `HttpVendorGateway`의 본문에는 `vendorOrgId`도 함께 간다.

## 6. 서비스 함수

`app/v3/service.py`는 프레임워크와 무관한 함수 모음이고, `api.py`(FastAPI)와 `devserver.py`(표준 라이브러리 HTTP 서버)가 같은 함수를 부른다.

```python
class App:                      # 저장소, 레지스트리, 모델, 게이트웨이, 시계를 묶는다
    def __init__(self, store, registry, *, llm_factory=None, vendor_gateway=None, today=None,
                 shields=None, documents=None, grade_client=None, distance_provider=None,
                 demo_open=False, detector=None): ...
    def llm_for(self, role, org_id, model) -> LLM      # llm_factory가 None을 주면 RulePolicy

def seed(app) -> dict                                   # 가정값, 근거 시드, 시연 데이터를 올린다

def reset_demo(app, viewer=None) -> dict                # 관리자만. App(demo_open=True)이면 누구나
def list_orgs(app) -> list[dict]                        # 조직 레코드의 order 순
def list_streams(app, org_id, viewer) -> list[dict]
def update_stream(app, org_id, stream_id, patch, viewer) -> dict
def get_vendor(app, org_id, viewer) -> dict
def update_vendor(app, org_id, patch, viewer) -> dict
def create_session(app, emitter_org_id, viewer) -> dict            # 진단 뷰
def get_view(app, session_id, viewer) -> dict                       # 계약 7장
def run_session(app, session_id, viewer) -> dict                    # 멈춤 지점까지
def answer_question(app, session_id, question_id, value, viewer) -> dict
def approve(app, session_id, thread_id, viewer) -> dict
def handle_vendor_message(app, vendor_org_id, message, context) -> dict   # 답 메시지(dict) 하나
def get_evidence(app, evidence_id, viewer) -> dict                  # {"evidence", "chain"}
def search_evidence(app, query, kinds, viewer) -> list[dict]
def csv_template(app) -> bytes
def upload_csv(app, org_id, filename, data, viewer) -> dict
def upload_document(app, org_id, kind, filename, data, viewer) -> dict
def confirm_document(app, org_id, document_name, fields, viewer) -> dict   # 확인한 추출값만 doc 근거로
def upload_media(app, org_id, stream_id, filename, data, viewer) -> dict  # 사진·영상 → 탐지 → model 근거
def admin_status(app, viewer) -> dict                               # 재학습 표(retrain) 포함. 관리자만
def run_eval(app, viewer) -> dict                                   # 사례 세트 + 시연 시나리오. 관리자만
def record_correction(app, session_id, body, viewer) -> dict        # 배출 기업 담당자가 고친 값
def record_outcome(app, session_id, body, viewer) -> dict           # 업체가 확정 뒤 기록하는 실제 결과
def export_retraining(app, viewer) -> dict                          # 재학습 행(관리자만)
def register_model_run(app, run, viewer) -> dict                    # 재학습 실행 결과 등록(관리자만)
```

`viewer`는 요청한 조직 ID다. 권한이 없으면 `ServiceError(code, message, status)`를 낸다. 업체가 보는 진단 뷰에는 자기 스레드와 그 스레드의 흐름만 들어간다.

**모델 고르기 (가장자리와 맞춘 계약).** `llm_factory(role, org_id, model) -> LLM | None`이다. `role`은 `"company" | "vendor"`, `model`은 업체 프로필의 `{"provider", "name"}` 또는 `None`(회사 측)이다. `None`을 돌려주면 그 조직은 `RulePolicy`(모델 미연결)로 돈다. `App(..., llm_factory=None)`이면 전부 `RulePolicy`다.

**업체 측 진입점 (가장자리와 맞춘 계약).** `handle_vendor_message(app, vendor_org_id, message, context) -> dict`는 업체 측 에이전트가 받은 메시지 하나에 답 메시지(dict) 하나를 돌려준다. `LocalVendorGateway`와 별도 서비스(`vendor_service.py`)가 같은 함수를 부른다. 그 업체 프로필의 `model`로 `llm_factory("vendor", vendor_org_id, model)`을 불러 모델을 고른다.

조직 시야:

- 배출 기업은 자기 진단만 본다. 다른 배출 기업의 진단, 흐름, 근거를 물으면 없는 것으로 답한다(404).
- 업체는 자기와 실제로 열린 스레드, 그 스레드의 흐름(현행 처리 단가 제외), 자기에게 배정된 흐름의 단가만 본다. `not_opened` 스레드, 다른 업체, 배출 기업의 현행 대비 손익과 담당자 질문은 보이지 않는다.
- 계산 근거의 입력 원본(`meta.inputs`)은 계산한 조직에게만 보이고, 다른 조직에게는 `{"hidden": true}`로 나간다.
- 메시지와 문서에 실린 claim의 근거는 받는 쪽에도 연다. 허가증 근거는 허가 품목을 대조하기 전에, 매입 기준 근거는 스레드를 열 때 배출 기업에 연다.

근거 검색(`GET /evidence?q=`): 기본은 로컬 어휘 검색이다. `AZURE_SEARCH_ENDPOINT`와 `AZURE_SEARCH_API_KEY`가 있으면 순위만 Azure AI Search(하이브리드 + 시맨틱)에서 받고, 내용은 레지스트리에서 다시 읽어 조직이 볼 수 없는 근거와 색인에만 남은 근거를 걸러 낸다. 색인이 실패하면 로컬 검색으로 답한다. 판단 도구(`search_clauses`)는 이 검색을 쓰지 않아 결과가 바뀌지 않는다. 색인은 `scripts/v3/index_evidence.py`로 올린다.

자료 올리기:

- `upload_csv`는 흐름 이름이 같은 흐름에 측정값을 붙이고 measure 근거로 등록한다. 이름이 맞는 흐름이 없는 행은 `unmatched`로 돌려준다.
- `upload_document`는 Document Intelligence가 설정돼 있으면 그것으로, 없으면 UTF-8 글 파일만 읽는다(`extraction: "text_only"`). Prompt Shields가 설정되지 않았으면 `shieldUnavailable: true`를 그대로 돌려준다. 주입이 탐지되면 문서를 쓰지 않고 올린 조직의 진행 중인 스레드를 모두 `handoff`로 넘긴다.
- 뽑은 값은 `confirm_document`로 담당자가 확인한 것만 doc 근거가 된다.
- `upload_media`는 사진·영상을 탐지 모델(Azure ML 관리형 온라인 엔드포인트, `intake/detector.py`의 `DetectorClient`)에 보내 재질 추정과 이물 비율을 model 근거로 등록하고 흐름의 `photo`에 붙인다. 엔드포인트가 설정되지 않았으면 `detectorUnavailable: true`를 돌려주고 아무것도 등록하지 않는다. 탐지 모델의 엔드포인트는 아직 배포되지 않았고, 가짜 transport로만 확인했다.

`run_eval`은 평가 사례 세트(`app/v3/eval/cases`, 50건, 모두 가상 수치)를 `eval.runner.run_cases`로 돌리고, 시연 시나리오도 따로 만든 저장소에서 처음부터 끝까지 돌린다. 사례마다 새 저장소를 만들므로 운영 데이터는 바뀌지 않는다. 지금 연결된 모델 설정과 공개 근거를 그대로 쓰므로 규칙 정책으로 돌았으면 결과에 그렇게 적힌다.

재학습 데이터 수집(`app/v3/feedback.py`):

- 교정과 실제 결과는 따로 표를 두지 않고 근거 레지스트리에 근거로 쌓는다(기록 전체가 `meta.feedback`에 실린다). 메모리 레지스트리와 SQL 레지스트리에서 똑같이 돌고, `POST /demo/reset`으로 레지스트리를 비우면 함께 지워진다.
- `feedback.export_grade_labels(app)`은 실제 등급이 있고 재질·오염도·수분이 모두 있는 기록만 학습 행으로 낸다(합성 데이터와 같은 열, `synthetic`은 늘 `False`). 오염도와 수분은 실제 결과를 기록할 때의 흐름 값(측정값 우선, 없으면 사진 추정과 담당자 답)을 붙여 둔 것이다.
- `feedback.export_detection_corrections(app)`은 박스·재질·출처·형태·혼입물 교정을 낸다. `photoEvidence`로 원본 사진을 찾고, 사진이 없는 흐름의 교정은 `hasPhoto: false`로 표시한다.
- `feedback.gate_status(app)`는 실제 등급 행으로 지금 기준값 규칙을 평가해 `deploy_gate`에 넣는다. 재학습한 후보 모델의 점수는 Azure ML 작업이 내고, 등록된 실행만 모델 버전 표에 나온다.
- `feedback.calibration_candidates(app, viewer)`는 실제 단가와 매입 기준표 단가의 차이를 흐름·업체별로 건별로 늘어놓는다. 평균을 내지 않고 기준값을 바꾸지 않는다.
- 관리자 조직(`org_admin`, `role: admin`)이 시연 데이터에 있다. 관리자 화면(`/admin/*`)과 내보내기, 실행 등록, 시연 되돌리기는 이 조직으로 부른다.

진행기에서 평가하며 고친 것:

- (설계 판단 3건) 측정값이 아닌 품질 값은 조건부(C12), 조건이 붙는 답은 `conditional`(C33), 합의한 대화가 넘어가면 그 업체를 빼고 다시 계산(C48). 위 3·4장에 적었다. 사례 세트에 open으로 남은 것은 없다.
- 형태를 모르는데 형태에 따라 매입 기준이 갈리면 판단 도구가 어느 기준도 고르지 않는다(`judge_route`는 조건부와 질문만 내고 단가를 내지 않는다).
- 모르는 값 때문에 업체를 정하지 못한 흐름은 결과 한 장에서 `needs_information`으로 나간다.
- 측정값과 사진 추정이 함께 있으면 `identify_stream`이 측정 수분으로 사진 추정 등급을 내어 견준다(다르면 로트 확인 질문).
- 대화 도중 담당자 답으로 흐름이 바뀌면 최적안을 내기 전에 `judge_route`를 다시 한다(결과 한 장의 확인할 것과 민감도가 여기서 나온다).
- 조직과 흐름의 이름에 든 숫자는 자리표시자 밖 숫자로 세지 않는다. 이름은 코드가 저장소에서 읽어 검증기에 넘기고(`validate_message(..., names=...)`), 숫자만으로 된 이름은 허용하지 않는다.

## 7. 실행 기록

모든 메시지, 도구 호출, 검증 결과, handoff, 승인은 `store`의 events에 남는다. 계약 7장의 `kind`에 `thread`(스레드 개설·미개설·합의·확정), `question`(담당자 질문과 답), `document`(문서 생성), `stage`(진단 생성)를 더했다. 업체가 보는 뷰에는 자기 스레드의 메시지·handoff·승인·스레드·문서 기록과 자기가 한 도구 호출·검증 기록만 나간다. 화면의 대화와 실행 기록은 이 기록을 그대로 보여 준다. 다만 기록에 실린 근거 ID 가운데 보는 조직이 열 수 없는 것은 뷰를 만들 때 빼고 개수(`detail.hiddenEvidenceCount`)만 남긴다(상대 쪽 계산 근거의 ID에는 도구 이름과 값의 종류가 드러난다). OpenTelemetry로 내보내는 것은 `tracing.py`가 한다: `APPLICATIONINSIGHTS_CONNECTION_STRING`이 있으면 `api.build_app`이 저장소를 `TracingStore`로 감싸고, `add_event`로 저장된 이벤트마다 span 하나(`rsai.<kind>`, 속성 `rsai.*`)를 낸다. 저장된 이벤트만 옮기므로 화면과 추적이 같은 기록을 본다. 내보내기가 실패해도 진단은 계속된다.

## 8. 로컬 서버

`python3 -m app.v3.devserver --port 8000 [--store 경로.json] [--reset] [--llm rule|foundry] [--demo-closed]`

- 표준 라이브러리 `http.server`로 계약 8장의 API를 모두 제공한다. 더한 주소는 `GET /health`와 `POST /intake/documents/confirm`이다.
- 시작할 때 가정값, 근거 시드, 시연 데이터를 올린다.
- `--store 경로.json`을 주면 저장소는 그 파일(`JsonFileStore`)에, 근거 레지스트리는 옆 파일 `<이름>.evidence.json`(`JsonFileRegistry`)에 쓴다. 두 파일이 이미 있으면 시드로 덮어쓰지 않고 이전 진단을 이어 간다(메시지와 문서가 인용한 근거가 그대로 남아 승인과 검증이 이어진다). `--reset`을 주면 처음 상태로 되돌린다. 저장소 파일만 있고 근거 파일이 없으면 이어 갈 수 없어 처음 상태로 시작한다. `POST /demo/reset`은 두 파일을 모두 처음 상태로 되돌린다.
- 이어 갈 때는 근거 시드(법령·기준값)를 다시 올리지 않는다. 시드 파일을 바꿨으면 `--reset`으로 다시 시작한다.
- `/admin/*`은 운영 조직(`org_admin`)으로 부른다. 시연 되돌리기는 누구나 부를 수 있고(`demo_open`), `--demo-closed`를 주면 관리자만 부른다.
- 기본은 `--llm rule`이다. `--llm foundry`일 때만 `FOUNDRY_ENDPOINT`, `FOUNDRY_DEPLOYMENT`, `FOUNDRY_API_KEY`를 읽는다. 업체가 ollama를 골랐으면 `OLLAMA_BASE_URL`이 있을 때만 그 모델을 쓰고, 없으면 그 업체는 규칙 정책으로 돈다.
- CORS는 `http://localhost:3000`과 `http://127.0.0.1:3000`만 허용한다(`X-Org-Id`, `Content-Type` 헤더, `OPTIONS` 처리).
- 업로드는 `multipart/form-data`(`file`, `orgId`, 문서는 `kind`)로 받는다.

## 9. 평가 (8단계)

| 모듈 | 하는 일 |
|---|---|
| `app/v3/eval/cases/*.json` | 사례 50건. C01~C10은 `docs/c-track/pp-validation-cases.md`를 옮긴 것(`review: 제품 정책 초안`), C11~C50은 새로 만든 것(`review: 초안`, 전문가 검토 전). 수치는 모두 가상이다. 형식은 `cases/README.md` |
| `app/v3/eval/loader.py` | 사례 불러오기와 형식 검사 |
| `app/v3/eval/world.py` | 사례의 가상 세계(저장소, 근거)를 만든다 |
| `app/v3/eval/runner.py` | `run_cases(app 또는 registry 또는 None, cases)`. 사례별로 판정 일치, 금지 위반, 검증기 통과를 보고 집계한다. 결정적이다 |
| `app/v3/eval/format_compliance.py` | 모델 형식 준수 시험. `python3 -m app.v3.eval.format_compliance --provider rule\|scripted\|foundry\|ollama` |
| `app/v3/eval/foundry_eval.py` | Foundry 평가(근거 충실도) 입력 JSONL(`query`, `response`, `context`)을 실행 기록에서 만든다 |
| `app/v3/eval/report.py` | `docs/v3/eval-report.md`를 만든다. 숫자는 실행 결과에서만 나오고, 테스트가 저장된 파일과 지금 결과를 견준다 |
| `scripts/v3/eval/run_foundry_eval.py` | Azure AI Evaluation SDK로 근거 충실도를 잰다(미실행) |

- 설계 판단이 필요한 사례는 `"status": "open"`과 `openNote`를 붙여 남긴다. open이 아닌 사례는 기대와 모두 맞아야 테스트가 통과하고, open 사례가 기대와 같아지면 표시를 지우라고 테스트가 알려 준다.
- 금지 위반은 사례의 `forbidden`과, 모든 사례에 걸리는 공통 규칙(가드레일, 허가 품목, 승인 전 확정, 턴 상한, 질문 한도, 조직 경계, 검증 없이 나간 메시지)을 함께 센다.
- 실제 모델의 형식 준수율과 Foundry 근거 충실도는 재지 않았다. 리포트에 "미측정"으로 적힌다.
