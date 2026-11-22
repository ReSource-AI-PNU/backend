"""읽기 요청(진단 뷰·회사 기록·실적) 하나 안에서는 피드백 목록을 종류마다 한 번만 읽는다."""

from collections import Counter

from app.v3 import feedback, service
from tests.v3.runtime_fixtures import HANBIT, make_app, to_approval


class Counting:
    """list_feedback 호출 수를 센다. 나머지는 원래 레지스트리에 넘긴다."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: Counter[str] = Counter()

    def list_feedback(self, kind):
        self.calls[kind] += 1
        return self._inner.list_feedback(kind)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def counted(app):
    registry = Counting(app.registry)
    app.registry = registry
    return registry


def test_reuse_reads_each_kind_once_and_only_inside():
    app = make_app()
    registry = counted(app)
    with feedback.reuse():
        first = feedback._feedback(app, "outcome")
        again = feedback._feedback(app, "outcome")
        with feedback.reuse():  # 안쪽에서 다시 열어도 바깥 것을 쓴다
            feedback._feedback(app, "outcome")
        feedback._feedback(app, "correction")
    assert first == again
    assert registry.calls == Counter({"outcome": 1, "correction": 1})
    feedback._feedback(app, "outcome")  # 닫은 뒤에는 다시 읽는다(쓰기 요청은 늘 새로 읽는다)
    assert registry.calls["outcome"] == 2


def test_reuse_returns_a_new_list_each_time():
    app = make_app()
    with feedback.reuse():
        feedback._feedback(app, "outcome").append("바깥에서 고친 값")
        assert "바깥에서 고친 값" not in feedback._feedback(app, "outcome")


def test_view_memory_report_read_feedback_once_per_kind():
    app = make_app()
    session_id = to_approval(app, HANBIT)["session"]["id"]
    registry = counted(app)
    for read in (
        lambda: service.get_view(app, session_id, HANBIT),
        lambda: service.get_memory(app, HANBIT, HANBIT),
        lambda: service.get_report(app, HANBIT, HANBIT),
    ):
        registry.calls.clear()
        read()
        assert all(count == 1 for count in registry.calls.values()), registry.calls
