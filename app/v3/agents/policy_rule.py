"""규칙 정책. 네트워크 없이 도는 결정적 정책이고, LLM 프로토콜을 그대로 따른다.

모델이 아니다. 화면과 실행 기록에는 모델 이름 대신 "규칙 정책(모델 미연결)"이라고 남는다.
에이전트가 주는 입력(JSON)만 읽고, 모델과 똑같이 type, text, claimRefs, toolCalls를 낸다.
숫자는 직접 쓰지 않고 풀의 claim ID로만 가리키므로 근거 검증기도 똑같이 거친다.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.v3.evidence.render import format_value
from app.v3.judge.common import PRIVATE_CHECKS
from app.v3.textutil import josa, particle

PROVIDER = "rule"
MODEL_NAME = "규칙 정책(모델 미연결)"
_QUALITY_FIELDS = {"contaminationPct": "오염도", "moisturePct": "수분", "form": "형태"}
_BASIS_KEYS = {"moisture_basis": "moisturePct", "contamination_basis": "contaminationPct"}
_SUBJECT = {key: josa(_QUALITY_FIELDS[key], "이가") for key in ("contaminationPct", "moisturePct")}


class RulePolicy:
    provider = PROVIDER
    model = MODEL_NAME

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        briefing = self._briefing(messages)
        role = briefing.get("role")
        if role == "company":
            output = _Company(briefing).decide()
        elif role == "vendor":
            output = _Vendor(briefing).decide()
        else:
            raise ValueError("규칙 정책은 회사 측과 업체 측 입력만 받는다")
        output.setdefault("claimRefs", [])
        output.setdefault("toolCalls", [])
        output.setdefault("streamIds", [])
        output.setdefault("text", "")
        return {
            "content": json.dumps(output, ensure_ascii=False),
            "tool_calls": [],
            "usage": {},
        }

    @staticmethod
    def _briefing(messages: list[dict[str, Any]]) -> dict[str, Any]:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            try:
                data = json.loads(message.get("content") or "")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and "role" in data:
                return data
        raise ValueError("규칙 정책이 읽을 입력(JSON)이 없다")


def _ph(claim_id: str) -> str:
    return "{{" + claim_id + "}}"


class _Base:
    def __init__(self, briefing: dict[str, Any]) -> None:
        self.b = briefing
        self.streams = {s["id"]: s for s in briefing.get("streams") or []}
        self.pool = {c["id"]: c for c in briefing.get("pool") or []}
        given = briefing.get("toolResults") or []
        self.results = [r for r in given if r.get("status") != "error"]
        # 실패한 호출도 부른 것이다(같은 입력이면 다시 불러도 같은 이유로 실패한다)
        self.failed = [r for r in given if r.get("status") == "error"]
        self.refs: list[str] = []
        self.incoming = briefing.get("incoming") or {}
        self.thread = briefing.get("thread") or {}

    def use(self, claim_id: str | None) -> str | None:
        """claim을 본문에 쓴다. 풀에 없으면 None(그 값은 쓰지 않는다)."""
        if not claim_id or claim_id not in self.pool:
            return None
        if claim_id not in self.refs:
            self.refs.append(claim_id)
        return _ph(claim_id)

    def after(self, claim_id: str | None, pair: str) -> str:
        """claim 자리표시자 뒤에 올 조사. 화면에는 값과 단위로 그려지므로 단위의 끝소리로 고른다."""
        claim = self.pool.get(str(claim_id)) or {}
        unit = claim.get("unit")
        return particle(str(unit) if unit else format_value(claim.get("value")), pair)

    def tried(self, tool: str, **arguments: Any) -> bool:
        """결과가 있거나, 불렀다가 실패한 호출인가."""
        if self.result(tool, **arguments) is not None:
            return True
        return any(
            item.get("tool") == tool
            and all((item.get("arguments") or {}).get(k) == v for k, v in arguments.items())
            for item in self.failed
        )

    def result(self, tool: str, **arguments: Any) -> dict[str, Any] | None:
        for item in self.results:
            if item.get("tool") != tool:
                continue
            given = item.get("arguments") or {}
            if all(given.get(k) == v for k, v in arguments.items()):
                return item
        return None

    def name(self, stream_id: str) -> str:
        return str(self.streams.get(stream_id, {}).get("name") or stream_id)

    def describe(self, stream_id: str) -> str:
        """흐름 한 줄. 값은 자리표시자로, 모르는 값은 모른다고 적는다."""
        stream = self.streams[stream_id]
        facts = stream.get("facts") or {}
        unknown = stream.get("unknownFields") or []
        head = ", ".join(str(x) for x in (stream.get("material"), stream.get("form")) if x)
        parts = []
        kg = self.use(facts.get("monthlyKg"))
        parts.append(f"월 발생량 {kg}" if kg else "월 발생량 확인 필요")
        measured = self.use(facts.get("contaminationPct"))
        photo = None if measured else self.use(facts.get("photoContaminationPct"))
        if measured:
            # 담당자가 질문에 답한 값은 측정값이 아니다(pool이 claim ID를 answered…로 둔다)
            source = "담당자 답" if _answered(facts.get("contaminationPct")) else "측정"
            parts.append(f"오염도 {measured}({source})")
        elif photo:
            parts.append(f"사진 추정 오염 비율 {photo}")
        else:
            parts.append("오염도 확인 필요")
        moisture = self.use(facts.get("moisturePct"))
        if moisture:
            answered = _answered(facts.get("moisturePct"))
            parts.append(f"수분 {moisture}(담당자 답)" if answered else f"수분 {moisture}")
        elif "moisturePct" in unknown:
            parts.append("수분은 담당자도 모른다고 답했습니다")
        else:
            parts.append("수분 확인 필요")
        mixed = [str(c) for c in stream.get("contaminants") or [] if c]
        if mixed:
            parts.append(f"혼입물 {', '.join(mixed)}")
        label = f"{self.name(stream_id)}({head})" if head else self.name(stream_id)
        return f"- {label}: {', '.join(parts)}"


def _answered(claim_id: Any) -> bool:
    """담당자가 질문에 답한 값의 claim인가(pool.add_stream의 answered… ID)."""
    return bool(_ANSWERED.search(str(claim_id or "")))


_ANSWERED = re.compile(r"answered(?:Contamination|Moisture)Pct(?:_\d+)?$")


class _Company(_Base):
    def decide(self) -> dict[str, Any]:
        task = self.b.get("task")
        if task == "inquiry":
            return self.inquiry()
        if task == "confirm":
            return self.confirm()
        return self.reply()

    def _missing(self, tool: str, stream_ids: list[str]) -> list[dict[str, Any]]:
        return [
            {"name": tool, "arguments": {"streamId": sid}}
            for sid in stream_ids
            if self.result(tool, streamId=sid) is None
        ]

    def _asks(self, stream_ids: list[str]) -> list[str]:
        lines = []
        for sid in stream_ids:
            route = self.result("judge_route", streamId=sid) or {}
            asks = [c["ask"] for c in route.get("checks") or [] if c.get("ask")]
            for ask in dict.fromkeys(asks):
                lines.append(f"- {self.name(sid)}: {ask}")
        return lines

    def inquiry(self) -> dict[str, Any]:
        stream_ids = list(self.thread.get("streamIds") or [])
        calls = self._missing("judge_route", stream_ids)
        if calls:
            return {"type": "inquiry", "stage": 4, "streamIds": stream_ids, "toolCalls": calls}
        org, other = self.b.get("org") or {}, self.b.get("counterpart") or {}
        hello = (
            f"{other.get('name')} 담당자께, {org.get('name')}의 다음 폐기물을 받을 수 있는지와 "
            "조건을 문의드립니다."
        )
        lines = [hello, *[self.describe(sid) for sid in stream_ids]]
        asks = self._asks(stream_ids)
        if asks:
            lines += ["조건을 정할 때 다음도 함께 알려 주세요.", *asks]
        return {
            "type": "inquiry",
            "stage": 4,
            "streamIds": stream_ids,
            "text": "\n".join(lines),
            "claimRefs": self.refs,
        }

    def reply(self) -> dict[str, Any]:
        kind = self.incoming.get("type")
        state = self.b.get("state") or {}
        if kind == "question":
            return self.answer(state)
        offered = list(state.get("offered") or [])
        calls = self._missing("cost_carbon", offered)
        if offered and self.result("check_loading", streamIds=offered) is None:
            calls.append({"name": "check_loading", "arguments": {"streamIds": offered}})
        if calls:
            return {"type": "accept", "stage": 5, "toolCalls": calls}
        uncovered = list(state.get("uncovered") or [])
        if uncovered and not state.get("asked"):
            lines = ["제안 감사합니다. 아직 조건을 받지 못한 폐기물이 있어 여쭙니다."]
            asks = self._asks(uncovered)
            for sid in uncovered:
                if not any(line.startswith(f"- {self.name(sid)}:") for line in asks):
                    asks.append(f"- {self.name(sid)}: 받을 수 있는지와 조건을 알려 주세요.")
            return {
                "type": "question",
                "stage": 5,
                "streamIds": uncovered,
                "text": "\n".join([*lines, *asks]),
                "claimRefs": self.refs,
                "asks": [{"streamId": sid, "field": "terms"} for sid in uncovered],
            }
        return {"type": "accept", "stage": 5, "streamIds": offered}

    def answer(self, state: dict[str, Any]) -> dict[str, Any]:
        asks = list(self.incoming.get("asks") or [])
        stream_ids = list(
            dict.fromkeys(
                [a.get("streamId") for a in asks if a.get("streamId") in self.streams]
                or self.incoming.get("streamIds")
                or self.thread.get("streamIds")
                or []
            )
        )
        allowed = set(self.b.get("userQuestionFields") or [])
        need_user = []
        for ask in asks:
            stream = self.streams.get(ask.get("streamId"))
            field = ask.get("field")
            if stream is None or field not in allowed:
                continue
            known = (stream.get("facts") or {}).get(field) or (
                field == "contaminationPct"
                and (stream.get("facts") or {}).get("photoContaminationPct")
            )
            if known or field in (stream.get("unknownFields") or []) or stream.get(field):
                continue
            need_user.append({"streamId": stream["id"], "field": field})
        if need_user and not state.get("cannotAskUser"):
            return {
                "type": "ask_user",
                "stage": 4,
                "streamIds": stream_ids,
                "userQuestions": need_user,
            }
        lines = ["문의하신 내용에 답합니다.", *[self.describe(sid) for sid in stream_ids]]
        if need_user:
            lines.append("확인되지 않은 값은 담당자 확인이 필요한 상태로 남겨 둡니다.")
        return {
            "type": "answer",
            "stage": 4,
            "streamIds": stream_ids,
            "text": "\n".join(lines),
            "claimRefs": self.refs,
        }

    def confirm(self) -> dict[str, Any]:
        agreed = list((self.b.get("state") or {}).get("agreed") or [])
        lines = ["양쪽 담당자 승인이 끝나 다음 조건으로 확정합니다."]
        for item in agreed:
            parts = []
            price = self.use(item.get("unitPrice"))
            if price:
                value = (self.pool.get(str(item.get("unitPrice"))) or {}).get("value")
                paid = isinstance(value, (int, float)) and value < 0
                parts.append(f"{'처리비' if paid else '단가'} {price}")
            kg = self.use(item.get("monthlyKg"))
            if kg:
                parts.append(f"월 발생량 {kg}")
            lines.append(f"- {self.name(item['streamId'])}: {', '.join(parts) or '조건 확인 필요'}")
        lines.append("요청서와 이행 계획서를 함께 남깁니다.")
        return {
            "type": "confirm",
            "stage": 6,
            "streamIds": [item["streamId"] for item in agreed],
            "text": "\n".join(lines),
            "claimRefs": self.refs,
        }


class _Vendor(_Base):
    def decide(self) -> dict[str, Any]:
        stream_ids = list(self.thread.get("streamIds") or [])
        excluded: list[dict[str, str]] = []
        live = []
        for sid in stream_ids:
            reason = self.streams[sid].get("autoDecline")
            if reason:
                excluded.append({"streamId": sid, "reason": reason})
            else:
                live.append(sid)
        calls = []
        for sid in live:
            for tool in ("match_permit", "judge_route"):
                if self.result(tool, streamId=sid) is None:
                    calls.append({"name": tool, "arguments": {"streamId": sid}})
        if calls:
            return {"type": "answer", "stage": 4, "streamIds": stream_ids, "toolCalls": calls}

        acceptable = []
        for sid in live:
            permit = (self.result("match_permit", streamId=sid) or {}).get("output") or {}
            route = self.result("judge_route", streamId=sid) or {}
            if permit.get("matched") is not True:
                reason = permit.get("reason") or "허가 품목을 확인하지 못했습니다."
                excluded.append({"streamId": sid, "reason": reason})
            elif (route.get("output") or {}).get("acceptable") is not True:
                reason = (route.get("output") or {}).get("reason") or "매입 기준에 맞지 않습니다."
                excluded.append({"streamId": sid, "reason": reason})
            else:
                acceptable.append(sid)
        if not acceptable:
            return self.decline(stream_ids, excluded)

        calls = []
        for tool in ("check_loading", "route_extra_cost"):
            # 배출 기업 위치가 없으면 노선 비용은 계산하지 못한다(실패한 호출). 다시 부르지 않는다
            if not self.tried(tool, streamIds=acceptable):
                calls.append({"name": tool, "arguments": {"streamIds": acceptable}})
        if calls:
            return {"type": "answer", "stage": 5, "streamIds": stream_ids, "toolCalls": calls}

        state = self.b.get("state") or {}
        asked = {(a.get("streamId"), a.get("field")) for a in state.get("asked") or []}
        need = []
        for sid in acceptable:
            unknown = self.streams[sid].get("unknownFields") or []
            route = self.result("judge_route", streamId=sid) or {}
            for check in route.get("checks") or []:
                field = check.get("key")
                if (
                    field in ("contaminationPct", "moisturePct")
                    and check.get("status") == "conditional"
                    and field not in unknown
                    and (sid, field) not in asked
                ):
                    need.append({"streamId": sid, "field": field})
        if need:
            return self.question(need)

        offered = set(state.get("offered") or [])
        todo = [sid for sid in acceptable if sid not in offered]
        firm = [
            sid
            for sid in todo
            if (self.result("judge_route", streamId=sid) or {}).get("status") == "calculated"
        ]
        if firm:
            rest = [sid for sid in todo if sid not in firm]
            return self.terms("offer", firm, acceptable, excluded, rest)
        if todo:
            return self.terms("conditional", todo, acceptable, excluded, [])
        return {
            "type": "answer",
            "stage": 5,
            "streamIds": list(self.incoming.get("streamIds") or []),
            "text": "앞서 드린 조건과 같습니다. 달라진 내용은 없습니다.",
        }

    def decline(self, stream_ids: list[str], excluded: list[dict[str, str]]) -> dict[str, Any]:
        lines = ["문의하신 폐기물은 받을 수 없습니다."]
        lines += [f"- {self.name(x['streamId'])}: {x['reason']}" for x in excluded]
        reason = excluded[0]["reason"] if excluded else "받을 수 있는 폐기물이 없습니다."
        return {
            "type": "decline",
            "stage": 4,
            "streamIds": stream_ids,
            "text": "\n".join(lines),
            "excluded": excluded,
            "reason": reason,
        }

    def question(self, need: list[dict[str, str]]) -> dict[str, Any]:
        lines = ["매입 기준과 대조하려면 다음 값이 필요합니다. 값이 없으면 없다고 알려 주세요."]
        for sid in dict.fromkeys(n["streamId"] for n in need):
            fields = [_QUALITY_FIELDS[n["field"]] for n in need if n["streamId"] == sid]
            lines.append(f"- {self.name(sid)}: {', '.join(fields)}")
        return {
            "type": "question",
            "stage": 4,
            "streamIds": list(dict.fromkeys(n["streamId"] for n in need)),
            "text": "\n".join(lines),
            "asks": need,
        }

    def _criterion(self, stream_id: str) -> dict[str, str]:
        route = self.result("judge_route", streamId=stream_id) or {}
        cid = (route.get("output") or {}).get("criterionId")
        return (self.b.get("criteria") or {}).get(str(cid)) or {}

    def _conditions(self, stream_id: str) -> list[str]:
        """조건부의 확인할 것을 업체가 내거는 조건 문장으로 바꾼다."""
        route = self.result("judge_route", streamId=stream_id) or {}
        criterion = self._criterion(stream_id)
        stream = self.streams[stream_id]
        out = []
        for check in route.get("checks") or []:
            if check.get("key") in PRIVATE_CHECKS:
                continue
            if check.get("status") != "conditional":
                continue
            key = _BASIS_KEYS.get(check.get("key"), check.get("key"))
            if key in ("moisturePct", "contaminationPct"):
                # 값이 없을 때와, 값은 상한 안이지만 측정값이 아닐 때(…_basis) 모두 검수 조건이다
                limit = self.use(
                    criterion.get(
                        "maxMoisturePct" if key == "moisturePct" else "maxContaminationPct"
                    )
                )
                out.append(
                    f"반입 검수에서 {_SUBJECT[key]} 매입 기준 상한 {limit} 이하로 확인될 것"
                    if limit
                    else f"반입 검수에서 {_SUBJECT[key]} 매입 기준 상한 이하로 확인될 것"
                )
            elif key == "mixture":
                mixed = ", ".join(str(c) for c in stream.get("contaminants") or [] if c)
                limit = self.use(criterion.get("maxContaminationPct"))
                tail = f" 매입 기준의 오염도 상한은 {limit}입니다." if limit else ""
                out.append(
                    f"혼입물({mixed or '다른 재질'})은 분리해 내보내거나 검수 기준 안일 것.{tail}"
                )
            elif key == "monthly_kg_basis":
                out.append("월 발생량이 추정값이므로 실제 계량한 물량으로 정산할 것")
            elif key == "form":
                out.append(f"형태 확인: {check.get('text')}")
            elif check.get("text"):
                out.append(str(check["text"]))
        return out

    def terms(
        self,
        kind: str,
        stream_ids: list[str],
        acceptable: list[str],
        excluded: list[dict[str, str]],
        later: list[str],
    ) -> dict[str, Any]:
        lines: list[str] = []
        items: list[dict[str, str]] = []
        conditions: list[str] = []
        for sid in stream_ids:
            route = self.result("judge_route", streamId=sid) or {}
            price_id = (route.get("claims") or {}).get("unitPrice")
            price = self.use(price_id)
            if not price:
                continue
            items.append({"streamId": sid, "unitPriceRef": str(price_id)})
            value = (self.pool.get(str(price_id)) or {}).get("value")
            paid = isinstance(value, (int, float)) and value < 0
            # 값은 음수 그대로 두고 문장으로 구분한다(claim의 role이 treatmentFee다)
            note = "처리비는" if paid else "매입 단가는"
            if kind == "offer":
                lines.append(f"- {self.name(sid)}: 매입 기준을 충족합니다. {note} {price}입니다.")
            else:
                found = self._conditions(sid)
                conditions += [f"{self.name(sid)}: {c}" for c in found]
                lines.append(
                    f"- {self.name(sid)}: 아래 조건이 확인되면 받습니다. {note} {price}입니다."
                )
                lines += [f"  · {c}" for c in found]
        loading = self.result("check_loading", streamIds=acceptable) or {}
        load_claims = loading.get("claims") or {}
        checks = {c.get("key"): c.get("status") for c in loading.get("checks") or []}
        days = list((self.b.get("guardrails") or {}).get("days") or [])
        tail_lines = []
        pickups_id = load_claims.get("pickupsPerMonth")
        pickups = self.use(pickups_id)
        if days and pickups:
            tail_lines.append(
                f"수거는 {'·'.join(days)}요일 가운데 정하고 수거 횟수는 {pickups}입니다."
            )
        elif days:
            tail_lines.append(f"수거는 {'·'.join(days)}요일 가운데 정합니다.")
        total_id = load_claims.get("totalKg")
        total, minimum = self.use(total_id), self.use(load_claims.get("minMonthlyKg"))
        if total and minimum:
            if checks.get("min_volume") == "met":
                tail_lines.append(
                    f"받을 수 있는 폐기물을 합친 물량 {total}{self.after(total_id, '은는')} "
                    f"최소 물량 {minimum} 이상입니다."
                )
            else:
                tail_lines.append(
                    f"받을 수 있는 폐기물을 합친 물량 {total}{self.after(total_id, '이가')} "
                    f"최소 물량 {minimum}에 못 미쳐, "
                    "기존 노선에 묶어 수거하는 조건입니다."
                )
                conditions.append("최소 물량에 못 미쳐 기존 노선에 묶어 수거할 것")
        if checks.get("loading") not in (None, "met"):
            tail_lines.append(
                "한 번 수거량이 차량 용량을 넘어, 수거 횟수를 늘려 나눠 싣는 조건입니다."
            )
            conditions.append("한 번 수거량이 차량 용량을 넘어 수거 횟수를 늘려 나눠 실을 것")
        if checks.get("loading_volume") not in (None, "met"):
            tail_lines.append("부피 적재는 겉보기 밀도를 확인한 뒤에 정하는 조건입니다.")
            conditions.append("겉보기 밀도를 확인해 부피 적재가 가능한지 반입 전에 확인할 것")
        if kind == "offer" and conditions:
            # 노선 묶음처럼 조건이 붙는 답은 그대로 받을 수 있는 제안(offer)이 아니다
            kind = "conditional"
        head = (
            "다음 폐기물은 지금 조건으로 받을 수 있습니다."
            if kind == "offer"
            else "다음 폐기물은 조건부로 받을 수 있습니다."
        )
        first = not ((self.b.get("state") or {}).get("offered"))
        for item in excluded if first else []:  # 받지 않는 흐름은 첫 조건 메시지에서 한 번만 알린다
            tail_lines.append(f"받지 않는 폐기물: {self.name(item['streamId'])}. {item['reason']}")
        if later:
            names = ", ".join(self.name(sid) for sid in later)
            tail_lines.append(f"조건을 더 확인해야 하는 폐기물은 따로 답하겠습니다: {names}.")
        output = {
            "type": kind,
            "stage": 5,
            "streamIds": [item["streamId"] for item in items],
            "text": "\n".join([head, *lines, *tail_lines]),
            "claimRefs": self.refs,
            "terms": {"items": items, "days": days, "conditions": conditions},
            "excluded": excluded,
        }
        if pickups:
            output["terms"]["pickupsPerMonthRef"] = str(pickups_id)
        return output
