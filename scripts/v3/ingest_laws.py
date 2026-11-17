"""국가법령정보센터 Open API(law.go.kr DRF)로 법령을 조 단위로 가져와 laws.json과 같은 형식으로 저장한다.

로컬 미실행(실제 호출): 이 스크립트로 law.go.kr을 부른 적이 없다. 파서는 공식 가이드의 출력 필드
이름으로 만든 예시 XML로만 확인했다(tests/v3/test_ingest_laws.py). 가이드에는 응답 본문 예시가
없고 필드 표만 있어, 요소가 어떻게 겹쳐 있는지에 기대지 않고 요소 이름과 문서 순서만 본다.
처음 실제로 돌릴 때는 --dry-run으로 조 수와 첫 조의 원문을 눈으로 확인한다.

공식 가이드 (2026-11-17 확인):
- 목록: https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=lsNwListGuide
  lawSearch.do?OC=&target=law&type=XML&query=  → 법령일련번호, 법령명한글, 법령ID, 시행일자, 현행연혁코드
- 본문: https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=lsNwInfoGuide
  lawService.do?OC=&target=law&type=XML&MST=   → 법령ID, 법령명_한글, 시행일자, 공포일자, 공포번호,
  조문번호, 조문가지번호, 조문여부, 조문제목, 조문시행일자, 조문내용, 항내용, 호내용, 목내용

    LAW_OC=<인증값> python -m scripts.v3.ingest_laws --out data/v3/laws.ingested.json [--dry-run]

규칙:
- 인증값은 환경 변수 LAW_OC로만 받는다. 없으면 안내하고 끝난다. 파일, 출력, 근거의 url에 넣지 않는다.
- 원문(조문내용)을 받지 못한 조는 넣지 않는다. 시행일을 알 수 없거나 아직 시행 전인 조도 넣지 않는다.
- 원문은 줄 앞뒤 공백만 떼고 글자 그대로 둔다(인용 대조가 부분 문자열로 이뤄진다).
- 결과는 시드 폴더가 아닌 곳에 쓴다. 사람이 확인한 뒤 app/v3/evidence/seeds/로 옮긴다
  (같은 ID가 있으면 나중 파일이 덮어쓰고 version이 오른다).
- 고시와 지침(행정규칙, target=admrul)은 가이드를 확인하지 못해 넣지 않았다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.v3 import ids

# transport(url) -> (status, body)
Transport = Callable[[str], tuple[int, bytes]]

BASE = "https://www.law.go.kr/DRF"
DEFAULT_LAWS = ("폐기물관리법", "폐기물관리법 시행규칙", "순환경제사회 전환 촉진법")
DEFAULT_OUT = "data/v3/laws.ingested.json"
CONTENT_TAGS = ("조문내용", "항내용", "호내용", "목내용")
ARTICLE_FIELDS = {
    "조문가지번호": "branch",
    "조문여부": "type",
    "조문제목": "title",
    "조문시행일자": "effective",
}
SEARCH_FIELDS = {
    "법령명한글": "name",
    "법령ID": "lawId",
    "시행일자": "effective",
    "현행연혁코드": "status",
}


class IngestError(RuntimeError):
    """법령을 가져오지 못했다."""


def urllib_transport(url: str) -> tuple[int, bytes]:
    if not url.startswith("https://www.law.go.kr/"):
        raise IngestError("law.go.kr만 호출한다")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise IngestError(f"law.go.kr에 연결하지 못했다: {type(error).__name__}") from error


def search_url(oc: str, name: str) -> str:
    query = {"OC": oc, "target": "law", "type": "XML", "query": name, "display": "100"}
    return f"{BASE}/lawSearch.do?{urllib.parse.urlencode(query)}"


def service_url(oc: str, mst: str) -> str:
    query = {"OC": oc, "target": "law", "type": "XML", "MST": mst}
    return f"{BASE}/lawService.do?{urllib.parse.urlencode(query)}"


def public_url(name: str) -> str:
    """근거에 남기는 주소. 인증값이 들어가지 않는 공개 본문 주소다."""
    return f"https://www.law.go.kr/%EB%B2%95%EB%A0%B9/{urllib.parse.quote(name)}"


def iso_date(raw: Any) -> str | None:
    """20261112 -> 2026-11-12. 날짜가 아니면 None."""
    text = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(text) != 8:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _text(element: ET.Element) -> str:
    return "".join(element.itertext())


def clean_lines(raw: str) -> list[str]:
    """줄 앞뒤 공백만 뗀다. 글자는 바꾸지 않는다."""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _root(xml: bytes | str) -> ET.Element:
    try:
        return ET.fromstring(xml)
    except ET.ParseError as error:
        raise IngestError(f"응답이 XML이 아니다 ({error})") from None


def parse_search(xml: bytes | str) -> list[dict[str, str]]:
    """목록 응답에서 법령마다 {mst, name, lawId, effective, status}를 뽑는다.

    요소가 어떻게 겹쳐 있든, 법령일련번호가 나오면 새 법령이 시작한다고 본다.
    """
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for element in _root(xml).iter():
        tag = str(element.tag)
        if tag == "법령일련번호":
            current = {"mst": _text(element).strip()}
            items.append(current)
        elif current is not None and tag in SEARCH_FIELDS:
            current.setdefault(SEARCH_FIELDS[tag], _text(element).strip())
    return [item for item in items if item.get("mst") and item.get("name")]


def pick_current(items: list[dict[str, str]], name: str) -> dict[str, str]:
    """이름이 글자 그대로 같은 현행 법령 하나. 없거나 여럿이면 IngestError."""
    exact = [item for item in items if item["name"] == name]
    current = [item for item in exact if item.get("status", "현행") == "현행"]
    if len(current) != 1:
        raise IngestError(
            f"'{name}'과 이름이 같은 현행 법령을 하나로 정하지 못했다({len(current)}건)"
        )
    return current[0]


def parse_law(xml: bytes | str) -> dict[str, Any]:
    """본문 응답을 {name, lawId, effective, promulgated, number, articles}로 바꾼다.

    조문번호가 나오면 새 조가 시작한다고 보고, 다음 조문번호가 나올 때까지의 조문내용·항내용·
    호내용·목내용을 문서 순서대로 그 조의 줄로 모은다. 조문여부가 "조문"이 아닌 것(편·장·절의
    제목인 "전문")은 버린다.
    """
    info: dict[str, str] = {}
    articles: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    wanted = {
        "법령ID": "lawId",
        "법령명_한글": "name",
        "시행일자": "effective",
        "공포일자": "promulgated",
        "공포번호": "number",
    }
    for element in _root(xml).iter():
        tag = str(element.tag)
        if tag == "조문번호":
            current = {"number": _text(element).strip(), "lines": []}
            articles.append(current)
        elif current is None:
            if tag in wanted:
                info.setdefault(wanted[tag], _text(element).strip())
        elif tag in ARTICLE_FIELDS:
            current.setdefault(ARTICLE_FIELDS[tag], _text(element).strip())
        elif tag in CONTENT_TAGS:
            current["lines"].extend(clean_lines(_text(element)))
    return {**info, "articles": articles}


def article_label(article: dict[str, Any]) -> str | None:
    number, branch = article.get("number") or "", article.get("branch") or ""
    if not number.isdigit() or int(number) <= 0:
        return None
    label = f"제{int(number)}조"
    if branch.isdigit() and int(branch) > 0:
        label += f"의{int(branch)}"
    return label


def to_evidence(
    law: dict[str, Any], mst: str, *, today: str, retrieved_at: str, only: set[str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """(근거 목록, 넣지 않은 조와 이유). 근거는 Evidence.to_dict() 모양이다."""
    name = (law.get("name") or "").strip()
    if not name:
        raise IngestError("응답에 법령 이름(법령명_한글)이 없다")
    law_effective = iso_date(law.get("effective"))
    promulgated = iso_date(law.get("promulgated"))
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    for article in law.get("articles") or []:
        if (article.get("type") or "조문") != "조문":
            continue  # 편·장·절 제목
        label = article_label(article)
        if label is None:
            skipped.append(
                {"article": str(article.get("number")), "reason": "조 번호를 알 수 없음"}
            )
            continue
        if only is not None and label not in only:
            continue
        lines = article.get("lines") or []
        if not lines:
            skipped.append({"article": label, "reason": "원문 없음"})
            continue
        effective = iso_date(article.get("effective")) or law_effective
        if effective is None:
            skipped.append({"article": label, "reason": "시행일을 알 수 없음"})
            continue
        if effective > today:
            skipped.append({"article": label, "reason": f"시행 전({effective})"})
            continue
        evidence_id = ids.law_id(name, label, effective)
        if evidence_id in seen:
            skipped.append({"article": label, "reason": "같은 조가 두 번 나옴"})
            continue
        seen.add(evidence_id)
        title = (article.get("title") or "").strip()
        items.append(
            {
                "id": evidence_id,
                "kind": "law",
                "label": f"{name} {label}({title})" if title else f"{name} {label}",
                "value": None,
                "unit": None,
                "excerpt": "\n".join(lines),
                "source": f"국가법령정보센터 · {name} · {label}",
                "asOf": effective,
                "validUntil": None,
                "access": ["*"],
                "version": 1,
                "meta": {
                    "lawName": name,
                    "title": title,
                    "effective": effective,
                    "url": public_url(name),
                    "lawId": law.get("lawId") or None,
                    "lsiSeq": str(mst),
                    "promulgated": promulgated,
                    "promulgationNumber": law.get("number") or None,
                    "retrievedAt": retrieved_at,
                    "retrievedBy": "law.go.kr DRF lawService (target=law, type=XML)",
                    "article": label,
                },
            }
        )
    return sorted(items, key=lambda item: item["id"]), skipped


def fetch(transport: Transport, url: str, what: str) -> bytes:
    status, body = transport(url)
    if status != 200:
        raise IngestError(f"{what} 호출 실패 (HTTP {status})")
    return body


def ingest(
    names: list[str],
    oc: str,
    *,
    transport: Transport,
    today: str,
    retrieved_at: str,
    only: Mapping[str, set[str]] | None = None,
) -> dict[str, Any]:
    """법령마다 목록에서 일련번호를 찾고 본문을 가져와 근거로 바꾼다."""
    evidence: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    for name in names:
        entry: dict[str, Any] = {"law": name, "articles": 0, "skipped": [], "error": None}
        report.append(entry)
        try:
            found = pick_current(parse_search(fetch(transport, search_url(oc, name), "목록")), name)
            law = parse_law(fetch(transport, service_url(oc, found["mst"]), "본문"))
            if (law.get("name") or "").strip() != name:
                raise IngestError(f"본문의 법령 이름이 다르다: {law.get('name')!r}")
            items, skipped = to_evidence(
                law,
                found["mst"],
                today=today,
                retrieved_at=retrieved_at,
                only=(only or {}).get(name),
            )
            if not items:
                raise IngestError("조문을 하나도 받지 못했다(인증값이나 응답 형식을 확인한다)")
        except IngestError as error:
            entry["error"] = str(error).replace(oc, "***")
            continue
        entry.update(articles=len(items), skipped=skipped, mst=found["mst"])
        evidence.extend(items)
    return {"evidence": sorted(evidence, key=lambda item: item["id"]), "report": report}


def parse_only(values: list[str]) -> dict[str, set[str]]:
    """--only "폐기물관리법=제17조,제18조" 를 {법령: {조}}로."""
    out: dict[str, set[str]] = {}
    for value in values:
        name, separator, articles = value.partition("=")
        wanted = {a.strip() for a in articles.split(",") if a.strip()}
        if not separator or not name.strip() or not wanted:
            raise ValueError(f"--only는 '법령 이름=제17조,제18조' 꼴이다: {value!r}")
        out.setdefault(name.strip(), set()).update(wanted)
    return out


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    transport: Transport | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="국가법령정보센터 Open API로 법령을 조 단위로 가져온다"
    )
    parser.add_argument(
        "--law", action="append", default=None, help="법령 이름(여러 번 줄 수 있다)"
    )
    parser.add_argument("--only", action="append", default=[], help="'법령 이름=제17조,제18조'")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"저장할 파일(기본 {DEFAULT_OUT})")
    parser.add_argument("--dry-run", action="store_true", help="가져와 세기만 하고 저장하지 않는다")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    oc = (env.get("LAW_OC") or "").strip()
    if not oc:
        print(
            "LAW_OC(국가법령정보센터 Open API 인증값)가 없다. open.law.go.kr에서 신청한 값을 "
            "환경 변수로 준다. 아무것도 가져오지 않았다.",
            file=sys.stderr,
        )
        return 2
    try:
        only = parse_only(args.only)
    except ValueError as error:
        print(f"입력 오류: {error}", file=sys.stderr)
        return 2
    moment = (now or (lambda: datetime.now(UTC)))()
    result = ingest(
        list(dict.fromkeys(args.law or DEFAULT_LAWS)),
        oc,
        transport=transport or urllib_transport,
        today=moment.date().isoformat(),
        retrieved_at=moment.isoformat(timespec="seconds"),
        only=only,
    )
    failed = False
    for entry in result["report"]:
        if entry["error"]:
            failed = True
            print(f"{entry['law']}: 가져오지 못했다 - {entry['error']}", file=sys.stderr)
            continue
        print(f"{entry['law']}: {entry['articles']}개 조, 넣지 않은 조 {len(entry['skipped'])}개")
        for skipped in entry["skipped"]:
            print(f"  넣지 않음 {skipped['article']}: {skipped['reason']}")
    if args.dry_run:
        first = result["evidence"][:1]
        if first:
            print(f"첫 조 {first[0]['id']}:\n{first[0]['excerpt']}")
        print("저장하지 않았다(--dry-run).")
    elif result["evidence"]:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result["evidence"], ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"{len(result['evidence'])}건을 {path}에 썼다. 확인한 뒤 시드 폴더로 옮긴다.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
