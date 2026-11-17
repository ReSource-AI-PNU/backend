"""근거 ID 규칙.

형식은 `종류:이름공간:키[@판]`이다. 설계안 v3 7.2를 읽지 못한 상태에서 정한 규칙이므로,
설계안과 다르면 이 파일만 고치면 되도록 ID를 만드는 곳은 모두 이 함수들을 거친다.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SAFE = re.compile(r"[^0-9A-Za-z가-힣._-]+")


def slug(text: str) -> str:
    return _SAFE.sub("-", str(text).strip()).strip("-") or "x"


def law_id(law: str, article: str, effective: str) -> str:
    """law:폐기물관리법:제17조@2026-01-01"""
    return f"law:{slug(law)}:{slug(article)}@{effective}"


def ref_id(series: str, item: str, period: str) -> str:
    """ref:가격조사:PP-펠렛-경남@2026-08"""
    return f"ref:{slug(series)}:{slug(item)}@{period}"


def doc_id(org_id: str, document: str, field: str) -> str:
    """doc:org_saegil:매입기준표:PP-A-단가"""
    return f"doc:{slug(org_id)}:{slug(document)}:{slug(field)}"


def measure_id(org_id: str, stream_id: str, field: str, measured_on: str) -> str:
    """measure:org_hanbit:st_sprue:오염도@2026-11-10"""
    return f"measure:{slug(org_id)}:{slug(stream_id)}:{slug(field)}@{measured_on}"


def model_id(model: str, version: str, key: str) -> str:
    """model:rsai-grade:v1:st_sprue"""
    return f"model:{slug(model)}:{slug(version)}:{slug(key)}"


def human_id(org_id: str, key: str) -> str:
    """human:org_hanbit:st_film-수분"""
    return f"human:{slug(org_id)}:{slug(key)}"


def assumption_id(key: str, version: str) -> str:
    """assumption:현행처리-혼합소각@v1"""
    return f"assumption:{slug(key)}@{version}"


def digest(payload: Any) -> str:
    """같은 입력이면 같은 값. 계산 근거 ID와 재현성 확인에 쓴다."""
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def calc_id(tool: str, inputs: Any, output_key: str) -> str:
    """calc:cost_carbon:3fa9c0d1e2b4:월손익 — 입력이 같으면 ID도 같다."""
    return f"calc:{slug(tool)}:{digest(inputs)}:{slug(output_key)}"
