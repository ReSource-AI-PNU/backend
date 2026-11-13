"""가상 PP 상태를 이용하는 수동 Azure 질문 선택 검증."""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.agents.pp_question_selector import select_questions
from app.agents.pp_questions import InputField, ReviewState


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    state = ReviewState(material=InputField(status="value", value="PP"))
    result = select_questions(
        state,
        endpoint=os.getenv("OPENAI_ENDPOINT"),
        deployment=os.getenv("CHAT_DEPLOYMENT"),
        key=os.getenv("FOUNDRY_KEY"),
        on_diagnostic=lambda diagnostic: print(
            json.dumps({"diagnostic": diagnostic.model_dump()}, ensure_ascii=False), file=sys.stderr
        ),
    )
    print(
        json.dumps(
            {
                "status": result.evaluation.status,
                "source": result.source,
                "fallback_reason": result.fallback_reason,
                "questions": [s.model_dump() for s in result.selected],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
