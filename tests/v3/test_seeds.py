import json

import pytest

from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.evidence.seeds import SEED_DIR, load_seed_dir, load_seed_file

ASSUMPTION = {
    "id": "assumption:현행처리-혼합소각@v1",
    "kind": "assumption",
    "label": "현행 처리 단가 (혼합 소각 가정)",
    "value": 250,
    "unit": "원/kg",
    "excerpt": "현행 처리를 입력하지 않은 흐름은 혼합 소각으로 가정한다.",
    "source": "팀 가정값 v1",
    "asOf": "2026-11-16",
}
LAW = {
    "id": "law:시험용법:제1조@2026-01-01",
    "kind": "law",
    "excerpt": "수탁자의 허가 품목을 확인하여야 한다.",
    "source": "시험용법",
    "asOf": "2026-01-01",
    "access": ["org_hanbit"],
}


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_seed_file(tmp_path):
    registry = InMemoryRegistry()
    assert load_seed_file(registry, write(tmp_path / "a.json", [ASSUMPTION, LAW])) == 2
    assert registry.get(ASSUMPTION["id"], "org_x").value == 250
    assert registry.get(LAW["id"], "org_hanbit").access == ("org_hanbit",)


def test_to_dict_output_can_be_loaded_back(tmp_path):
    source = Evidence.from_dict(ASSUMPTION)
    registry = InMemoryRegistry()
    load_seed_file(registry, write(tmp_path / "a.json", [source.to_dict()]))
    assert registry.get(source.id) == source


def test_load_seed_dir_in_name_order(tmp_path):
    newer = {**ASSUMPTION, "value": 260}
    write(tmp_path / "10_base.json", [ASSUMPTION, LAW])
    write(tmp_path / "20_override.json", [newer])
    (tmp_path / "README.md").write_text("문서", encoding="utf-8")
    registry = InMemoryRegistry()
    assert load_seed_dir(registry, tmp_path) == 3
    stored = registry.get(ASSUMPTION["id"])
    assert (stored.value, stored.version) == (260, 2)
    assert load_seed_dir(InMemoryRegistry(), tmp_path / "없는폴더") == 0


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("{not json", "bad.json: JSON"),
        (json.dumps({"id": "x"}), "bad.json: 최상위"),
        (json.dumps([ASSUMPTION, "문자열"]), "bad.json: 2번째 항목"),
        (json.dumps([{**ASSUMPTION, "asOf": ""}]), "bad.json: 1번째 항목에 필수 키가 없다: asOf"),
        (json.dumps([ASSUMPTION, {"kind": "law", "asOf": "2026-01-01"}]), "bad.json: 2번째 항목"),
        (json.dumps([{**ASSUMPTION, "kind": "law"}]), "bad.json: 1번째 항목의 형식"),
        (json.dumps([{**ASSUMPTION, "kind": "소문"}]), "bad.json: 1번째 항목의 형식"),
        (json.dumps([{**ASSUMPTION, "version": "하나"}]), "bad.json: 1번째 항목의 형식"),
        (json.dumps([ASSUMPTION, LAW, ASSUMPTION]), "bad.json: 3번째 항목의 ID가"),
    ],
)
def test_format_errors_name_file_and_item(tmp_path, content, expected):
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    registry = InMemoryRegistry()
    with pytest.raises(ValueError, match=expected):
        load_seed_file(registry, path)
    assert registry.list() == []  # 하나라도 틀리면 아무것도 넣지 않는다


def test_packaged_seed_dir_loads():
    assert SEED_DIR.is_dir() and not (SEED_DIR / "__init__.py").exists()
    assert load_seed_dir(InMemoryRegistry()) >= 0
