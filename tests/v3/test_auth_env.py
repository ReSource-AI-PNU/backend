"""로컬 서버의 조직 식별을 환경 변수로 켠다(V3_AUTH, V3_ORG_TOKENS, V3_ORG_TOKENS_FILE).

FastAPI 경로와 같은 이름을 쓴다. 아래 토큰은 테스트용으로 지어낸 글이다(실제 값이 아니다).
"""

import json

import pytest

from app.v3 import devserver

HANBIT, SAEGIL = "org_hanbit", "org_saegil"
TOKENS = {HANBIT: "test-hanbit-" + "a" * 12, SAEGIL: "test-saegil-" + "c" * 12}


def bearer(org):
    return {"Authorization": f"Bearer {TOKENS[org]}"}


def test_env_turns_token_mode_on_without_options(tmp_path):
    env = {"V3_AUTH": "token", "V3_ORG_TOKENS": json.dumps(TOKENS)}
    auth = devserver.load_auth(None, None, env)
    assert auth.mode == "token" and auth.viewer(bearer(SAEGIL)) == SAEGIL
    # X-Org-Id는 보지 않는다
    assert auth.viewer({**bearer(HANBIT), "X-Org-Id": SAEGIL}) == HANBIT
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(TOKENS), encoding="utf-8")
    from_file = devserver.load_auth(
        None, None, {"V3_AUTH": "token", "V3_ORG_TOKENS_FILE": str(path)}
    )
    assert from_file.tokens == auth.tokens


def test_option_wins_over_env_and_default_stays_header(tmp_path):
    assert devserver.load_auth(None, None, {}).mode == "header"
    assert devserver.load_auth(None, None).mode == "header"
    env = {"V3_AUTH": "token", "V3_ORG_TOKENS": json.dumps(TOKENS)}
    assert devserver.load_auth("header", None, env).mode == "header"  # 옵션이 먼저다
    other = {HANBIT: "other-hanbit-" + "z" * 12}
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(other), encoding="utf-8")
    auth = devserver.load_auth("token", str(path), env)  # --org-tokens 파일이 환경 변수보다 먼저다
    assert dict(auth.tokens) == other


def test_wrong_env_stops_without_printing_tokens(tmp_path):
    cases = (
        {"V3_AUTH": "token"},
        {"V3_AUTH": "token", "V3_ORG_TOKENS": "{"},
        {"V3_AUTH": "token", "V3_ORG_TOKENS": json.dumps({HANBIT: "short"})},
        {"V3_AUTH": "token", "V3_ORG_TOKENS_FILE": str(tmp_path / "없음.json")},
        {"V3_AUTH": "entra", "V3_ORG_TOKENS": json.dumps(TOKENS)},
    )
    for env in cases:
        with pytest.raises(SystemExit) as caught:
            devserver.load_auth(None, None, env)
        assert TOKENS[HANBIT] not in str(caught.value) and str(caught.value)


def test_cors_origins_are_added_not_replaced():
    assert devserver.cors_origins(None) == devserver.ALLOWED_ORIGINS
    added = devserver.cors_origins("http://localhost:3100/, https://app.example.com ,")
    assert added[: len(devserver.ALLOWED_ORIGINS)] == tuple(devserver.ALLOWED_ORIGINS)
    assert added[-2:] == ("http://localhost:3100", "https://app.example.com")
    for bad in ("*", "localhost:3100", "http://a.example/path", "javascript://x"):
        with pytest.raises(SystemExit):
            devserver.cors_origins(bad)
