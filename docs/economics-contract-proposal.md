# 가상 경제 효과 공통 계약 변경 제안

> **구현 및 검토 중 — 팀 전체 합의 및 병합 전이다.** 아래 표는 PR #29의 기존 계약과 이 작업에서 구현한 변경을 비교한다. 공통 계약을 수정했고 어댑터를 추가했으며, 세션 API는 구현하지 않았다.
> 기준은 backend PR #29 `feat/1-schemas` 커밋 `03f725de705bf84edfc1ce8ef57877fe35a5526d`의 `app/schemas.py`, `app/examples/demo_session.json`, 계약 확인 API와 PR #38 `2e6ee28`의 계산 코어다. 예시는 모두 synthetic/demo이며 실제 거래·품질·법적 적합성을 뜻하지 않는다.

## 현재 계약과 제안

PR #29는 `extra="forbid"`, camelCase JSON 별칭과 정수 `Won`을 사용한다. 아래 변경은 경제 결과 모델에 적용했다. 기존의 다른 `Won` 필드는 유지한다.

| 기존 필드 (PR #29) | 제안 필드 | 변경 이유 | 프런트 영향 |
|---|---|---|---|
| `Stream.monthlyKg` (0 이상), `monthlyKgConfirmed` | 필드는 유지. 0kg 또는 확인 전이면 해당 흐름 계산 보류 | 0kg 고정 인건비 차감이나 미확인 값을 확정치처럼 사용하지 않음 | 보류와 확인 요청을 표시. 다른 흐름 결과는 유지 |
| `StreamDecision` 상태 필드 없음 | `status`: `calculated` / `conditional` / `needs_information` | 계산됨, 조건부 묶음, 보류를 명확히 구분 | 상태별 라벨과 안내 추가 |
| 사유 코드 없음, `reason` 필수 문자열 | `reasonCode`: `missing_information`, `unconfirmed_quantity`, `zero_quantity`, `unsupported_combination` 등 또는 `null`; `reason`은 설명 | 정보 부족과 미지원 조합을 구분 | 자유 문장 파싱 대신 사유 코드 사용. 미지원은 실제 부적합 뜻이 아님 |
| `route` 필수, `aiRoute` 선택 | `route` nullable, `aiRoute` nullable | 경로를 정할 정보가 없을 수 있음. 사용자 변경 시 원래 경로 보존 | null 안전 처리. 변경 시 원래 경로 표시 |
| `currentPerKg`, `afterPerKg`, `monthlyNow`, `monthlyAfter`: 필수 정수 `Won` | 같은 필드의 소수 보존 숫자 또는 `null`, `monthlyDifference` 추가 제안 | 미계산과 0을 구별하고 계산 정밀도 보존 | nullable 숫자 처리, 화면에서만 반올림 |
| `FeasibilityResult.totalNow`, `totalAfter`: 필수 정수 | 소수 보존 숫자 또는 `null`, `difference`와 전체 `status` 추가 제안 | 보류 흐름이 있으면 부분 합계를 전체값으로 오인하지 않음 | 합계 보류·조건부 표시 및 null 처리 |
| `StreamDecision`·`FeasibilityResult`의 `carbonNowT`, `carbonAfterT`: 필수 실수 | 미계산 시 `null` 허용 | 탄소 미계산을 배출량 0으로 오인하지 않음 | null을 미계산으로 표시 |
| `action`: `FieldAction` 또는 생략. `title`, `laborHours`, 정수 `laborCost` | 구조 유지, `laborCost` 소수 보존. 코어가 이름·시간·비용을 직접 제공 | 어댑터가 작업 시간이나 이름을 추정하지 않음 | 소수 금액 표시. `action: null` 의미는 아래 참조 |
| `ladder`, `alternatives`: 필수 배열 | 배열 또는 `null`. `null`은 미계산, `[]`는 계산했으나 항목 없음 | 숫자나 대안을 임의로 채우지 않음 | null과 빈 배열을 구분 |
| (위와 같음, #10 후속) | 계산된 흐름(`calculated`·`conditional`)은 코어가 `ladder`와 `alternatives`를 채운다. 보류 흐름은 `null` 유지 | 프런트 mock의 단계별 손익·대안 표시를 백엔드 값으로 대체 | `alternatives[].perKg`는 그 경로가 적용된다고 가정한 값이며 가능 여부가 아님. `note`와 함께 표시 |
| 경제 효과 세부 항목 없음 | `cashSaleRevenue`, `purchaseAvoidanceNet`, `disposalCost`, `transportCost`, `laborCost`: 소수 또는 `null` | 현금 수입과 분쇄·관리비 포함 구매 회피 순효과, 별도 비용을 구분 | 각 항목의 단위·부호·포함 관계 표시 |
| `FeasibilityResult.reasons`, `note`; 별도의 시연 표시 없음 | 결과에 `dataMode: "synthetic_demo"`, 구조화된 `assumptions` 추가 | 시연값과 가정의 출처·범위를 명확히 함 | 시연 표시와 가정 영역 추가 |

`action: null`은 결과가 계산된 경우 이 시연 모델에서 별도 작업이 없다는 뜻으로 제안한다. `needs_information`의 `action: null`은 작업 **미확정**이다. 상태와 함께 해석해야 하며 `laborCost: null`은 미계산, 명시적 `0`은 시연상 0 가정이다. 공통 모델의 금액 nullable과 결과 상태 표현, 필드 이름, 하위 호환·버전 정책은 스키마 담당자와 합의해야 한다.

## 계산과 표시 제안

- 코어와 API는 계산한 소수를 보존하고 중간·응답 단계에서는 반올림하지 않는다. 공장 합계는 반올림 전 흐름별 월 결과의 합이다. 미계산 흐름이 하나라도 있으면 전체 합계는 `null`이며 부분 합계를 전체 합계로 표시하지 않는다.
- 화면에서만 표시를 반올림한다. 표시된 흐름별 금액의 합과 표시된 공장 합계가 1원 이상 차이날 수 있으므로, 화면 합계의 표시 규칙·음수 및 정확히 절반인 값의 처리·1원 차이 안내는 추후 합의한다.
- 현재 코어와 경제 결과는 `float` 및 JSON number를 사용하므로 이진 부동소수점 오차가 있다. 소수를 보존한다는 것은 이 오차가 없다는 뜻이 아니다. Decimal 전환 여부는 후속 검토한다.
- 기존 `afterPerKg`는 묶음 운송비를 이미 차감하고 별도 월 인건비는 차감하지 않은 효과다. `monthlyAfter = monthlyKg × afterPerKg − laborCost`. 재투입 `purchaseAvoidanceNet`은 이미 분쇄·관리비가 포함된 구매 회피 순효과다. 운송비를 다시 차감하거나 이를 현금 매출로 더하지 않는다.
- 어댑터는 0kg과 `monthlyKgConfirmed=false`를 흐름별 보류로 반환한다. 코어는 여전히 0kg에 `ValueError`를 내고 확인 여부를 받지 않는다. 다른 공통 입력 검증 실패는 스키마에서 거부한다.
- 세션 단위 보류는 나머지 흐름 결과를 보존하되 공장 합계를 `null`로 한다. 조건부 흐름이 있고 보류가 없으면 공장 상태는 `conditional`, 보류 흐름이 있으면 `needs_information`으로 제안한다.

## JSON 예시

아래는 구현된 필드를 보여 주는 발췌이며, 각 사례는 서로 독립적이다. 공통 모델의 다른 필수 필드는 가독성을 위해 생략했다. 전체 실행 예시는 `app/examples/demo_session.json`에 있다. 금액은 원, `*PerKg`는 원/kg, 시간은 월 기준 시간이다.

### 정상: 가상 PP 재투입

```json
{
  "streamId": "st_sprue",
  "status": "calculated",
  "reasonCode": null,
  "route": "reuse",
  "aiRoute": null,
  "reason": "시연 규칙: 이물이 적은 단일 PIR 플라스틱 재투입",
  "currentPerKg": -250.0,
  "afterPerKg": 900.0,
  "monthlyNow": -150000.0,
  "monthlyAfter": 510000.0,
  "monthlyDifference": 660000.0,
  "cashSaleRevenue": 0.0,
  "purchaseAvoidanceNet": 540000.0,
  "disposalCost": 0.0,
  "transportCost": 0.0,
  "laborCost": 30000.0,
  "action": {"title": "색별로 분리해 분쇄기에 넣기", "laborHours": 2.0, "laborCost": 30000.0},
  "carbonNowT": null,
  "carbonAfterT": null,
  "ladder": [
    {"label": "현재(혼합 소각)", "perKg": -250.0},
    {"label": "분리해 매각", "perKg": 300.0},
    {"label": "분쇄 재투입", "perKg": 900.0}
  ],
  "alternatives": [
    {"route": "sell", "perKg": 300.0, "note": "이물을 떼야 매입 기준을 맞춥니다."},
    {"route": "pyrolysis", "perKg": -80.0, "note": "재질이 섞인 흐름에 맞는 경로입니다."},
    {"route": "incinerate", "perKg": -250.0, "note": "지금처럼 혼합 폐기물로 위탁합니다."}
  ],
  "dataMode": "synthetic_demo",
  "assumptions": ["재투입 순효과는 분쇄·관리비 포함", "별도 작업 인건비 차감"]
}
```

### 조건부: 가상 LDPE 묶음 후보

```json
{
  "streamId": "st_film",
  "status": "conditional",
  "reasonCode": null,
  "route": "bundle",
  "aiRoute": null,
  "reason": "시연 규칙: PCR 최소 물량 미만 묶음 후보",
  "currentPerKg": -250.0,
  "afterPerKg": 100.0,
  "monthlyNow": -75000.0,
  "monthlyAfter": 15000.0,
  "monthlyDifference": 90000.0,
  "cashSaleRevenue": 45000.0,
  "purchaseAvoidanceNet": 0.0,
  "disposalCost": 0.0,
  "transportCost": 15000.0,
  "laborCost": 15000.0,
  "action": {"title": "흐름을 마대에 따로 모으기", "laborHours": 1.0, "laborCost": 15000.0},
  "carbonNowT": null,
  "carbonAfterT": null,
  "ladder": [
    {"label": "현재(혼합 소각)", "perKg": -250.0},
    {"label": "분리해 단독 매각", "perKg": -40.0},
    {"label": "단지 묶음 매각", "perKg": 100.0}
  ],
  "alternatives": [
    {"route": "sell", "perKg": -40.0, "note": "혼자 팔면 출장비 때문에 단가가 깎입니다."},
    {"route": "pyrolysis", "perKg": -80.0, "note": "재질이 섞인 흐름에 맞는 경로입니다."},
    {"route": "incinerate", "perKg": -250.0, "note": "지금처럼 혼합 폐기물로 위탁합니다."}
  ],
  "dataMode": "synthetic_demo",
  "assumptions": ["묶음 운송비 50원/kg은 afterPerKg에 포함", "가상 조건부 후보이며 거래 미확정"]
}
```

### 계산 보류: 미확인 발생량을 포함한 공장

```json
{
  "status": "needs_information",
  "decisions": [
    {
      "streamId": "st_pending",
      "status": "needs_information",
      "reasonCode": "unconfirmed_quantity",
      "route": null,
      "aiRoute": null,
      "reason": "월 발생량 확인 필요",
      "currentPerKg": null,
      "afterPerKg": null,
      "monthlyNow": null,
      "monthlyAfter": null,
      "monthlyDifference": null,
      "cashSaleRevenue": null,
      "purchaseAvoidanceNet": null,
      "disposalCost": null,
      "transportCost": null,
      "laborCost": null,
      "action": null,
      "carbonNowT": null,
      "carbonAfterT": null,
      "ladder": null,
      "alternatives": null,
      "dataMode": "synthetic_demo",
      "assumptions": []
    }
  ],
  "totalNow": null,
  "totalAfter": null,
  "difference": null,
  "carbonNowT": null,
  "carbonAfterT": null,
  "dataMode": "synthetic_demo",
  "assumptions": ["미확인 흐름이 있어 공장 전체 합계 계산 보류"]
}
```

계산된 흐름의 `ladder`·`alternatives`는 backend #10 후속 작업에서 채웠다. 프런트 mock과 달리 아래 두 규칙을 더했다.

- 최소 물량 미만 PCR 흐름의 단독 매각 출장비(190원/kg)는 대안 표시와 본 계산에 똑같이 적용한다. 경로를 실제로 `sell`로 바꾸면 `afterPerKg`는 −40원/kg, 출장비는 `transportCost`로 나온다(가상 포장 필름 300kg: 월 −42,000원). 프런트 mock은 본 계산에서 출장비를 빼지 않아 150원/kg이므로, 화면 연결 때 백엔드 값을 써야 한다.
- 열분해는 플라스틱 흐름(폐플라스틱, LDPE·LLDPE 포장재)에만 대안으로 낸다. 금속 스크랩·폐목재에는 나오지 않는다.

미지원 조합은 같은 `needs_information` 상태에 `reasonCode: "unsupported_combination"`을 사용한다. 0kg은 `zero_quantity`를 사용하며 코어의 정보 부족·미지원 사유도 구조화된 코드로 전달한다.

## API와 후속 순서

backend #2가 계획한 `POST /sessions/{id}/steps/{step}`의 feasibility 단계에서 `app.agents.economics_adapter.evaluate_streams`를 호출하도록 제안한다. 별도 계산 엔드포인트나 세션 연결은 아직 없다. #29·#38 및 이 변경의 리뷰·병합 후 **세션 단계 연결 → 화면 확인**을 진행한다. PR #29의 `/api/v1/contract/session-example` 및 `/collections-example`은 읽기 전용 계약 예시이며 계산 API가 아니다. 세션 예시 API는 명시적 null을 보존한다.
