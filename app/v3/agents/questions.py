"""배출 기업 담당자에게 하는 질문. 항목과 문구는 코드가 정하고, 모델은 항목을 새로 만들지 못한다.

우선순위는 docs/c-track/question-policy.md를 따른다: 재질과 근거 → 형태·혼입 상태 → 지역·발생량.
"""

from __future__ import annotations

import math
import re
from typing import Any

from app.v3 import ids
from app.v3.a2a.policy import UNKNOWN
from app.v3.agents.amounts import read_amount
from app.v3.evidence.model import Evidence
from app.v3.textutil import josa

FIELDS: dict[str, dict[str, Any]] = {
    # 현행 처리비를 모르면 혼합 소각 가정값으로 견주게 된다(설계안 15장 2).
    # 재질·품질 질문이 업체 대조에 먼저 필요하므로 그 뒤에, 질문 한도가 남을 때만 묻는다
    "currentCost": {
        "priority": 4,
        "answerType": "number",
        "unit": "원/kg",
        "label": "현행 처리비",
        "text": "{name_object} 지금 처리하는 데 kg당 얼마를 내고 계신가요? "
        "모르면 '모름'으로 답해 주세요.",
    },
    "material": {
        "priority": 1,
        "answerType": "text",
        "label": "재질",
        "text": "{name}의 재질은 무엇인가요? 모르면 '모름'으로 답해 주세요.",
    },
    "origin": {
        "priority": 1,
        "answerType": "choice",
        "choices": ["PIR", "PCR"],
        "label": "출처",
        "text": "{name_topic} 공정에서 나온 것(PIR)인가요, 쓰고 난 뒤 나온 것(PCR)인가요?",
    },
    "form": {
        "priority": 2,
        "answerType": "choice",
        "choices": ["가공 전", "압축", "분쇄", "펠렛", "필름"],
        "label": "형태",
        "text": "{name}의 지금 형태를 골라 주세요. 모르면 '모름'으로 답해 주세요.",
    },
    "contaminationPct": {
        "priority": 2,
        "answerType": "number",
        "unit": "%",
        "label": "오염도",
        "text": "{name}의 오염도(이물 혼입 비율)는 대략 몇 %인가요? 모르면 '모름'으로 답해 주세요.",
    },
    "moisturePct": {
        "priority": 2,
        "answerType": "number",
        "unit": "%",
        "label": "수분",
        "text": "{name}의 수분은 대략 몇 %인가요? 모르면 '모름'으로 답해 주세요.",
    },
    "lotSame": {
        "priority": 2,
        "answerType": "choice",
        "choices": ["예", "아니오"],
        "label": "로트 일치",
        "text": "{name}의 측정 시료와 사진 속 물량이 같은 로트인가요?",
    },
    "monthlyKg": {
        "priority": 3,
        "answerType": "number",
        "unit": "kg/월",
        "label": "월 발생량",
        "text": "{name}의 월 발생량은 대략 몇 kg인가요? 모르면 '모름'으로 답해 주세요.",
    },
}

# '모름'으로 읽는 답
_UNKNOWN_WORDS = re.compile(r"unknown|(?:잘\s*)?(?:모름|몰라요?|모르겠(?:어요|습니다|음)?)\.?")

# 메시지 meta.asks[].field에 올 수 있는 값(계약 7장). 업체가 배출 기업에 묻는 흐름 항목과,
# 회사 측이 업체에 조건을 달라고 할 때의 "terms"다. 이 밖의 값은 메시지에 싣지 않는다.
# 현행 처리비는 배출 기업만의 값이라 업체가 물을 수 없다.
PRIVATE_FIELDS = ("currentCost",)
ASK_FIELDS = (*[f for f in FIELDS if f not in PRIVATE_FIELDS], "terms")


def clean_asks(items: Any) -> list[dict[str, str]]:
    """모델이 낸 asks에서 {streamId, field}만, field가 ASK_FIELDS에 있는 것만 남긴다."""
    out: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("field") not in ASK_FIELDS:
            continue
        entry = {"streamId": str(item.get("streamId")), "field": str(item["field"])}
        if entry not in out:
            out.append(entry)
    return out


# 메시지 meta.asks의 항목을 본문에 적을 때의 이름
ASK_LABELS = {**{field: spec["label"] for field, spec in FIELDS.items()}, "terms": "받는 조건"}


def with_ask_lines(text: str, asks: list[dict[str, str]], names: dict[str, str]) -> str:
    """질문(question) 본문에 묻는 항목 목록이 없으면 '- 이름: 항목'으로 붙인다.

    모델이 "아래 값을 회신해 주시면"이라고만 쓰고 항목은 asks에만 적어, 상대가 무엇을 묻는지 본문에서
    알 수 없던 경우다. 본문에 '- ' 목록이 이미 있으면 그대로 둔다. 이름은 상대에게 보이는 이름이다.
    """
    if not asks or any(line.lstrip().startswith("- ") for line in text.splitlines()):
        return text
    lines = []
    for stream_id in dict.fromkeys(a["streamId"] for a in asks):
        fields = [
            ASK_LABELS.get(a["field"], "확인할 값") for a in asks if a["streamId"] == stream_id
        ]
        lines.append(f"- {names.get(stream_id) or '폐기물'}: {', '.join(dict.fromkeys(fields))}")
    return "\n".join([text.rstrip(), *lines])


_EVIDENCE_LABEL = {
    "moisturePct": "수분",
    "contaminationPct": "오염도",
    "monthlyKg": "월발생량",
    "currentCost": "현행처리비",
}
# identify_stream의 check 키 → 물을 항목
_CHECK_FIELDS = {
    "material": "material",
    "origin": "origin",
    "form": "form",
    "moisture": "moisturePct",
    "quality": "contaminationPct",
    "grade_disagreement": "lotSame",
}


def make_question(stream: dict[str, Any], field: str, *, asked_by: str = "identify") -> dict:
    """계약 7장 questions 항목(묶음 번호와 ID는 진행기가 붙인다)."""
    spec = FIELDS[field]
    name = str(stream.get("name") or stream["id"])
    return {
        "streamId": stream["id"],
        "field": field,
        "text": spec["text"].format(
            name=name, name_topic=josa(name, "은는"), name_object=josa(name, "을를")
        ),
        "answerType": spec["answerType"],
        "unit": spec.get("unit"),
        "choices": spec.get("choices"),
        "askedBy": asked_by,
    }


def known(stream: dict[str, Any], field: str) -> bool:
    """이미 값이 있거나 담당자가 모른다고 답한 항목은 다시 후보로 올리지 않는다."""
    if field in (stream.get("unknownFields") or []):
        return True
    answers = stream.get("answers") or {}
    if answers.get(field) is not None:
        return True
    if field == "moisturePct":
        return (stream.get("measure") or {}).get("moisturePct") is not None
    if field == "contaminationPct":
        return any(
            (stream.get(k) or {}).get("contaminationPct") is not None for k in ("measure", "photo")
        )
    if field == "lotSame":
        return False
    if field == "currentCost":
        return (stream.get("currentTreatment") or {}).get("unitCostKrwPerKg") is not None
    return stream.get(field) is not None


def from_identify(stream: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """identify_stream 결과의 확인할 것에서 질문 후보를 만든다."""
    out = []
    if stream.get("monthlyKg") is None:
        out.append("monthlyKg")
    for check in result.get("checks") or []:
        field = _CHECK_FIELDS.get(str(check.get("key")))
        if field:
            out.append(field)
    out.append("currentCost")
    return [make_question(stream, f) for f in dict.fromkeys(out) if not known(stream, f)]


def ordered(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """우선순위, 흐름 ID 순서. 같은 순위 안에서는 들어온 순서를 지킨다."""
    return sorted(candidates, key=lambda q: (FIELDS[q["field"]]["priority"], q["streamId"]))


def parse_answer(field: str, value: Any) -> Any:
    """답을 항목 형식에 맞춘다. "모름"은 UNKNOWN. 형식이 틀리면 ValueError(한국어 문장)."""
    spec = FIELDS.get(field)
    if spec is None:
        raise ValueError("물을 수 없는 항목입니다.")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("답이 비어 있습니다. 모르면 '모름'으로 답해 주세요.")
    if isinstance(value, str) and _UNKNOWN_WORDS.fullmatch(value.strip().lower()):
        return UNKNOWN
    if spec["answerType"] == "number":
        # "2 퍼센트", "650 킬로", "0.65톤", "약 2%"처럼 적은 답도 항목의 단위로 읽는다(amounts)
        number = read_amount(value, spec.get("unit"), spec["label"])
        if not math.isfinite(number) or number < 0:
            raise ValueError("0 이상의 숫자로 답해 주세요.")
        if spec.get("unit") == "%" and number > 100:
            raise ValueError("0에서 100 사이의 값으로 답해 주세요.")
        return number
    text = str(value).strip()
    if spec["answerType"] == "choice" and text not in spec["choices"]:
        raise ValueError(f"다음 가운데에서 골라 주세요: {', '.join(spec['choices'])}.")
    return text


def apply_answer(
    registry: Any, stream: dict[str, Any], field: str, value: Any, today: str
) -> dict[str, Any]:
    """답을 흐름에 적는다. 수치 답은 human 근거로 등록한다. 바뀐 흐름을 돌려준다."""
    stream = dict(stream)
    if value == UNKNOWN:
        stream["unknownFields"] = sorted({*(stream.get("unknownFields") or []), field})
        return stream
    spec = FIELDS[field]
    name = stream.get("name") or stream["id"]
    answers = dict(stream.get("answers") or {})
    if spec["answerType"] == "number":
        evidence = registry.put(
            Evidence(
                id=ids.human_id(stream["orgId"], f"{stream['id']}-{_EVIDENCE_LABEL[field]}"),
                kind="human",
                label=f"{name} {spec['label']}(담당자 답변)",
                value=float(value),
                unit=spec["unit"],
                excerpt=f"담당자가 질문에 답한 값: {name} {spec['label']} {value:g}{spec['unit']}",
                source="담당자 질문 답변",
                as_of=today,
                access=(stream["orgId"],),
                meta={"ownerOrgId": stream["orgId"], "streamId": stream["id"], "field": field},
            )
        )
        answers[field] = float(value)
        if field not in PRIVATE_FIELDS:
            # answers.evidence는 업체에 열리는 근거다. 현행 처리비 같은 배출 기업만의 답은 섞지 않는다
            answers["evidence"] = list(
                dict.fromkeys([*(answers.get("evidence") or []), evidence.id])
            )
        if field == "monthlyKg":
            stream["monthlyKg"] = float(value)
            stream["monthlyKgBasis"] = "estimated"
            stream["monthlyKgEvidence"] = [evidence.id]
        if field == "currentCost":
            # 처리 방법은 묻지 않았다. 자료 화면에서 넣기 전까지는 혼합 소각으로 본다
            current = dict(stream.get("currentTreatment") or {})
            stream["currentTreatment"] = {
                "method": current.get("method") or "혼합 소각",
                "methodAssumed": not current.get("method"),
                "unitCostKrwPerKg": float(value),
                "evidence": [evidence.id],
            }
    elif field == "lotSame":
        answers[field] = value
    else:
        stream[field] = value
        stream[f"{field}Basis"] = "human"
    stream["answers"] = answers
    stream["unknownFields"] = [f for f in stream.get("unknownFields") or [] if f != field]
    return stream
