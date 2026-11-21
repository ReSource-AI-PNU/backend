"""Loopback-only in-memory BFF for local mentoring QA; no operational data or models.

Frontend environment is documented in docs/v3/verification-2026-11-21.md.
Run from the backend root: python scripts/serve_local_verification.py
"""

import uvicorn

from app.core.config import settings
from app.main import app
from app.v3 import api, service
from app.v3.account_store import InMemoryAccounts
from app.v3.evidence.registry import InMemoryRegistry
from app.v3.store import InMemoryStore

settings.database_url = ""
settings.v3_auth = "bff"
settings.v3_llm = "rule"
settings.v3_distance = "none"
settings.v3_bff_key = "local-evidence-verification-only-20261121"
state = service.App(
    InMemoryStore(),
    InMemoryRegistry(),
    auth_mode="bff",
    accounts=InMemoryAccounts(),
    today="2026-11-21",
)
service.seed(state)
api.set_state(state)
uvicorn.run(app, host="127.0.0.1", port=5800, log_level="warning")
