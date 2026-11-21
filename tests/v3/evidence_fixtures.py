"""Explicit synthetic inputs for numerical history tests; never production evidence.

Original-check status is mocked only to exercise immutable snapshots. These values
do not establish that the official source is applicable to a real transaction.
"""

from dataclasses import replace

import pytest

from app.v3 import service
from tests.v3.runtime_fixtures import HANBIT, MISO
from tests.v3.runtime_fixtures import make_app as seeded_app


@pytest.fixture(autouse=True)
def simulated_legacy_factor_gate(monkeypatch):
    """Only imported history-test modules simulate old accepted coefficients.

    This deliberately replaces source admission, not snapshot arithmetic. The
    production gate rejects testOnly, separately tested in test_no_unsupported_defaults.
    pytest restores the gate after each test; nothing is installed in app code.
    """
    from app.v3.judge import basis

    original = basis.usable_basis
    monkeypatch.setattr(
        basis, "usable_basis", lambda e: bool(e.meta.get("testOnly")) or original(e)
    )


def numerical_history_app(**options):
    app = seeded_app(**options)
    for factor in app.registry.list(org_id=None):
        if factor.unit == "kgCO2e/kg":
            app.registry.put(
                replace(
                    factor,
                    source="가상 시험 계수 (원문 확인 모의)",
                    meta={
                        **factor.meta,
                        "verificationStatus": "original_checked",
                        "metric": "process_ghg",
                        "datasetVersion": "synthetic-history-v1",
                        "functionalUnit": "kg waste input",
                        "systemBoundary": "synthetic treatment only",
                        "country": "TEST",
                        "material": "test plastic",
                        "evidenceClass": "assumption",
                        "testOnly": True,
                    },
                )
            )
    # This older scenario formerly relied on an implicit 250 KRW/kg baseline.
    service.update_stream(
        app,
        HANBIT,
        "st_film",
        {"currentTreatment": {"method": "소각", "unitCostKrwPerKg": 250}},
        HANBIT,
    )
    return app


def negotiation_app(**options):
    app = seeded_app(**options)
    # Explicit fixture decision: tests the approved-policy path, not a production default.
    from app.v3.judge import defaults

    policy = app.registry.get(defaults.default_id("negotiation.askPct"))
    app.registry.put(
        replace(
            policy,
            meta={
                **policy.meta,
                "policyApproval": {
                    "status": "approved",
                    "approvedBy": "TEST ONLY operator",
                    "reason": "TEST negotiation arithmetic",
                    "source": "tests/v3/evidence_fixtures.py (synthetic decision)",
                    "approvedOn": "2026-11-17",
                },
            },
        )
    )
    for sid in ("st_film", "st_purge"):
        service.update_stream(
            app,
            HANBIT,
            sid,
            {"currentTreatment": {"method": "소각", "unitCostKrwPerKg": 250}},
            HANBIT,
        )
    service.update_vendor(
        app,
        MISO,
        {
            "fleet": {"serviceAreas": ["부산"]},
            "transport": {"offRoute": {"status": "visit", "basis": "included"}},
        },
        MISO,
    )
    return app
