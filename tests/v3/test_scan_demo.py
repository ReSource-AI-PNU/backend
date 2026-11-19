"""체험하기의 시연 보관장 3D 스캔: 저장소에 있는 실제 재구성 결과(app/v3/demo/scan/)로 시드한다."""

import pytest

from app.v3.demo.scope import demo_app
from app.v3.scan import demo as scan_demo
from app.v3.scan import views
from tests.v3.runtime_fixtures import HANBIT, make_app

pytestmark = pytest.mark.skipif(
    not (scan_demo.ASSETS / "hanbit-result.json").exists(), reason="시연 스캔 자산이 없다"
)


def test_demo_scan_is_seeded_with_evidence_and_a_proposal():
    app = make_app()
    view = views.get_scan(demo_app(app), HANBIT, scan_demo.SCAN_ID, HANBIT)
    assert view["status"] == "done" and view["summary"]["pileCount"] >= 3
    assert all(p["streamId"] for p in view["piles"])  # 정답 파일의 지정
    for pile in view["piles"]:
        claims = [pile["volume"], *(pile["weight"] or {}).values()]
        for claim in claims:
            for eid in claim["evidence"]:
                assert app.registry.get(eid, HANBIT)
    assert view["layout"]["proposal"] is not None
    current = view["layout"]["current"]["mixedPairs"]
    assert view["layout"]["proposal"]["metrics"]["mixedPairs"] <= current
    assert views.list_scans(demo_app(app), HANBIT, HANBIT)["canScan"] is False
