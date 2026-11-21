"""Bounded real Azure check using the application's Chat Completions adapter.

No deployment, resource mutation, database access, business documents, or secret
output. CLI authentication verifies this user's access, not an App Service identity.
"""

import argparse
import json
import os
import subprocess
from urllib.parse import urlsplit

from app.v3.agents.llm import FoundryChat, LLMError, urllib_transport


def azure_endpoint(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.hostname.endswith((".openai.azure.com", ".cognitiveservices.azure.com"))
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or url.port not in (None, 443)
    ):
        raise ValueError("Azure OpenAI resource HTTPS root endpoint required")
    return value.rstrip("/")


def probe(env, *, auth="api-key", az="az", transport=urllib_transport):
    endpoint = azure_endpoint(env.get("FOUNDRY_ENDPOINT", ""))
    deployment = env.get("FOUNDRY_DEPLOYMENT", "").strip()
    if not deployment or any(c in deployment for c in "/?#\\"):
        raise ValueError("FOUNDRY_DEPLOYMENT required")
    key = env.get("FOUNDRY_API_KEY", "").strip()
    if auth == "azure-cli":
        # stdout is held in memory and never logged. No token file is created here.
        result = subprocess.run(
            [
                az,
                "account",
                "get-access-token",
                "--resource",
                "https://cognitiveservices.azure.com",
                "--query",
                "accessToken",
                "--output",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        token = result.stdout.strip()
        if result.returncode or not token:
            raise ValueError("Azure CLI login or token acquisition failed")

        def authenticated(method, url, headers, body, timeout):
            return transport(
                method,
                url,
                {
                    **{k: v for k, v in headers.items() if k.lower() != "api-key"},
                    "Authorization": f"Bearer {token}",
                },
                body,
                timeout,
            )

        wire = authenticated
    elif auth == "api-key" and key:
        wire = transport
    else:
        raise ValueError("FOUNDRY_API_KEY required")
    client = FoundryChat(
        endpoint,
        deployment,
        key,
        transport=wire,
        timeout=30,
        retries=0,
        max_completion_tokens=1024,
        reasoning_effort=None if env.get("FOUNDRY_REASONING_EFFORT") == "none" else "low",
    )
    response = client.complete(
        [{"role": "user", "content": "Connection test only. Return connected=true."}],
        response_schema={
            "type": "object",
            "properties": {"connected": {"type": "boolean"}},
            "required": ["connected"],
            "additionalProperties": False,
        },
    )
    if json.loads(response.get("content") or "null") != {"connected": True}:
        raise ValueError("Unexpected model response")
    return {
        "connected": True,
        "auth": auth,
        "adapter": "FoundryChat",
        "apiVersion": "2024-10-21",
        "maxCompletionTokens": 1024,
        "timeoutSeconds": 30,
        "deploymentChanged": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth", choices=("api-key", "azure-cli"), default="api-key")
    parser.add_argument("--az", default="az", help="Path to locally installed az executable")
    args = parser.parse_args()
    try:
        print(json.dumps(probe(os.environ, auth=args.auth, az=args.az), ensure_ascii=False))
        return 0
    except LLMError as error:
        print(json.dumps({"connected": False, "stage": "inference", "httpStatus": error.status}))
    except Exception:  # noqa: BLE001 - do not leak secrets from third-party exceptions
        # Exceptions may contain credentials or remote payloads: deliberately omit text.
        print(json.dumps({"connected": False, "stage": "configuration_authentication_or_response"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
