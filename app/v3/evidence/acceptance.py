"""A label or a stored number alone is not authority to use it in a new decision."""

from datetime import date


def approved_policy(evidence) -> bool:
    meta = evidence.meta
    approval = meta.get("policyApproval") or {}
    if meta.get("evidenceClass") != "policy" or not isinstance(approval, dict):
        return False
    if approval.get("status") != "approved" or not all(
        isinstance(approval.get(k), str) and approval[k].strip()
        for k in ("approvedBy", "reason", "source", "approvedOn")
    ):
        return False
    try:
        date.fromisoformat(approval["approvedOn"])
    except ValueError:
        return False
    return True


def usable_basis(evidence) -> bool:
    """Block unsupported defaults; historical snapshots remain readable."""
    meta = evidence.meta
    if meta.get("synthetic") or meta.get("testOnly") or meta.get("demo"):
        return False
    if meta.get("evidenceClass") == "policy":
        return approved_policy(evidence)
    if evidence.kind == "assumption" or meta.get("evidenceClass") in ("assumption", "hold"):
        return False
    if evidence.kind == "ref":
        return meta.get("verificationStatus") == "original_checked"
    return evidence.kind in ("doc", "measure", "human")
