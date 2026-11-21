"""Site material balance; a linked source is mandatory, not a default 1:1 credit."""

from app.v3.judge.common import num


def displaced(reuse: dict, input_kg: float) -> float | None:
    if num(input_kg) is None or input_kg < 0:
        return None
    yield_pct = num(reuse.get("recoveryYieldPct"))
    substitution = num(reuse.get("virginSubstitutionPct"))
    if (
        yield_pct is None
        or substitution is None
        or not 0 <= yield_pct <= 100
        or not 0 <= substitution <= 100
        or not reuse.get("recoveryApplication")
        or not reuse.get("recoveryEvidence")
    ):
        return None
    return input_kg * yield_pct / 100 * substitution / 100
