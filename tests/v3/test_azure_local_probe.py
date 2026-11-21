"""Mock transport tests do not claim an Azure connection succeeded."""

import json
from types import SimpleNamespace

import pytest

from scripts.check_azure_local import azure_endpoint, probe


@pytest.mark.parametrize(
    "url",
    [
        "http://example.openai.azure.com",
        "https://evil.test",
        "https://example.openai.azure.com.evil.test",
        "https://secret@example.openai.azure.com",
        "https://example.openai.azure.com/openai",
    ],
)
def test_rejects_unintended_secret_destinations(url):
    with pytest.raises(ValueError):
        azure_endpoint(url)


def test_bounded_request_uses_real_adapter_without_returning_credentials(monkeypatch):
    monkeypatch.setattr(
        "scripts.check_azure_local.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="TEST_TOKEN"),
    )
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, json.loads(body), timeout))
        return (
            200,
            {},
            json.dumps(
                {
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": '{"connected":true}'}}
                    ]
                }
            ).encode(),
        )

    result = probe(
        {"FOUNDRY_ENDPOINT": "https://test.openai.azure.com", "FOUNDRY_DEPLOYMENT": "test-model"},
        auth="azure-cli",
        transport=transport,
    )
    assert len(calls) == 1
    _, url, headers, payload, timeout = calls[0]
    assert "api-version=2024-10-21" in url
    assert headers == {"Content-Type": "application/json", "Authorization": "Bearer TEST_TOKEN"}
    assert timeout == 30 and payload["max_completion_tokens"] == 1024
    assert result["connected"] and "TEST_TOKEN" not in json.dumps(result)
