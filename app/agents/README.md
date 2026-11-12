# 에이전트 모듈

대회 6단계를 전문 에이전트 6개로 구현한다. 파일 하나가 단계 하나를 맡는다. 진단 단위는 공장의 폐플라스틱 흐름(Stream) 여러 개이고, ①·②는 흐름마다 돌고 ②의 끝과 ⑤·⑥에서 공장 단위로 합친다 (설계안 v2 5.2절).

| 파일 | 단계 | 입력 | 출력 (`app/schemas.py`) |
|---|---|---|---|
| `classify.py` | ① 흐름 식별 | 흐름별 사진·영상, 공정명, 원자재 명세(선택), 되묻기 답변 | `Stream` (`classify`, `origin`, `monthlyKg`) |
| `feasibility.py` | ② 경로 판정 | `Stream[]`, 가격조사 단가, 소각 위탁비, 신재 단가 | `FeasibilityResult` (흐름별 `StreamDecision`, 공장 월 손익) |
| `regulation.py` | ③ 규정 검토 | 경로, 출처, 회사 정보 | `RegulationResult` (조항 인용, 확인 필요 항목은 `Question`) |
| `matching.py` | ④ 업체·묶음 매칭 | 경로가 정해진 흐름, 위치, 단지 기업, 허가 업체 | `MatchingResult` (순회 수거 `BundleGroup`, 매각처·열분해 `StreamPartner`) |
| `cost_carbon.py` | ⑤ 비용·탄소 비교 | ②·④ 결과, 도로 거리, 배출계수, KAU | `CostCarbonResult` (시나리오 A·B·C·D와 흐름별 내역) |
| `proposal.py` | ⑥ 이행안·승인 | ①~⑤ 종합 | `ProposalResult` (추천안, 서류 6종 초안) |

오케스트레이션은 `orchestrator.py`가 맡는다. 외부로 나가는 행동(서류 발송)은 담당자 승인(`Approval`)을 거친 뒤에만 한다. 계약 예시는 `GET /api/v1/contract/session-example`에서 볼 수 있다.
