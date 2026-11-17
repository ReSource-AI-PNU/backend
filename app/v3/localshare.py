"""로컬에서 프로세스 둘이 같은 저장 파일을 쓰게 하는 부품(개발·시연용).

배포에서는 API와 업체 측 에이전트 서비스가 같은 PostgreSQL을 본다. 로컬 서버(devserver.py)와
업체 측 에이전트 서버(vendor_devserver.py)는 그 대신 같은 JSON 파일 둘(저장소, 근거)을 본다.

- 회사 측 프로세스는 업체에 메시지를 보내는 동안 기다린다. 그래서 두 프로세스는 번갈아 가며 쓰고,
  동시에 쓰지 않는다. 파일 잠금은 하지 않는다(로컬 전용).
- ReloadingStore, ReloadingRegistry: 파일을 다시 읽는 reload()가 있는 JSON 파일 저장.
- SplitVendorGateway: 주소가 있는 업체는 HTTP로, 나머지는 같은 프로세스에서 부른다. HTTP로 다녀온
  뒤에는 근거 파일을 다시 읽는다(업체 측이 남긴 계산 근거를 회사 측이 다시 검증할 수 있게).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.v3 import service
from app.v3.agents.gateway import GatewayError, HttpVendorGateway
from app.v3.evidence.model import Evidence
from app.v3.evidence.registry import JsonFileRegistry
from app.v3.store import _TABLES, JsonFileStore


class ReloadingStore(JsonFileStore):
    """JsonFileStore에 reload()를 더했다. 다른 프로세스가 쓴 내용을 다시 읽는다."""

    def reload(self) -> bool:
        """파일이 있으면 읽어 들이고 True, 없으면 그대로 두고 False."""
        if not self.path.exists():
            return False
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        self._data = {table: loaded.get(table, {}) for table in _TABLES}
        return True


class ReloadingRegistry(JsonFileRegistry):
    """JsonFileRegistry에 reload()를 더했다. 다른 프로세스가 쓴 근거를 다시 읽는다."""

    def reload(self) -> bool:
        if not self.path.exists():
            return False
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        items: dict[str, Evidence] = {}
        for raw in loaded.get("evidence") or []:
            item = Evidence.from_dict(raw)
            items[item.id] = item
        self._items = items
        return True


def vendor_agents(raw: Any) -> dict[str, str]:
    """V3_VENDOR_AGENTS를 {업체 조직 ID: 주소}로 읽는다. JSON 글이나 객체. 모양이 틀리면 ValueError."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError("V3_VENDOR_AGENTS가 JSON이 아니다") from None
    if not isinstance(raw, Mapping):
        raise ValueError('V3_VENDOR_AGENTS는 {"업체 조직 ID": "주소"} 꼴의 객체여야 한다')  # noqa: TRY004
    found: dict[str, str] = {}
    for org_id, url in raw.items():
        org, base = str(org_id).strip(), str(url or "").strip()
        if not org or not base.startswith(("http://", "https://")):
            raise ValueError("V3_VENDOR_AGENTS의 값은 http(s) 주소여야 한다")
        found[org] = base
    return found


class SplitVendorGateway:
    """주소가 있는 업체는 HTTP로, 나머지는 같은 프로세스에서 부른다.

    urls의 "*"는 나머지 모든 업체다(그러면 같은 프로세스에서 부르는 업체가 없다).
    HTTP로 다녀온 뒤에는 reload의 것들을 다시 읽는다. calls에 업체별 호출 수를 남긴다.
    """

    def __init__(
        self,
        urls: Mapping[str, str],
        service_key: str,
        local: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]],
        *,
        reload: Iterable[Any] = (),
        transport: Any = None,
    ) -> None:
        self._urls = dict(urls)
        self._remote = HttpVendorGateway(self._urls, service_key, transport)
        self._local = local
        self._reload = tuple(reload)
        self.calls: dict[str, dict[str, int]] = {"remote": {}, "local": {}}

    def __repr__(self) -> str:
        return f"SplitVendorGateway(remote={sorted(self._urls)})"

    def is_remote(self, vendor_org: str) -> bool:
        return vendor_org in self._urls or "*" in self._urls

    def deliver(self, vendor_org: str, message: dict, context: dict) -> dict:
        kind = "remote" if self.is_remote(vendor_org) else "local"
        self.calls[kind][vendor_org] = self.calls[kind].get(vendor_org, 0) + 1
        if kind == "local":
            reply = self._local(vendor_org, message, context)
            if not isinstance(reply, dict):
                raise GatewayError("업체 측 에이전트의 답이 메시지 모양이 아니다")
            return reply
        try:
            return self._remote.deliver(vendor_org, message, context)
        finally:
            # 실패했어도 다시 읽는다: 업체 측이 답을 못 냈더라도 남긴 근거가 있을 수 있다
            for target in self._reload:
                target.reload()


def split_gateway(app: service.App, env: Mapping[str, str]) -> SplitVendorGateway | None:
    """환경 변수 V3_VENDOR_AGENTS, VENDOR_SERVICE_KEY로 게이트웨이를 만든다. 주소가 없으면 None.

    저장소와 근거가 reload()를 가진 파일 저장이어야 한다(업체 측 프로세스와 같은 파일을 본다).
    """
    urls = vendor_agents(env.get("V3_VENDOR_AGENTS"))
    if not urls:
        return None
    key = (env.get("VENDOR_SERVICE_KEY") or "").strip()
    if not key:
        raise ValueError("V3_VENDOR_AGENTS를 쓰려면 VENDOR_SERVICE_KEY가 있어야 한다")
    if not hasattr(app.registry, "reload") or not hasattr(app.store, "reload"):
        raise ValueError(
            "V3_VENDOR_AGENTS를 쓰려면 --store로 저장 파일을 정해야 한다"
            "(업체 측 프로세스와 같은 파일을 본다)"
        )

    def local(vendor_org: str, message: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return service.handle_vendor_message(app, vendor_org, message, context)

    return SplitVendorGateway(urls, key, local, reload=(app.registry,))
