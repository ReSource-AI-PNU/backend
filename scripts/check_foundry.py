"""Foundry Responses API 수동 연결 확인. backend 디렉터리에서 실행한다."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

PROMPT = "연결 확인입니다. 한국어로 확인 완료라고만 답하세요."


def main() -> int:
    try:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
        names = ("OPENAI_ENDPOINT", "CHAT_DEPLOYMENT", "FOUNDRY_KEY")
        settings = {name: os.environ.get(name, "").strip() for name in names}
        missing = [name for name in names if not settings[name]]
        if missing:
            print(f"설정 누락: {', '.join(missing)}", file=sys.stderr)
            return 1

        client = OpenAI(
            base_url=settings["OPENAI_ENDPOINT"],
            api_key=settings["FOUNDRY_KEY"],
            timeout=20.0,
            max_retries=0,
        )
        response = client.responses.create(model=settings["CHAT_DEPLOYMENT"], input=PROMPT)
        if response.status != "completed" or not response.output_text.strip():
            print("요청 실패: 응답이 완료되지 않았거나 텍스트가 없습니다.", file=sys.stderr)
            return 1
        print(response.output_text)
        return 0
    except APITimeoutError:
        print("요청 실패: 연결 시간이 초과되었습니다.", file=sys.stderr)
    except APIStatusError as exc:
        print(f"요청 실패: HTTP {exc.status_code} 응답을 받았습니다.", file=sys.stderr)
    except APIConnectionError:
        print("요청 실패: 서비스에 연결할 수 없습니다.", file=sys.stderr)
    except Exception:  # noqa: BLE001 - 예외에 포함된 비밀값을 출력하지 않는다.
        print("요청 실패: 설정 또는 응답을 확인할 수 없습니다.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
