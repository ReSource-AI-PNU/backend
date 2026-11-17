"""Configurable JSON/CSV/XML source collector. No data is trusted or published automatically."""

import argparse
import csv
import hashlib
import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.platform.db import SourceRow, engine
from scripts.import_public_data import KINDS

REQUIRED = {
    "prices": {"material", "price", "unit", "effective_date"},
    "permits": {"company_name", "permit_item", "permit_number"},
    "emissions": {"material", "factor", "unit", "boundary"},
    "laws": {"title", "article", "text", "effective_date"},
    "tenants": {"company_name", "complex_id", "address"},
}


class SourceConfig(BaseModel):
    kind: str
    source_url: str
    endpoint_env: str
    license: str = Field(min_length=1)
    format: str = "json"
    records_path: str = "data"
    id_field: str = "id"
    fields: dict[str, str]
    params: dict[str, str] = Field(default_factory=dict)
    secret_params: dict[str, str] = Field(default_factory=dict)
    page_param: str | None = None
    page_size_param: str = "perPage"
    page_size: int = Field(default=100, ge=1, le=1000)
    max_pages: int = Field(default=100, ge=1, le=1000)


def field(value, path):
    for name in path.split("."):
        if not isinstance(value, dict) or name not in value:
            raise ValueError(f"원문 필드 누락: {path}")
        value = value[name]
    return value


def records(content, config):
    if config.format == "json":
        root = json.loads(content)
        result = field(root, config.records_path) if config.records_path else root
    elif config.format == "csv":
        result = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    elif config.format == "xml":
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            raise ValueError("DTD·외부 엔터티 XML은 지원하지 않습니다")
        root = ElementTree.fromstring(content)
        result = [
            {child.tag: child.text or "" for child in item}
            for item in root.findall(config.records_path)
        ]
    else:
        raise ValueError("json/csv/xml 중 수집 형식을 지정하세요")
    if not isinstance(result, list) or any(not isinstance(r, dict) for r in result):
        raise ValueError("원문 레코드는 객체 배열이어야 합니다")
    return result


def collect(config, client=None):
    if config.kind not in KINDS or not REQUIRED[config.kind] <= config.fields.keys():
        raise ValueError("자료 종류와 필수 정규화 필드 매핑을 확인하세요")
    url = os.environ.get(config.endpoint_env, "")
    for candidate in [url, config.source_url]:
        p = urlparse(candidate)
        if p.scheme != "https" or not p.hostname or p.username or p.fragment:
            raise ValueError("HTTPS 수집 주소와 공개 원문 주소가 필요합니다")
    if urlparse(config.source_url).query:
        raise ValueError("공개 원문 주소에는 인증정보가 포함될 수 있는 쿼리를 넣지 마세요")
    params = dict(config.params)
    for param, env in config.secret_params.items():
        if not os.environ.get(env):
            raise ValueError(f"필요한 인증 환경변수가 없습니다: {env}")
        params[param] = os.environ[env]
    if client is None:
        with httpx.Client(timeout=30, follow_redirects=False) as connection:
            return collect(config, connection)
    output, seen = [], set()
    for page in range(1, config.max_pages + 1):
        if config.page_param:
            params.update(
                {config.page_param: str(page), config.page_size_param: str(config.page_size)}
            )
        try:
            with client.stream("GET", url, params=params) as response:
                if response.status_code != 200:
                    raise ValueError(f"공공데이터 응답 오류({response.status_code})")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 5_000_000:
                        raise ValueError("페이지 최대 수집 크기 초과")
        except httpx.HTTPError as exc:
            raise ValueError("공공데이터 연결 실패 · 부분 적재하지 않았습니다") from exc
        batch = records(bytes(content), config)
        for raw in batch:
            record_id = str(field(raw, config.id_field)).strip()
            if not record_id or record_id in seen:
                raise ValueError("원문 ID 누락·중복 또는 페이지 반복")
            seen.add(record_id)
            record = {key: field(raw, path) for key, path in config.fields.items()}
            if any(
                record[k] is None or str(record[k]).strip() == "" for k in REQUIRED[config.kind]
            ):
                raise ValueError("정규화 필수 값이 비었습니다")
            if config.kind in {"prices", "emissions"}:
                import math

                k = "price" if config.kind == "prices" else "factor"
                number = float(record[k])
                if not math.isfinite(number) or (k == "factor" and number < 0):
                    raise ValueError("유한한 단가·배출계수가 필요합니다")
                record[k] = number
            identity = f"{config.kind}:{config.source_url}:{record_id}"
            output.append(
                SourceRow(
                    id=hashlib.sha256(identity.encode()).hexdigest(),
                    kind=config.kind,
                    payload={
                        "source_url": config.source_url,
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "license": config.license,
                        "record_id": record_id,
                        "record": record,
                        "verified": False,
                    },
                )
            )
        if not config.page_param or len(batch) < config.page_size:
            return output
    raise ValueError("최대 페이지에 도달했습니다. 페이지 설정 확인 후 다시 수집하세요")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--import-db", action="store_true")
    args = parser.parse_args()
    config = SourceConfig.model_validate_json(args.config.read_text("utf-8"))
    rows = collect(config)
    if args.import_db:
        with Session(engine()) as db:
            for row in rows:
                db.merge(row)
            db.commit()
    print(
        f"{len(rows)}건 {'적재' if args.import_db else '검증'} 완료 · 자료 검증 전 · 판정에 자동 반영하지 않음"
    )
