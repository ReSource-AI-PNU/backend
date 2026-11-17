# 근거 시드

이 폴더의 `*.json`은 서버가 뜰 때 `app.v3.evidence.seeds.load_seed_dir`가 이름 순서로 읽어 근거 레지스트리에 넣는다. 이 폴더에 `__init__.py`를 두지 않는다(`seeds.py` 모듈과 이름이 같다).

## 형식

파일 하나는 근거 객체의 배열이고, 객체는 `Evidence.to_dict()` 모양이다.

```json
[
  {
    "id": "assumption:현행처리-혼합소각@v1",
    "kind": "assumption",
    "label": "현행 처리 단가 (혼합 소각 가정)",
    "value": 250,
    "unit": "원/kg",
    "excerpt": "현행 처리를 입력하지 않은 흐름은 혼합 소각으로 가정한다.",
    "source": "팀 가정값 v1",
    "asOf": "2026-11-16",
    "validUntil": null,
    "access": ["*"],
    "version": 1,
    "meta": {}
  }
]
```

| 키 | 필수 | 내용 |
|---|---|---|
| `id` | 예 | `app/v3/ids.py`의 규칙을 따른다. `kind` + `:`로 시작한다 |
| `kind` | 예 | `law`, `ref`, `doc`, `measure`, `model`, `human`, `calc`, `assumption` |
| `asOf` | 예 | 기준일 `YYYY-MM-DD` |
| `excerpt` | 조항 근거는 예 | 원문을 글자 그대로. 인용 대조는 이 문자열의 부분 문자열로 한다 |
| `value`, `unit` | 수치 근거는 예 | 단위는 `docs/v3/CONTRACT.md` 3장의 목록만 쓴다 |
| `validUntil` | 아니오 | 이 날짜가 지나면 검증기가 만료로 거부한다 |
| `access` | 아니오 | 볼 수 있는 조직 ID 목록. 비우면 `["*"]`(공개) |
| `label`, `source`, `version`, `meta` | 아니오 | `kindLabel`은 읽을 때 무시한다 |

## 규칙

- 원문을 확인하지 못한 법령과 기준값은 넣지 않는다. 팀이 정한 값은 `assumption`으로 넣는다.
- 형식이 틀리면 파일 이름과 몇 번째 항목인지 밝힌 `ValueError`가 나고, 그 파일은 하나도 들어가지 않는다.
- 한 파일 안에서 ID가 겹치면 오류다. 파일 사이에서 겹치면 나중 파일이 덮어쓰고 `version`이 오른다.
