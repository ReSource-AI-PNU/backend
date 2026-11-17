"""화면과 근거의 기준일은 한국 날짜다. 서버 시계가 UTC여도 KST 11-18 06시 50분의 답은 11-18이다."""

from __future__ import annotations

from datetime import UTC, datetime

from app.v3 import service
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.store import InMemoryStore


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):  # KST 2026-11-18 06:50 = UTC 2026-11-17 21:50
        moment = datetime(2026, 11, 17, 21, 50, tzinfo=UTC)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


def test_today_is_the_korean_date(monkeypatch):
    monkeypatch.setattr(service, "datetime", _Clock)
    app = service.App(InMemoryStore(), InMemoryRegistry())
    assert app.today == "2026-11-18"
    # 정해 준 날짜가 있으면 그대로다(시험과 시연)
    assert service.App(InMemoryStore(), InMemoryRegistry(), today="2026-11-17").today == (
        "2026-11-17"
    )
