from app.v3.evidence.registry import InMemoryRegistry
from app.v3.intake.csv_measure import parse_csv, register_measures, template_csv

KO = "흐름 이름,공정,재질,오염도(%),수분(%),측정일,출처,형태,로트,월 발생량\n"


def test_template_round_trips_with_bom():
    data = template_csv()
    assert data.startswith(b"\xef\xbb\xbf")
    result = parse_csv(data)
    assert result["encoding"] == "utf-8-sig"
    assert result["errors"] == []
    assert [r["streamName"] for r in result["rows"]] == ["스프루·런너", "포장 필름"]
    first = result["rows"][0]
    assert first["rowNumber"] == 2
    assert first["contaminationPct"] == 1.0
    assert first["monthlyKg"] == 600.0
    assert result["rows"][1]["monthlyKg"] is None


def test_cp949_korean_headers():
    data = (KO + '스프루,사출,PP,1.5,0.4,2026.11.10,pir,가공 전,L1,"1,200"\n').encode("cp949")
    result = parse_csv(data)
    assert result["encoding"] == "cp949"
    assert result["errors"] == []
    row = result["rows"][0]
    assert row["measuredOn"] == "2026-11-10"
    assert row["origin"] == "PIR"
    assert row["monthlyKg"] == 1200.0


def test_english_headers_and_semicolon():
    data = b"Stream Name;process;MATERIAL;contamination_pct;moisture;date\nfilm;unpack;LDPE;3;1;2026/11/09\n"
    result = parse_csv(data)
    assert result["errors"] == []
    assert result["rows"][0]["material"] == "LDPE"
    assert result["rows"][0]["measuredOn"] == "2026-11-09"
    assert result["rows"][0]["origin"] is None


def test_errors_point_at_row_and_column():
    data = (
        KO
        + "스프루,사출,PP,1.0,0.3,2026-11-10,PIR,,,\n"
        + "불량품,사출,PP,120,물,2026-13-01,XYZ,,,-5\n"
        + ",사출,PP,1,1,2026-11-10,,,,\n"
        + "스프루,사출,PP,2.0,0.3,2026-11-10,PIR,,,\n"
    ).encode("utf-8")
    result = parse_csv(data)
    assert [r["rowNumber"] for r in result["rows"]] == [2]
    found = {(e["row"], e["column"]) for e in result["errors"]}
    assert found == {
        (3, "오염도(%)"),
        (3, "수분(%)"),
        (3, "측정일"),
        (3, "출처"),
        (3, "월 발생량"),
        (4, "흐름 이름"),
        (5, "흐름 이름"),
    }
    duplicate = next(e for e in result["errors"] if e["row"] == 5)
    assert "2행" in duplicate["message"]


def test_missing_required_and_unknown_columns():
    result = parse_csv(
        "흐름 이름,공정,재질,수분(%),측정일,비고\n스프루,사출,PP,0.3,2026-11-10,x\n".encode()
    )
    assert result["rows"] == []
    assert {"row": 1, "column": "오염도(%)"}.items() <= result["errors"][-1].items()
    assert any(e["column"] == "비고" and "모르는 열" in e["message"] for e in result["errors"])


def test_unreadable_and_empty_files():
    assert parse_csv(b"\xff\xfe\x00\x01\x81")["errors"][0]["message"].startswith("파일 인코딩")
    assert parse_csv(b"")["errors"][0]["message"] == "파일이 비어 있습니다."
    assert parse_csv(KO.encode())["errors"][0]["message"] == "측정값 행이 없습니다."


def test_register_measures_creates_private_evidence():
    registry = InMemoryRegistry()
    rows = parse_csv(template_csv())["rows"]
    lookup = {"스프루·런너": "st_sprue"}
    ids_ = register_measures(
        registry, "org_hanbit", lambda row: lookup.get(row["streamName"]), rows, "측정값.csv"
    )
    assert ids_ == [
        "measure:org_hanbit:st_sprue:오염도@2026-11-10",
        "measure:org_hanbit:st_sprue:수분@2026-11-10",
        "measure:org_hanbit:st_sprue:월발생량@2026-11-10",
    ]
    evidence = registry.get(ids_[0], "org_hanbit")
    assert (evidence.value, evidence.unit, evidence.kind) == (1.0, "%", "measure")
    assert evidence.source == "측정값.csv 2행"
    assert "오염도(%)=1" in evidence.excerpt
    assert registry.get(ids_[2], "org_hanbit").unit == "kg/월"
    assert registry.list(org_id="org_other") == []
