"""문서 경로. Document Intelligence(prebuilt-layout)로 읽고, 표와 키-값을 정규화한다.

추출값은 사람이 확인하기 전에는 근거가 아니다. confirm_fields가 확인된 값만 doc 근거로 등록한다.
확신이 낮거나 형식이 애매한 값에는 needsReview=True가 붙는다.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from typing import Any

from app.v3 import ids
from app.v3.evidence.model import Evidence
from app.v3.intake._http import Transport, default_transport, header

API_VERSION = "2024-11-30"
LOW_CONFIDENCE = 0.8
MATERIALS = ("LLDPE", "LDPE", "HDPE", "PVC", "PET", "ABS", "PP", "PE", "PS", "PC", "PA")
WASTE_ITEMS = {
    "폐플라스틱": ("폐합성수지", "폐플라스틱", "합성수지"),
    "포장재": ("포장재", "필름", "비닐"),
    "금속 스크랩": ("금속", "고철", "스크랩"),
    "폐목재": ("폐목재", "목재"),
}
METHODS = ("위탁 재활용", "혼합 소각", "열분해", "재활용", "소각", "매립")
GRADES = ("A", "B", "C", "등외")
_NUMBER = re.compile(r"[-−–]?\s*\d[\d,]*(?:\.\d+)?")
_MATERIAL = re.compile(r"(?<![A-Za-z])(" + "|".join(MATERIALS) + r")(?![A-Za-z])", re.IGNORECASE)


class DocumentError(RuntimeError):
    """Document Intelligence 호출이 실패했다."""


class DocumentIntelligenceClient:
    def __init__(self, endpoint: str = "", api_key: str = "", transport: Transport | None = None):
        self.endpoint = endpoint.strip().rstrip("/")
        self.api_key = api_key
        self.transport = transport or default_transport

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.api_key)

    def begin_analyze(self, data: bytes, *, model: str = "prebuilt-layout") -> str:
        """분석을 맡기고 결과를 물어볼 주소(Operation-Location)를 돌려준다."""
        if not self.configured:
            raise DocumentError("Document Intelligence가 설정되지 않았습니다.")
        url = (
            f"{self.endpoint}/documentintelligence/documentModels/{model}:analyze"
            f"?api-version={API_VERSION}&features=keyValuePairs"
        )
        body = json.dumps({"base64Source": base64.b64encode(data).decode("ascii")}).encode("ascii")
        headers = {"Content-Type": "application/json", "Ocp-Apim-Subscription-Key": self.api_key}
        try:
            status, response_headers, _ = self.transport("POST", url, headers, body)
        except OSError as error:
            raise DocumentError(f"Document Intelligence에 연결하지 못했습니다: {error}") from error
        location = header(response_headers, "Operation-Location")
        if status != 202 or not location:
            raise DocumentError(f"문서 분석 요청이 거부됐습니다({status}).")
        return location

    def get_result(self, operation_url: str) -> dict[str, Any]:
        headers = {"Ocp-Apim-Subscription-Key": self.api_key}
        try:
            status, _, raw = self.transport("GET", operation_url, headers, None)
        except OSError as error:
            raise DocumentError(f"Document Intelligence에 연결하지 못했습니다: {error}") from error
        if status != 200:
            raise DocumentError(f"문서 분석 결과를 가져오지 못했습니다({status}).")
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as error:
            raise DocumentError("문서 분석 응답 형식이 잘못됐습니다.") from error

    def analyze(
        self,
        data: bytes,
        *,
        model: str = "prebuilt-layout",
        poll_seconds: float = 1.0,
        max_polls: int = 60,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any]:
        """끝날 때까지 기다려 analyzeResult(layout)를 돌려준다."""
        operation_url = self.begin_analyze(data, model=model)
        for _ in range(max_polls):
            payload = self.get_result(operation_url)
            state = payload.get("status")
            if state == "succeeded":
                return payload.get("analyzeResult") or {}
            if state == "failed":
                message = (payload.get("error") or {}).get("message", "")
                raise DocumentError(f"문서 분석이 실패했습니다. {message}".strip())
            sleep(poll_seconds)
        raise DocumentError("문서 분석이 제시간에 끝나지 않았습니다.")


# ── layout 읽기 ───────────────────────────────────────────────


def layout_text(layout: dict[str, Any]) -> str:
    """Prompt Shields 검사와 정규식 추출에 쓰는 문서 글 전체."""
    if layout.get("content"):
        return str(layout["content"])
    return "\n".join(str(p.get("content", "")) for p in layout.get("paragraphs") or [])


def tables(layout: dict[str, Any]) -> list[dict[str, Any]]:
    """표를 {"headers": [...], "rows": [[{"text", "confidence"}]]}로 편다. 첫 행을 머리글로 본다."""
    result = []
    for table in layout.get("tables") or []:
        rows = int(table.get("rowCount") or 0)
        cols = int(table.get("columnCount") or 0)
        grid = [[{"text": "", "confidence": None} for _ in range(cols)] for _ in range(rows)]
        for cell in table.get("cells") or []:
            r, c = int(cell.get("rowIndex", 0)), int(cell.get("columnIndex", 0))
            if r < rows and c < cols:
                grid[r][c] = {
                    "text": str(cell.get("content", "")).strip(),
                    "confidence": cell.get("confidence"),
                }
        if grid:
            result.append({"headers": [c["text"] for c in grid[0]], "rows": grid[1:]})
    return result


def key_values(layout: dict[str, Any]) -> list[dict[str, Any]]:
    pairs = []
    for pair in layout.get("keyValuePairs") or []:
        key = str((pair.get("key") or {}).get("content", "")).strip()
        value = str((pair.get("value") or {}).get("content", "")).strip()
        if key:
            pairs.append({"key": key, "value": value, "confidence": pair.get("confidence")})
    for line in layout_text(layout).splitlines():  # "항목: 값" 꼴의 줄도 키-값으로 본다
        if ":" in line or "：" in line:
            key, _, value = line.replace("：", ":").partition(":")
            if key.strip() and value.strip() and len(key.strip()) <= 20:
                pairs.append({"key": key.strip(), "value": value.strip(), "confidence": None})
    return pairs


def _number(text: str) -> float | None:
    match = _NUMBER.search(text or "")
    if not match:
        return None
    cleaned = re.sub(r"[\s,]", "", match.group()).replace("−", "-").replace("–", "-")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _low(confidence: Any) -> bool:
    return isinstance(confidence, (int, float)) and confidence < LOW_CONFIDENCE


def _materials(text: str) -> list[str]:
    found = []
    for match in _MATERIAL.finditer(text or ""):
        name = match.group(1).upper()
        if name not in found:
            found.append(name)
    return found


def _waste_item(text: str) -> str | None:
    for item, words in WASTE_ITEMS.items():
        if any(word in (text or "") for word in words):
            return item
    return None


def _find(headers: list[str], *words: str) -> int | None:
    for index, name in enumerate(headers):
        compact = name.replace(" ", "")
        if any(word in compact for word in words):
            return index
    return None


def _field(
    key: str, label: str, value: Any, unit: str | None, raw: str, reasons: list[str]
) -> dict[str, Any]:
    return {
        "field": key,
        "label": label,
        "value": value,
        "unit": unit,
        "raw": raw,
        "needsReview": bool(reasons) or value is None,
        "reviewReasons": reasons if value is not None else [*reasons, "값을 읽지 못했습니다"],
        "confirmed": False,
    }


def _first_value(pairs: list[dict[str, Any]], pattern: str) -> dict[str, Any] | None:
    regex = re.compile(pattern)
    for pair in pairs:
        if regex.search(pair["key"].replace(" ", "")):
            return pair
    return None


# ── 업체 문서 ─────────────────────────────────────────────────


def extract_permit(layout: dict[str, Any]) -> dict[str, Any]:
    """허가증에서 허가 품목을 읽는다. 표(폐기물 종류/품목 열)와 "허가 품목: ..." 키-값을 본다."""
    pairs = key_values(layout)
    entries: list[dict[str, Any]] = []

    def add(raw: str, confidence: Any, extra: str = "") -> None:
        item = _waste_item(raw)
        materials = _materials(f"{raw} {extra}")
        if not raw.strip() or (item is None and not materials):
            return
        reasons = []
        if item is None:
            reasons.append("폐기물 종류를 알아보지 못했습니다")
        if _low(confidence):
            reasons.append("인식 확신이 낮습니다")
        entries.append(
            {
                "wasteItem": item,
                "materials": materials,
                "raw": f"{raw} {extra}".strip(),
                "needsReview": bool(reasons),
                "reviewReasons": reasons,
            }
        )

    for table in tables(layout):
        kind = _find(table["headers"], "폐기물종류", "폐기물의종류", "품목", "대상폐기물")
        if kind is None:
            continue
        detail = _find(table["headers"], "재질", "세부", "성상")
        for row in table["rows"]:
            extra = row[detail]["text"] if detail is not None and detail != kind else ""
            add(row[kind]["text"], row[kind]["confidence"], extra)
    for pair in pairs:
        if re.search(r"허가품목|영업대상|처리대상|대상폐기물", pair["key"].replace(" ", "")):
            for part in re.split(r"[\n;]|,(?![^(]*\))", pair["value"]):
                add(part.strip(), pair["confidence"])

    fields = []
    for index, entry in enumerate(entries):
        label = entry["wasteItem"] or "미확인 품목"
        value = f"{label} ({', '.join(entry['materials'])})" if entry["materials"] else label
        fields.append(
            _field(
                f"permit-{index + 1}",
                "허가 품목",
                value,
                None,
                entry["raw"],
                entry["reviewReasons"],
            )
        )
    info = {}
    for key, pattern in (("businessName", r"상호|업체명"), ("permitNumber", r"허가번호")):
        pair = _first_value(pairs, pattern)
        info[key] = pair["value"] if pair else None
    return {"permits": entries, "fields": fields, **info}


def extract_purchase_criteria(layout: dict[str, Any]) -> dict[str, Any]:
    """매입 기준표: 재질·등급·형태별 오염도·수분 상한, 단가(매입이면 양수, 처리비면 음수), 최소 물량."""
    criteria: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    for table in tables(layout):
        headers = table["headers"]
        material_col = _find(headers, "재질", "품목", "수지")
        price_col = _find(headers, "단가", "매입가", "가격")
        fee_col = _find(headers, "처리비")
        if material_col is None or (price_col is None and fee_col is None):
            continue
        cols = {
            "grade": _find(headers, "등급"),
            "form": _find(headers, "형태", "성상"),
            "maxContaminationPct": _find(headers, "오염", "이물"),
            "maxMoisturePct": _find(headers, "수분", "함수"),
            "minMonthlyKg": _find(headers, "최소물량", "최소", "물량"),
        }
        for row in table["rows"]:
            if not any(cell["text"] for cell in row):
                continue
            reasons: list[str] = []
            material_text = row[material_col]["text"]
            found = _materials(material_text)
            material = found[0] if found else (material_text or None)
            if not found:
                reasons.append("재질 이름을 알아보지 못했습니다")
            if len(found) > 1:
                reasons.append("한 칸에 재질이 여러 개 있습니다")
            grade = (
                row[cols["grade"]]["text"].replace("등급", "").strip()
                if cols["grade"] is not None
                else None
            )
            if grade is not None and grade not in GRADES:
                reasons.append(f"등급을 알아보지 못했습니다: '{grade}'")
                grade = None
            entry: dict[str, Any] = {
                "material": material,
                "grade": grade,
                "form": (row[cols["form"]]["text"] or None) if cols["form"] is not None else None,
            }
            for key in ("maxContaminationPct", "maxMoisturePct", "minMonthlyKg"):
                col = cols[key]
                entry[key] = _number(row[col]["text"]) if col is not None else None
                if col is not None and row[col]["text"] and entry[key] is None:
                    reasons.append(f"'{headers[col]}' 값을 숫자로 읽지 못했습니다")
            use_fee = price_col is None or (fee_col is not None and not row[price_col]["text"])
            col = fee_col if use_fee else price_col
            raw_price = row[col]["text"]
            price = _number(raw_price)
            if price is None:
                reasons.append("단가를 읽지 못했습니다")
            else:
                unit_text = f"{headers[col]} {raw_price}".replace(" ", "")
                if "톤" in unit_text or "/t" in unit_text.lower():
                    price = price / 1000
                    reasons.append("단위를 원/t에서 원/kg으로 바꿨습니다")
                if use_fee:
                    price = -abs(price)
            entry["unitPriceKrwPerKg"] = price
            if any(_low(cell["confidence"]) for cell in row):
                reasons.append("인식 확신이 낮은 칸이 있습니다")
            slug = ids.slug(f"{material or 'x'}_{grade or 'x'}").lower()
            entry["id"] = f"pc_{slug}_{len(criteria) + 1}"
            entry["raw"] = " | ".join(cell["text"] for cell in row)
            entry["needsReview"] = bool(reasons)
            entry["reviewReasons"] = reasons
            criteria.append(entry)
            name = " ".join(x for x in (material, grade, entry["form"]) if x)
            kind = "처리비" if use_fee else "매입 단가"
            fields.append(
                _field(f"{entry['id']}-단가", f"{name} {kind}", price, "원/kg", raw_price, reasons)
            )
            for key, label, unit in (
                ("maxContaminationPct", "오염도 상한", "%"),
                ("maxMoisturePct", "수분 상한", "%"),
                ("minMonthlyKg", "최소 물량", "kg/월"),
            ):
                if cols[key] is not None:
                    raw = row[cols[key]]["text"]
                    cell_reasons = (
                        ["인식 확신이 낮습니다"] if _low(row[cols[key]]["confidence"]) else []
                    )
                    fields.append(
                        _field(
                            f"{entry['id']}-{key}",
                            f"{name} {label}",
                            entry[key],
                            unit,
                            raw,
                            cell_reasons,
                        )
                    )
    return {"criteria": criteria, "fields": fields}


# ── 배출 기업 문서 ────────────────────────────────────────────


def extract_material_spec(layout: dict[str, Any]) -> dict[str, Any]:
    """원자재 명세: 품명과 재질(수지 종류)."""
    items: list[dict[str, Any]] = []
    for table in tables(layout):
        name_col = _find(table["headers"], "품명", "원자재", "자재명", "제품명")
        material_col = _find(table["headers"], "재질", "수지", "원료")
        if name_col is None and material_col is None:
            continue
        supplier_col = _find(table["headers"], "공급", "제조사", "업체")
        for row in table["rows"]:
            name = row[name_col]["text"] if name_col is not None else ""
            source = row[material_col]["text"] if material_col is not None else name
            found = _materials(f"{source} {name}" if material_col is None else source)
            if not name and not found:
                continue
            reasons = []
            if not found:
                reasons.append("재질 이름을 알아보지 못했습니다")
            if len(found) > 1:
                reasons.append("재질이 여러 개 적혀 있습니다")
            if any(_low(cell["confidence"]) for cell in row):
                reasons.append("인식 확신이 낮은 칸이 있습니다")
            items.append(
                {
                    "name": name or None,
                    "material": "+".join(found) if found else None,
                    "supplier": (row[supplier_col]["text"] or None)
                    if supplier_col is not None
                    else None,
                    "raw": " | ".join(cell["text"] for cell in row),
                    "needsReview": bool(reasons),
                    "reviewReasons": reasons,
                }
            )
    fields = [
        _field(
            f"material-{i + 1}",
            f"{item['name'] or '원자재'} 재질",
            item["material"],
            None,
            item["raw"],
            item["reviewReasons"],
        )
        for i, item in enumerate(items)
    ]
    return {"materials": items, "fields": fields}


def extract_msds(layout: dict[str, Any]) -> dict[str, Any]:
    """MSDS: 제품명과 구성 성분(이름, CAS 번호, 함유량)."""
    pairs = key_values(layout)
    product = _first_value(pairs, r"제품명|상품명|물질명")
    components: list[dict[str, Any]] = []
    for table in tables(layout):
        name_col = _find(table["headers"], "화학물질명", "성분", "물질명")
        cas_col = _find(table["headers"], "CAS", "cas")
        content_col = _find(table["headers"], "함유량", "함량", "%")
        if name_col is None or (cas_col is None and content_col is None):
            continue
        for row in table["rows"]:
            name = row[name_col]["text"]
            if not name:
                continue
            content_raw = row[content_col]["text"] if content_col is not None else ""
            reasons = []
            if "~" in content_raw or "-" in content_raw.strip("-"):
                reasons.append("함유량이 범위로 적혀 있어 앞의 값만 읽었습니다")
            if any(_low(cell["confidence"]) for cell in row):
                reasons.append("인식 확신이 낮은 칸이 있습니다")
            components.append(
                {
                    "name": name,
                    "casNo": (row[cas_col]["text"] or None) if cas_col is not None else None,
                    "contentPct": _number(content_raw),
                    "raw": " | ".join(cell["text"] for cell in row),
                    "needsReview": bool(reasons),
                    "reviewReasons": reasons,
                }
            )
    resin = _materials(
        " ".join([product["value"] if product else "", *[c["name"] for c in components]])
    )
    fields = []
    if product:
        reasons = ["인식 확신이 낮습니다"] if _low(product["confidence"]) else []
        fields.append(
            _field("msds-제품명", "제품명", product["value"], None, product["value"], reasons)
        )
    for i, item in enumerate(components):
        fields.append(
            _field(
                f"msds-성분-{i + 1}",
                f"{item['name']} 함유량",
                item["contentPct"],
                "%",
                item["raw"],
                item["reviewReasons"],
            )
        )
    return {
        "productName": product["value"] if product else None,
        "components": components,
        "resinCandidates": resin,
        "fields": fields,
    }


def extract_contract(layout: dict[str, Any]) -> dict[str, Any]:
    """위탁 계약서: 현행 처리 방법과 단가(원/kg). 단가는 배출 기업이 내는 처리비라 양수로 둔다."""
    pairs = key_values(layout)
    text = layout_text(layout)
    fields = []

    method_pair = _first_value(pairs, r"처리방법|처리방식|위탁내용")
    method_source = method_pair["value"] if method_pair else text
    method = next(
        (m for m in METHODS if m.replace(" ", "") in method_source.replace(" ", "")), None
    )
    reasons = []
    if method_pair is None:
        reasons.append("'처리 방법' 항목을 찾지 못해 본문에서 읽었습니다")
    elif _low(method_pair["confidence"]):
        reasons.append("인식 확신이 낮습니다")
    fields.append(
        _field(
            "contract-처리방법",
            "현행 처리 방법",
            method,
            None,
            (method_pair or {}).get("value", ""),
            reasons,
        )
    )

    price_pair = _first_value(pairs, r"단가|처리비|위탁비")
    price_source = price_pair["value"] if price_pair else text
    match = re.search(
        r"(\d[\d,]*(?:\.\d+)?)\s*원\s*/\s*(kg|킬로그램|톤|t)(?![A-Za-z])",
        price_source,
        re.IGNORECASE,
    )
    unit_cost = None
    reasons = []
    if match:
        unit_cost = float(match.group(1).replace(",", ""))
        if match.group(2).lower() in ("톤", "t"):
            unit_cost /= 1000
            reasons.append("단위를 원/t에서 원/kg으로 바꿨습니다")
    if price_pair is None:
        reasons.append("'단가' 항목을 찾지 못해 본문에서 읽었습니다")
    elif _low(price_pair["confidence"]):
        reasons.append("인식 확신이 낮습니다")
    fields.append(
        _field(
            "contract-단가",
            "현행 처리 단가",
            unit_cost,
            "원/kg",
            match.group(0) if match else "",
            reasons,
        )
    )

    vendor = _first_value(pairs, r"수탁자|처리업체|수탁업체|^을$")
    period = _first_value(pairs, r"계약기간")
    return {
        "method": method,
        "unitCostKrwPerKg": unit_cost,
        "vendorName": vendor["value"] if vendor else None,
        "period": period["value"] if period else None,
        "fields": fields,
    }


EXTRACTORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "permit": extract_permit,
    "purchase_criteria": extract_purchase_criteria,
    "material_spec": extract_material_spec,
    "msds": extract_msds,
    "contract": extract_contract,
}


def confirm_fields(
    registry: Any,
    org_id: str,
    document_name: str,
    fields: list[dict[str, Any]],
    confirmed_by: str,
    *,
    today: str,
) -> list[str]:
    """사람이 확인한 값(confirmed=True)만 doc 근거로 등록한다. 값이 없는 항목은 등록하지 않는다."""
    registered = []
    for item in fields:
        if item.get("confirmed") is not True or item.get("value") is None:
            continue
        value = item["value"]
        if isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)):
            value = str(value)
        raw = str(item.get("raw") or item["value"])
        evidence = registry.put(
            Evidence(
                id=ids.doc_id(org_id, document_name, item["field"]),
                kind="doc",
                label=str(item.get("label") or item["field"]),
                value=value,
                unit=item.get("unit"),
                excerpt=raw,
                source=f"{document_name} (Document Intelligence 추출, {confirmed_by} 확인)",
                as_of=today,
                access=(org_id,),
                meta={
                    "document": document_name,
                    "field": item["field"],
                    "confirmedBy": confirmed_by,
                    "neededReview": bool(item.get("needsReview")),
                },
            )
        )
        registered.append(evidence.id)
    return registered
