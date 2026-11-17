"""모델 선택(llm_factory)을 만드는 한곳. FastAPI 경로(api.py)와 로컬 서버(devserver.py)가 같이 쓴다.

두 서버가 같은 환경 변수로 같은 모델을 고르게 하려고 한곳에 둔다.

| 환경 변수 | 뜻 |
|---|---|
| V3_LLM | rule(기본, 규칙 정책·모델 미연결) \\| foundry \\| anthropic |
| FOUNDRY_ENDPOINT, FOUNDRY_DEPLOYMENT, FOUNDRY_API_KEY | V3_LLM=foundry일 때 셋 다 있어야 한다 |
| ANTHROPIC_API_KEY | V3_LLM=anthropic일 때의 키 |
| ANTHROPIC_API_KEY_FILE | 키를 담은 파일 경로(ANTHROPIC_API_KEY가 비어 있을 때만 읽는다) |
| ANTHROPIC_MODEL | 기본 claude-sonnet-5-5 |
| OLLAMA_BASE_URL | 업체가 ollama를 골랐을 때 그 업체의 모델 주소 |
| V3_LLM_ALL_VENDORS | 1이면 ollama를 고른 업체도(주소가 없을 때) anthropic 모델이 답한다 |

- 돌려주는 값은 llm_factory(role, org_id, model) -> LLM | None 이다. None이면 service가 규칙 정책으로 돈다.
- 업체가 ollama를 골랐는데 주소가 없으면 다른 모델로 바꿔 부르지 않고 None을 준다(쓰지 않은 모델을
  쓴 것처럼 보이지 않게). V3_LLM_ALL_VENDORS=1은 anthropic에서만 이 규칙을 푼다. 그때도 화면에는
  설정한 모델과 실제로 답한 모델이 따로 나온다.
- 설정이 빠졌을 때: strict면 LlmConfigError로 멈추고(로컬 서버), 아니면 경고를 남기고 규칙 정책으로
  돈다(배포 경로. 서버는 뜨고, 관리자 화면과 대화에 "규칙 정책(모델 미연결)"로 나온다).
- 키 값은 예외 문구와 기록에 싣지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.v3.agents.llm import AnthropicChat, FoundryChat, OllamaChat

logger = logging.getLogger(__name__)

LLM_MODES = ("rule", "foundry", "anthropic")
FOUNDRY_ENV = ("FOUNDRY_ENDPOINT", "FOUNDRY_DEPLOYMENT", "FOUNDRY_API_KEY")
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5-5"
MODE_LABELS = {
    "rule": "규칙 정책(모델 미연결)",
    "foundry": "Foundry",
    "anthropic": "Anthropic(Claude)",
}

LlmFactory = Callable[[str, str, dict[str, Any] | None], Any]


class LlmConfigError(ValueError):
    """모델 설정이 틀렸거나 빠졌다. 문구에는 어떤 값이 없는지만 싣고 값은 싣지 않는다."""


def _value(env: Mapping[str, Any], name: str) -> str:
    return str(env.get(name) or "").strip()


def normalize_mode(mode: str | None) -> str:
    chosen = (mode or "rule").strip().lower()
    if chosen not in LLM_MODES:
        raise LlmConfigError(f"V3_LLM은 {' | '.join(LLM_MODES)} 가운데 하나여야 한다: {mode!r}")
    return chosen


def anthropic_key(env: Mapping[str, Any]) -> str:
    """ANTHROPIC_API_KEY, 없으면 ANTHROPIC_API_KEY_FILE이 가리키는 파일의 내용. 둘 다 없으면 빈 글."""
    key = _value(env, "ANTHROPIC_API_KEY")
    key_file = _value(env, "ANTHROPIC_API_KEY_FILE")
    if not key and key_file:
        try:
            key = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            raise LlmConfigError(
                "ANTHROPIC_API_KEY_FILE이 가리키는 파일을 읽을 수 없습니다."
            ) from None
    return key


def build_llm_factory(
    mode: str | None, env: Mapping[str, Any], *, strict: bool = False
) -> LlmFactory | None:
    """mode와 환경 변수로 llm_factory를 만든다. rule이면 None(환경 변수를 읽지 않는다).

    strict가 아니면 설정이 빠졌을 때 경고를 남기고 계속한다: 본 모델은 None(규칙 정책)이 되고,
    ollama를 고른 업체는 주소가 있으면 그대로 그 모델을 쓴다.
    """
    chosen = normalize_mode(mode)
    if chosen == "rule":
        return None
    ollama = _value(env, "OLLAMA_BASE_URL")
    all_vendors = False
    make_main: Callable[[], Any] | None = None

    if chosen == "foundry":
        missing = [name for name in FOUNDRY_ENV if not _value(env, name)]
        if missing and strict:
            raise LlmConfigError(f"V3_LLM=foundry에는 환경 변수가 필요합니다: {', '.join(missing)}")
        if missing:
            logger.warning(
                "V3_LLM=foundry인데 %s 설정이 비어 있어 규칙 정책으로 돈다", ", ".join(missing)
            )
        else:
            endpoint, deployment, key = (_value(env, name) for name in FOUNDRY_ENV)

            def make_main() -> Any:
                return FoundryChat(endpoint, deployment, key)

    else:  # anthropic
        try:
            key = anthropic_key(env)
        except LlmConfigError as error:
            if strict:
                raise
            logger.warning("V3_LLM=anthropic: %s 규칙 정책으로 돈다", error)
            key = ""
        else:
            if not key and strict:
                raise LlmConfigError(
                    "V3_LLM=anthropic에는 ANTHROPIC_API_KEY나 ANTHROPIC_API_KEY_FILE이 필요합니다."
                )
            if not key:
                logger.warning("V3_LLM=anthropic인데 키 설정이 비어 있어 규칙 정책으로 돈다")
        if key:
            name = _value(env, "ANTHROPIC_MODEL") or ANTHROPIC_DEFAULT_MODEL
            all_vendors = _value(env, "V3_LLM_ALL_VENDORS") == "1"

            def make_main() -> Any:
                return AnthropicChat(key, name)

    def factory(role: str, org_id: str, model: dict[str, Any] | None = None) -> Any:
        picked = model or {}
        if role == "vendor" and picked.get("provider") == "ollama":
            if ollama and picked.get("name"):
                return OllamaChat(ollama, str(picked["name"]))
            if not all_vendors:
                logger.warning("%s: Ollama 주소나 모델 이름이 없어 규칙 정책으로 돈다", org_id)
                return None
        return make_main() if make_main is not None else None

    return factory
