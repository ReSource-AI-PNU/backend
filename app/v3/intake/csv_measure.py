"""측정값 CSV. 양식을 내려 주고, 올라온 파일을 행과 열을 짚어 검사한 뒤 measure 근거로 등록한다."""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Callable
from typing import Any

from app.v3 import ids
from app.v3.evidence.model import Evidence
from app.v3.textutil import josa

# 필드 -> (양식에 쓰는 한국어 머리글, 필수 여부, 받아 주는 별칭)
COLUMNS: dict[str, tuple[str, bool, tuple[str, ...]]] = {
    # 화면과 양식은 "폐기물 이름"이다. 예전 양식의 "흐름 이름"도 받는다
    "streamName": (
        "폐기물 이름",
        True,
        ("폐기물", "폐기물명", "흐름 이름", "흐름", "흐름명", "stream", "stream name", "name"),
    ),
    "process": ("공정", True, ("process",)),
    "material": ("재질", True, ("material",)),
    "contaminationPct": (
        "오염도(%)",
        True,
        ("오염도", "contamination(%)", "contamination", "contamination pct"),
    ),
    "moisturePct": ("수분(%)", True, ("수분", "moisture(%)", "moisture", "moisture pct")),
    "measuredOn": ("측정일", True, ("측정일자", "measured on", "measured", "date")),
    "origin": ("출처", False, ("origin",)),
    "form": ("형태", False, ("form",)),
    "lot": ("로트", False, ("lot",)),
    "monthlyKg": (
        "월 발생량",
        False,
        ("월 발생량(kg)", "월발생량", "monthly kg", "monthly", "monthly kg(kg)"),
    ),
}
ORIGINS = ("PIR", "PCR")
# 형태는 정해 둔 낱말만 받는다(계약 6장). 올린 파일의 글이 흐름 자료를 거쳐 에이전트 입력으로
# 들어가지 못하게 한다(업로드 문서는 근거로만 쓰고 지시로 해석하지 않는다).
FORMS = ("가공 전", "압축", "분쇄", "펠렛", "필름")
_DATE = re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$")
_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _norm(header: str) -> str:
    return re.sub(r"[\s_]+", "", header.replace("\ufeff", "")).lower()


_ALIASES: dict[str, str] = {}
for _field, (_label, _required, _names) in COLUMNS.items():
    for _name in (_label, _field, *_names):
        _ALIASES[_norm(_name)] = _field


def template_csv() -> bytes:
    """엑셀에서 바로 열리도록 UTF-8 BOM을 붙인다. 예시 행은 가상 값이다."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([label for label, _, _ in COLUMNS.values()])
    writer.writerow(
        ["스프루·런너", "사출", "PP", "1.0", "0.3", "2026-11-10", "PIR", "가공 전", "L-001", "600"]
    )
    writer.writerow(
        ["포장 필름", "입고 포장 해체", "LDPE", "3.5", "1.2", "2026-11-10", "PCR", "필름", "", ""]
    )
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


def _decode(data: bytes) -> tuple[str | None, str | None]:
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data[3:].decode("utf-8"), "utf-8-sig"
        except UnicodeDecodeError:
            return None, None
    for encoding in ("utf-8", "cp949"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return None, None


def _delimiter(first_line: str) -> str:
    counts = {d: first_line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _number(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("%", "").replace("kg", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _date(text: str) -> str | None:
    match = _DATE.match(text.strip())
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    if not 1 <= month <= 12:
        return None
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    limit = 29 if month == 2 and leap else _DAYS[month - 1]
    if not 1 <= day <= limit:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_csv(data: bytes) -> dict[str, Any]:
    """행 번호는 파일의 줄 번호다(머리글이 1행). 오류가 있는 행은 rows에 넣지 않는다."""
    errors: list[dict[str, Any]] = []

    def fail(row: int, column: str | None, message: str) -> None:
        errors.append({"row": row, "column": column, "message": message})

    text, encoding = _decode(data)
    if text is None:
        fail(0, None, "파일 인코딩을 읽을 수 없습니다. UTF-8 또는 CP949(엑셀 CSV)로 저장해 주세요.")
        return {"rows": [], "errors": errors, "encoding": None}
    lines = text.splitlines()
    if not any(line.strip() for line in lines):
        fail(1, None, "파일이 비어 있습니다.")
        return {"rows": [], "errors": errors, "encoding": encoding}

    table = list(csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(lines[0])))
    headers = [h.strip() for h in table[0]]
    mapping: dict[int, str] = {}
    written: dict[str, str] = {}
    for index, name in enumerate(headers):
        field = _ALIASES.get(_norm(name))
        if not name:
            continue
        if field is None:
            fail(1, name, f"모르는 열입니다: '{name}'. 양식의 머리글을 써 주세요.")
        elif field in written:
            fail(1, name, f"'{written[field]}' 열과 같은 내용의 열이 두 번 있습니다.")
        else:
            mapping[index] = field
            written[field] = name
    missing = [
        label
        for field, (label, required, _) in COLUMNS.items()
        if required and field not in written
    ]
    for label in missing:
        fail(1, label, f"필수 열 {josa(repr(label), '이가')} 없습니다.")
    if missing:
        return {"rows": [], "errors": errors, "encoding": encoding}

    def column(field: str) -> str:
        return written.get(field, COLUMNS[field][0])

    rows: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    for number, cells in enumerate(table[1:], start=2):
        if not any(cell.strip() for cell in cells):
            continue
        before = len(errors)
        raw = {field: (cells[i].strip() if i < len(cells) else "") for i, field in mapping.items()}
        if len(cells) > len(headers) and any(c.strip() for c in cells[len(headers) :]):
            fail(number, None, "머리글보다 칸이 많습니다. 값 안의 쉼표는 큰따옴표로 감싸 주세요.")
        row: dict[str, Any] = {"rowNumber": number}
        for field in ("streamName", "process", "material"):
            if raw[field]:
                row[field] = raw[field]
            else:
                fail(number, column(field), f"'{column(field)}' 값이 비어 있습니다.")
        for field in ("contaminationPct", "moisturePct"):
            if not raw[field]:
                fail(number, column(field), f"'{column(field)}' 값이 비어 있습니다.")
                continue
            value = _number(raw[field])
            if value is None:
                fail(number, column(field), f"숫자가 아닙니다: '{raw[field]}'.")
            elif not 0 <= value <= 100:
                fail(number, column(field), f"0에서 100 사이여야 합니다: '{raw[field]}'.")
            else:
                row[field] = value
        if not raw["measuredOn"]:
            fail(number, column("measuredOn"), f"'{column('measuredOn')}' 값이 비어 있습니다.")
        else:
            day = _date(raw["measuredOn"])
            if day is None:
                fail(
                    number,
                    column("measuredOn"),
                    f"날짜 형식이 아닙니다: '{raw['measuredOn']}'. 2026-11-10처럼 써 주세요.",
                )
            else:
                row["measuredOn"] = day
        origin = raw.get("origin", "")
        if origin and origin.upper() not in ORIGINS:
            fail(number, column("origin"), f"출처는 PIR 또는 PCR이어야 합니다: '{origin}'.")
        row["origin"] = origin.upper() or None
        form = raw.get("form", "")
        if form and form not in FORMS:
            fail(number, column("form"), f"형태는 {', '.join(FORMS)} 가운데 하나여야 합니다.")
        row["form"] = form if form in FORMS else None
        row["lot"] = raw.get("lot") or None
        row["monthlyKg"] = None
        if raw.get("monthlyKg"):
            value = _number(raw["monthlyKg"])
            if value is None:
                fail(number, column("monthlyKg"), f"숫자가 아닙니다: '{raw['monthlyKg']}'.")
            elif value < 0:
                fail(number, column("monthlyKg"), f"0 이상이어야 합니다: '{raw['monthlyKg']}'.")
            else:
                row["monthlyKg"] = value
        if "streamName" in row and "measuredOn" in row:
            key = (row["streamName"], row["measuredOn"])
            if key in seen:
                fail(
                    number,
                    column("streamName"),
                    f"{seen[key]}행과 폐기물 이름·측정일이 같습니다. 한 폐기물은 측정일마다 한 줄만 올려 주세요.",
                )
            else:
                seen[key] = number
        if len(errors) == before:
            rows.append(row)
    if not rows and not errors:
        fail(2, None, "측정값 행이 없습니다.")
    return {"rows": rows, "errors": errors, "encoding": encoding}


def _excerpt(row: dict[str, Any]) -> str:
    parts = []
    for field, (label, _, _) in COLUMNS.items():
        value = row.get(field)
        if value is not None and value != "":
            parts.append(f"{label}={value:g}" if isinstance(value, float) else f"{label}={value}")
    return ", ".join(parts)


def register_measures(
    registry: Any,
    org_id: str,
    stream_id_for: Callable[[dict[str, Any]], str | None],
    rows: list[dict[str, Any]],
    source_name: str,
) -> list[str]:
    """오염도, 수분, 월 발생량을 measure 근거로 등록한다. 흐름을 찾지 못한 행(None)은 건너뛴다."""
    registered: list[str] = []
    for row in rows:
        stream_id = stream_id_for(row)
        if not stream_id:
            continue
        values = (
            ("오염도", row.get("contaminationPct"), "%"),
            ("수분", row.get("moisturePct"), "%"),
            ("월발생량", row.get("monthlyKg"), "kg/월"),
        )
        for label, value, unit in values:
            if value is None:
                continue
            evidence = registry.put(
                Evidence(
                    id=ids.measure_id(org_id, stream_id, label, row["measuredOn"]),
                    kind="measure",
                    label=f"{row['streamName']} {label}",
                    value=float(value),
                    unit=unit,
                    excerpt=_excerpt(row),
                    source=f"{source_name} {row['rowNumber']}행",
                    as_of=row["measuredOn"],
                    access=(org_id,),
                    meta={
                        "streamId": stream_id,
                        "field": label,
                        "file": source_name,
                        "row": row["rowNumber"],
                        "lot": row.get("lot"),
                    },
                )
            )
            registered.append(evidence.id)
    return registered
