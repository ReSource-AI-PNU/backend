# 평가셋

평가 설계(부산대표 `docs/평가_설계.md`) 4.1, 12장, 14장을 따른다. 회사, 업체, 좌표, 수치, 개인정보, 링크는 모두 가상이다.

| 위치 | 내용 | 건수 |
|---|---|---|
| `../cases/` | 개발 사례 C01~C50(L4 개발셋). 프롬프트와 규칙을 고칠 때 본다 | 50 |
| `dev/l2_queries.json` | L2 검색 개발 질의 | 40 |
| `dev/l6_attacks.json` | L6 방어를 고칠 때만 보는 개발 공격 | 20 |
| `test-v1/scenarios.json` | L4 시험 시나리오(축 수준과 기대 끝 상태) | 70 |
| `test-v1/variants.json` | 변형(INV·DIR, 기본 사례 40건 × 2) | 80 |
| `test-v1/l2_queries.json` | L2 검색 시험 질의(정답 근거 ID와 관련 등급) | 80 |
| `test-v1/l5_negotiation.json` | L5 협상 대상과 전술 변형 | 29 + 29 |
| `test-v1/l6_attacks.json` | 간접 주입 공격과 정상 쌍둥이 | 60 + 60 |
| `test-v1/l6_probes.json` | 조직 간 노출 카나리아와 탐침 | 30 |
| `test-v1/l6_pii.json` | 가상 개인정보 문서·입력 | 20 |
| `corpus/laws_extra.json` | 평가 전용 법령 말뭉치(law.go.kr 본문, 조마다 수집 주소와 날짜) | 348조 |
| `baselines/l2.json` | 개발셋에서 고른 검색 방식, 답 없음 문턱값, S0·S1 기준선 | - |

## 기대값의 상태

- 시험 시나리오의 기대값은 `scripts/v3/eval/build_test_v1.py`의 블록 함수에 손으로 쓴 규칙(계약 문서, 질문 정책, 개발 사례의 설계 판단)에서 나온다. 구현을 돌려 나온 값을 옮기지 않았다. 규칙 정책과 견준 뒤 정한 것(기대 오류, 생성기 오류, open)은 `scenarios.json`의 `triage`에 이유와 함께 남겼다.
- 모두 전문가 검토 전 초안(`"review": "초안"`)이다. 두 번째 작성자 대조(12.2의 30%, 21건)와 L2 관련 등급의 이중 라벨, κ는 아직 하지 않았다. 결과에는 "초안 기대값 기준"을 붙인다.
- 시험셋은 기대값을 쓴 사람과 구현을 만든 쪽이 같다(한 사람 개발). 그래서 시험 실패를 보고 고친 것은 그 판의 결과로 주장하지 않고 다음 판에서 확인한다.

## 동결과 새 판

- `test-v1/MANIFEST.json`에 파일마다 SHA-256, 건수, 만든 날, 라벨링 지침 판이 있다. 시험셋이 함께 쓰는 `corpus/laws_extra.json`도 들어 있다. `tests/v3/test_eval_sets.py`가 해시를 대조해 다르면 실패한다.
- 리포트 머리에는 매니페스트 해시(`app.v3.eval.sets.manifest_hash`)를 적는다.
- **동결된 판은 고치지 않는다.** 시나리오나 정답을 고쳐야 하면 새 판을 만든다.
  1. `scripts/v3/eval/build_test_v1.py`를 복사해 `build_test_v2.py`로 바꾸고 `VERSION`, `OUT`을 `test-v2`로 고친다(또는 새 데이터를 `test-v2/`에 둔다).
  2. 바꾼 이유를 새 판의 `scenarios.json` `triage`에 적는다.
  3. `python3 -m app.v3.eval.sets --freeze test-v2`로 매니페스트를 쓴다.
  4. `app/v3/eval/sets.py`의 `CURRENT_TEST`를 `test-v2`로 바꾼다. `test-v1`은 지우지 않는다.
  5. 판이 다른 결과끼리는 숫자를 비교하지 않는다(리포트와 신뢰 페이지에 판을 적는다).
- 동결을 풀어야 하는 예외(파일이 깨짐 등)는 `build_test_v1.py --force`로 같은 판을 다시 내고 매니페스트를 지운 뒤 다시 동결한다. 이 경우 커밋 메시지에 이유를 적고, 그 전의 결과와 숫자를 비교하지 않는다.

```bash
python3 -m app.v3.eval.sets --verify test-v1     # 동결 그대로인지
python3 -m scripts.v3.eval.build_test_v1         # 시나리오·변형 다시 만들기(동결 뒤에는 거부한다)
```
