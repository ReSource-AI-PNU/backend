"""공개 출처에서 모은 law, ref 근거 파일의 형식을 확인한다. 값의 옳고 그름은 docs/v3/sources.md의 출처로 본다."""

import json
import re
from pathlib import Path

import pytest

from app.v3 import ids
from app.v3.evidence.model import Evidence

SEEDS = Path(__file__).parents[2] / "app" / "v3" / "evidence" / "seeds"
UNITS = {
    "원", "원/kg", "원/t", "원/월", "원/회", "원/km", "원/시간", "시간/월", "원/tCO2e", "kg", "t", "kg/월", "t/월", "%",
    "km", "m", "m3", "kg/m3", "tCO2e", "kgCO2e", "tCO2e/월", "kgCO2e/kg", "tCO2e/t", "회", "회/월",
    "일", "개",
}  # fmt: skip
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load(name):
    return json.loads((SEEDS / name).read_text("utf-8"))


LAWS = load("laws.json")
REFS = load("refs.json")


def test_files_are_lists_with_unique_ids():
    all_ids = [item["id"] for item in LAWS + REFS]
    assert len(all_ids) == len(set(all_ids))


@pytest.mark.parametrize("item", LAWS + REFS, ids=lambda item: item["id"])
def test_common_fields(item):
    ev = Evidence.from_dict(item)
    assert ev.to_dict()["id"] == item["id"]
    assert ev.access == ("*",)
    assert ISO_DATE.match(ev.as_of)
    assert ev.source
    assert ev.meta["url"].startswith("https://")
    assert ev.meta["retrievedAt"]


@pytest.mark.parametrize("item", LAWS, ids=lambda item: item["id"])
def test_law_items(item):
    meta = item["meta"]
    assert item["kind"] == "law"
    assert item["id"] == ids.law_id(meta["lawName"], meta["article"], meta["effective"])
    assert item["asOf"] == meta["effective"]
    assert item["excerpt"].strip()
    assert item["value"] is None
    assert "law.go.kr" in meta["url"]
    assert meta["title"]
    # 조 단위 항목은 원문이 조 번호와 제목으로 시작한다. 호 단위 항목은 호 번호로 시작한다.
    if "parent" in meta:
        ho = meta["article"].split("제")[-1].replace("호의", "의").replace("호", "")
        assert item["excerpt"].startswith(ho + ". ")
    else:
        assert item["excerpt"].startswith(f"{meta['article']}({meta['title']})")
    assert "[시행일:" not in item["excerpt"]


@pytest.mark.parametrize("item", REFS, ids=lambda item: item["id"])
def test_ref_items(item):
    meta = item["meta"]
    assert item["kind"] == "ref"
    assert item["id"] == ids.ref_id(meta["series"], meta["item"], meta["period"])
    assert isinstance(item["value"], (int, float))
    assert not isinstance(item["value"], bool)
    assert item["value"] > 0
    assert item["unit"] in UNITS
    assert item["excerpt"].strip()
    # 값이 원문 줄에 그대로 있어야 한다 (천 단위 쉼표 허용).
    plain = f"{item['value']:g}"
    with_comma = f"{item['value']:,.1f}".rstrip("0").rstrip(".")
    assert plain in item["excerpt"].replace(",", "") or with_comma in item["excerpt"]
