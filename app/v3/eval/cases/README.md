# 평가 사례 세트

사례의 회사, 업체, 좌표, 수치는 모두 가상이다. 실제 업체·견적·측정이 아니다.

- `c01_c10_pp_validation.json`: `docs/c-track/pp-validation-cases.md`의 C01~C10을 그대로 옮겼다. 원문 상태를 따라 `"review": "제품 정책 초안"`이다.
- 나머지 파일(C11~C50): 새로 만든 40건이다. 전문가 검토 전이라 모두 `"review": "초안"`이다.

파일은 `{"title", "synthetic": true, "note", "cases": [...]}` 모양이고, 사례 하나는 다음과 같다.

| 키 | 내용 |
|---|---|
| `id`, `title`, `tags` | 사례 번호(`C01`~), 제목, 주제 |
| `review` | `초안`(전문가 검토 전), `제품 정책 초안`(원문 상태), `검토 완료` |
| `synthetic` | 늘 `true`. 수치가 가상이라는 표시 |
| `world` | 입력. `streams`(흐름), `vendors`(업체: 허가 품목, 매입 기준, 차량, 가드레일), 선택으로 `otherEmitters`(다른 배출 기업), `evidencePatches`(근거 만료 등), `shieldKeywords`(Prompt Shields 자리의 결정적 가짜가 탐지할 글귀) |
| `answers` | 담당자 질문에 줄 답. `"st_a.moisturePct": 2.0`, 적지 않은 질문은 `"unknown"`(모름) |
| `approve`, `actions` | 양쪽 승인까지 갈지, 중간에 할 일(`upload_document`, `patch_evidence`, `approve_one`, `update_stream`)과 시점(`before_run`, `awaiting_approval`, `after_confirm`) |
| `session` | `false`면 도구 수준에서만 본다 |
| `expect` | 기대. `tools`(판단 도구 호출별 판정 상태 `status`, `checks` 키와 상태, `claims`, `output`), `result.status`, `assignments`(흐름별 업체·경로·판정 상태·checks 키), `threads`(업체별 스레드 상태), `questions`, `unresolved`, `stages`, `streams`, `uploads`, `approvals`, `documents` |
| `forbidden` | 금지 사항. `kind`와 이유 `why`. 종류는 `app/v3/eval/loader.py`의 `FORBIDDEN_KINDS` |
| `status`, `openNote` | 기대와 구현이 다르고 어느 쪽이 옳은지 설계 판단이 필요한 사례는 `"status": "open"`으로 남기고 이유를 적는다 |
| `note` | 사례에 대한 메모(리포트에 그대로 나온다) |

흐름과 업체는 줄여 적고, `app/v3/eval/world.py`가 시연 데이터와 같은 방식으로 근거(measure, model, doc, human)를 붙여 등록한다. 배출 기업은 `org_eval`((주)가상정밀)이다.

사례를 고치거나 더한 뒤에는 다음을 돌린다.

```bash
python3 -m pytest -q tests/v3/test_eval_cases.py tests/v3/test_eval_runner.py
python3 -m app.v3.eval.report --out docs/v3/eval-report.md
```

기대값은 지시문과 계약의 규칙에서 정한다. 구현에 맞추려고 기대를 느슨하게 바꾸지 않는다. 구현이 틀렸으면 구현을 고치고, 판단이 필요하면 `open`으로 남긴다.
