"""평가 통계. 신뢰구간, pass^k, 짝 비교, 지표 한 칸의 모양(평가 설계 4.3, 8장, 15장).

    wilson_ci(p, n) -> (하한, 상한) | None       # 비율 지표
    bootstrap_ci(values, clusters=None) -> (하한, 상한) | None   # 비율이 아닌 지표
    pass_hat_k(counts, n, k) -> float           # τ-bench의 pass^k 추정
    paired_bootstrap(a, b) -> {"delta", "ci", "n", "verdict"}
    metric(key, label, value, ci=..., n=..., target=...) -> 리포트 한 줄

- 비율 지표(성공률, 통과율, 거부율, 표적 ASR, 재현율처럼 0~1인 값의 평균)의 95% 신뢰구간은 Wilson 점수
  구간이다(Wilson 1927, "Probable inference, the law of succession, and statistical inference",
  JASA 22(158)). n이 10~30이고 값이 0이나 1이면 퍼센타일 부트스트랩은 [1, 1]처럼 폭이 0인 구간을 내지만,
  Wilson 구간은 그렇지 않다(n=10에서 10/10이면 [0.72, 1]).
  - n은 독립 단위의 수다. 묶음(clusters)이 있으면 묶음 수를 n으로 쓰고 p̂는 전체 평균이다. 0~1 값의 분산은
    p(1−p)를 넘지 않으므로 묶음 평균이나 0~1 사이 값(재현율 등)에 쓰면 구간이 실제보다 넓어진다(보수적).
- 비율이 아닌 지표(nDCG, MRR, 정규화 후회, 전술 민감도, 턴 수)는 95% 퍼센타일 부트스트랩이고 재표집은
  10,000번, 시드는 고정이다(같은 입력이면 같은 구간). 묶음을 주면 묶음 단위로 재표집한다.
- 지표 한 칸의 ciMethod가 "wilson" | "bootstrap"이다. 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Hashable, Sequence
from typing import Any

SEED = 20261118
RESAMPLES = 10_000
ALPHA = 0.05
SMALL_SAMPLE = 10  # 세부 범주 칸이 이보다 작으면 "표본 적음"(평가 설계 4.3)
NOT_MEASURED = "미측정"
Z_95 = 1.959963984540054  # 표준정규분포의 97.5% 분위수
WILSON, BOOTSTRAP = "wilson", "bootstrap"
# 0~1 값이지만 비율이 아닌 지표(부트스트랩으로 구간을 낸다). 나머지 rate_metric 지표는 비율이다
BOOTSTRAP_KEYS = frozenset(
    {
        "ndcg@10",
        "mrr",
        "normalizedRegret",
        "regretWithTransport",
        "tacticSensitivity",
        "turnsToAgreement",
        "turnsPerThread",
        "latencyP50",
        "latencyP95",
    }
)


def is_proportion(key: str) -> bool:
    """신뢰구간을 Wilson으로 내는 비율 지표인가."""
    return key not in BOOTSTRAP_KEYS


def wilson_ci(p: float | None, n: float | None, *, z: float = Z_95) -> tuple[float, float] | None:
    """비율 p(0~1)와 독립 단위 수 n의 Wilson 점수 구간(Wilson 1927). n이 없거나 0이면 None.

    center = (p + z²/2n) / (1 + z²/n), half = z·sqrt(p(1−p)/n + z²/4n²) / (1 + z²/n).
    """
    if p is None or not n or n <= 0:
        return None
    p = min(1.0, max(0.0, float(p)))
    n = float(n)
    z2 = z * z
    denominator = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denominator
    low = 0.0 if p == 0.0 else max(0.0, center - half)
    high = 1.0 if p == 1.0 else min(1.0, center + half)  # 끝값은 반올림 오차 없이 0과 1이다
    return (low, high)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def mean(values: Sequence[float]) -> float | None:
    return _mean(values) if values else None


def _percentile(sorted_values: list[float], q: float) -> float:
    """선형 보간 없는 퍼센타일(가장 가까운 순위). 부트스트랩 분포가 커서 차이가 없다."""
    index = min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))
    return sorted_values[index]


def bootstrap_ci(
    values: Sequence[float],
    *,
    clusters: Sequence[Hashable] | None = None,
    statistic: Callable[[Sequence[float]], float] = _mean,
    resamples: int = RESAMPLES,
    seed: int = SEED,
    alpha: float = ALPHA,
) -> tuple[float, float] | None:
    """퍼센타일 부트스트랩 구간. 값이 없으면 None.

    clusters가 있으면 values[i]는 clusters[i] 묶음에 속하고, 묶음을 복원 추출해 그 안의 값을 모두
    넣는다(묶음 표준오차와 같은 생각, Miller 2024).
    """
    if not values:
        return None
    if clusters is not None and len(clusters) != len(values):
        raise ValueError("clusters와 values의 길이가 다르다")
    rng = random.Random(seed)
    if clusters is None:
        groups = [[v] for v in values]
    else:
        by: dict[Hashable, list[float]] = {}
        for value, cluster in zip(values, clusters, strict=True):
            by.setdefault(cluster, []).append(value)
        groups = [by[key] for key in sorted(by, key=str)]
    if len(groups) == 1 or len(set(values)) == 1:
        point = statistic(values)
        return (point, point)
    size = len(groups)
    stats: list[float] = []
    for _ in range(resamples):
        sample: list[float] = []
        for _ in range(size):
            sample.extend(groups[rng.randrange(size)])
        stats.append(statistic(sample))
    stats.sort()
    return (_percentile(stats, alpha / 2), _percentile(stats, 1 - alpha / 2))


def pass_hat_k(counts: Sequence[int], n: int, k: int) -> float | None:
    """τ-bench(Yao et al., ICLR 2025)의 pass^k: 과업마다 C(c,k)/C(n,k)를 평균한다.

    c는 n번 시도 가운데 성공한 횟수다. k번 독립 시도가 모두 성공할 확률을 n번 시도로 추정한 값이고,
    k=1이면 평균 성공률이다. k > n이면 추정할 수 없어 None.
    """
    if not counts or k < 1 or k > n:
        return None
    return _mean([per_task_pass_hat_k(c, n, k) for c in counts])


def per_task_pass_hat_k(c: int, n: int, k: int) -> float:
    if c > n or c < 0:
        raise ValueError(f"성공 횟수 {c}가 시도 횟수 {n}의 범위를 벗어났다")
    return math.comb(c, k) / math.comb(n, k)


def paired_bootstrap(
    a: Sequence[float],
    b: Sequence[float],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
    alpha: float = ALPHA,
) -> dict[str, Any]:
    """같은 사례의 두 판 값(a 뒤, b 앞)의 짝 차이 a − b를 재표집한다(Koehn 2004, Miller 2024).

    차이의 CI가 0을 포함하면 "차이 확인 못 함"이다.
    """
    if len(a) != len(b):
        raise ValueError("짝 비교에는 같은 사례 수가 필요하다")
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    if not diffs:
        return {"delta": None, "ci": None, "n": 0, "verdict": NOT_MEASURED}
    ci = bootstrap_ci(diffs, resamples=resamples, seed=seed, alpha=alpha)
    assert ci is not None
    if ci[0] > 0:
        verdict = "나아짐"
    elif ci[1] < 0:
        verdict = "나빠짐"
    else:
        verdict = "차이 확인 못 함"
    return {"delta": _mean(diffs), "ci": list(ci), "n": len(diffs), "verdict": verdict}


# ── 목표와 지표 한 칸 ─────────────────────────────────────────────────


def target(op: str, value: float, text: str | None = None) -> dict[str, Any]:
    """평가 설계 3장 등의 목표. 결과가 아니라 목표라는 표시(isTarget)를 늘 붙인다."""
    if op not in (">=", "<=", "=="):
        raise ValueError(f"모르는 비교: {op}")
    symbol = {">=": "≥", "<=": "≤", "==": "="}[op]
    return {"op": op, "value": value, "text": text or f"{symbol} {value:g}", "isTarget": True}


def verdict(value: float | None, goal: dict[str, Any] | None) -> str:
    if value is None:
        return NOT_MEASURED
    if goal is None:
        return "-"
    op, bound = goal["op"], goal["value"]
    ok = {
        ">=": value >= bound - 1e-12,
        "<=": value <= bound + 1e-12,
        "==": abs(value - bound) <= 1e-12,
    }[op]
    return "달성" if ok else "미달"


def metric(
    key: str,
    label: str,
    value: float | None,
    *,
    ci: Sequence[float] | None = None,
    n: int | None = None,
    clusters: int | None = None,
    runs: int | None = None,
    goal: dict[str, Any] | None = None,
    note: str | None = None,
    ci_method: str | None = None,
) -> dict[str, Any]:
    """리포트 표 한 줄(평가 설계 15장): 지표, 값, 95% CI(구하는 법), n(묶음 수), 목표, 판정."""
    small = n is not None and 0 < n < SMALL_SAMPLE
    if ci is not None and ci_method is None:
        ci_method = BOOTSTRAP
    return {
        "key": key,
        "label": label,
        "value": value,
        "ci": list(ci) if ci is not None else None,
        "ciMethod": ci_method if ci is not None else None,
        "n": n,
        "clusters": clusters,
        "runs": runs,
        "target": goal,
        "verdict": verdict(value, goal),
        "smallSample": small,
        "note": note,
    }


def rate_metric(
    key: str,
    label: str,
    flags: Sequence[bool | int | float],
    *,
    clusters: Sequence[Hashable] | None = None,
    goal: dict[str, Any] | None = None,
    note: str | None = None,
    resamples: int = RESAMPLES,
) -> dict[str, Any]:
    """0/1(또는 0~1) 값의 평균과 95% CI. 값이 없으면 미측정.

    비율 지표는 Wilson 점수 구간(n은 묶음이 있으면 묶음 수), BOOTSTRAP_KEYS의 지표는 부트스트랩이다.
    """
    values = [float(f) for f in flags]
    if not values:
        return metric(key, label, None, n=0, goal=goal, note=note)
    groups = len(set(clusters)) if clusters is not None else None
    n = groups if groups is not None else len(values)
    value = _mean(values)
    if is_proportion(key):
        ci, method = wilson_ci(value, n), WILSON
    else:
        ci, method = bootstrap_ci(values, clusters=clusters, resamples=resamples), BOOTSTRAP
    return metric(
        key,
        label,
        value,
        ci=ci,
        n=n,
        clusters=groups,
        runs=len(values) if groups is not None else None,
        goal=goal,
        note=note,
        ci_method=method,
    )


def count_metric(
    key: str,
    label: str,
    count: int,
    *,
    n: int,
    goal: dict[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """건수 지표(위반 건수, 노출 건수). 0이어야 하는 게이트라 CI 대신 n을 붙인다."""
    return metric(key, label, float(count), n=n, goal=goal, note=note)


def not_measured(
    key: str, label: str, reason: str, goal: dict[str, Any] | None = None
) -> dict[str, Any]:
    return metric(key, label, None, goal=goal, note=reason)


def layer(
    code: str,
    title: str,
    *,
    unit: str,
    n: int,
    metrics: list[dict[str, Any]],
    status: str = "measured",
    tables: list[dict[str, Any]] | None = None,
    not_measured_items: list[str] | None = None,
    notes: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """층 하나의 결과. status는 measured | partial | not_measured."""
    return {
        "layer": code,
        "title": title,
        "status": status,
        "unit": unit,
        "n": n,
        "metrics": metrics,
        "tables": tables or [],
        "notMeasured": not_measured_items or [],
        "notes": notes or [],
        "details": details or {},
    }
