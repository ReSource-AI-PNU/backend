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

## 정오표(다음 판 test-v2에서 고친다)

동결된 판의 기대가 틀렸다고 정한 사례다. 동결된 파일은 고치지 않으므로 그 판의 결과(성공률, 금지 위반)에는 그대로 들어가고, 리포트의 L4 메모에 이 이유가 함께 적힌다(`app/v3/eval/sets.py`의 `ERRATA`). test-v2를 만들 때 생성기(`build_test_v2.py`)에서 고치고 `triage`에 옮긴다.

### test-v1 T042 · 출처를 모르는 흐름의 판정(2026-11-18 결정: 기대 오류)

- 사례: PE 분쇄품(st_a)의 출처(PIR·PCR)를 담당자도 모른다고 답했다. 업체 org_k1의 매입 기준(재질 PE, 등급 B, 오염도·수분 상한, 최소 물량)은 모두 충족한다. `scenarios.json`은 `status: "open"`이고, `forbidden`에 "st_a 판정 calculated 금지"가 있다(생성기 블록 `missing`이 C38의 "모르는 값이 남았는데 확정으로 두지 않는다"를 따랐다).
- 구현: st_a를 org_k1에 배정하고 `calculated`로 낸다. 모르는 출처는 `unresolved[{st_a, origin}]`과 `streams[].unknownFields`에 남는다.
- 결정: **기대가 틀렸고 구현이 맞다.**
  - 판정 상태의 뜻(설계안 v3 8.2, 용어집): `conditional`은 "확인할 것이 남아 있어 확정할 수 없는 판정"이다. 확인할 것은 그 경로의 판단에 들어가는 값이다.
  - 출처는 매각 경로의 판단에 들어가지 않는다. 업체 매입 기준 대조는 재질, 등급, 수분, 형태, 최소 물량만 본다(8.3). 최소 물량과 적재 검사는 출처와 관계없다(8.2, 개발 사례 C36). 출처는 공정 재투입 후보(`judge_reuse`의 `reuse_origin`)와 PCR 인정 표시("미확인", H3)에만 쓰인다. 이 사례에는 재투입 입력이 없다.
  - C38의 규칙은 질문 한도를 넘겨 판단에 쓰는 값(오염도, 수분 등)을 모르는 채 남긴 경우다. 판단에 쓰이지 않는 값까지 넓히면 출처만 모르는 흐름이 업체 기준을 모두 충족해도 확정되지 못한다. 같은 이유로 C16(출처를 답으로 받은 경우)은 `calculated`이고, 답이 "모름"이어도 경로 판단은 같아야 한다.
  - 모르는 값은 숨기지 않는다. 결과 한 장의 `unresolved`와 흐름 카드의 `unknownFields`에 남는다.
- test-v2에서 고칠 것: T042의 `forbidden`에서 `{"kind": "verdict", "stream": "st_a", "status": "calculated"}`를 빼고 `expect.assignments.st_a.status: "calculated"`를 적는다. `status`를 `open`에서 빼고 `triage`에 "기대 오류: 판단에 쓰이지 않는 빠진 값(출처)은 확정을 막지 않는다"로 옮긴다. 생성기 블록 `missing`은 빠진 값이 경로 판단에 쓰이는 항목(재질, 형태, 오염도, 수분, 물량)일 때만 `calculated`를 금지한다. st_b의 금지(오염도 측정값 없음)는 그대로 둔다.
- test-v1 결과에서는 T042가 L4 실패 1건(금지 위반 `verdict`)으로 남는다. 판이 다른 결과끼리 숫자를 비교하지 않는다.
