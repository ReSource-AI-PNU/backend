"""평가 설계의 층(L1~L7)을 한 번에 돌리고 리포트(JSON + Markdown)와 latest.json을 쓴다(15장, 17장).

    run_suite(layers, split, repeats, llm, budget_calls) -> 결과 dict
    render_markdown(result) -> str
    update_latest(path, result, report_paths) -> latest.json의 dict

CLI는 scripts/v3/eval/run_all.py다. 규칙 정책(llm=rule)은 모델을 부르지 않는다. 그 결과는 회귀
시험이지 AI 품질이 아니라는 표시를 리포트와 latest.json에 늘 싣는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.v3.eval import (
    injection,
    leakage,
    metamorphic,
    negotiation,
    public,
    retrieval,
    sets,
    stats,
    trials,
)

ROOT = Path(__file__).resolve().parents[3]
LAYERS = ("L1", "L2", "L3", "L4", "L5", "L6", "L7")
RULE_LABEL = "규칙 정책(모델 미연결)"
PUBLIC_RULE_LABEL = "모델을 연결하지 않은 기본 규칙"  # 신뢰 페이지(latest.json)에 쓰는 이름
CI_NOTE = (
    "95% 신뢰구간입니다. 비율(성공률, 통과율 등)은 Wilson 점수 구간(Wilson 1927)으로 구해, 시험 수가 "
    "적고 결과가 모두 성공이거나 모두 실패여도 폭이 0이 되지 않습니다. 순위 점수와 횟수의 평균은 "
    "재표집(부트스트랩)으로 구합니다."
)
STALE_DAYS = 30
# 평가 설계 3장의 목표. 결과가 아니라 목표다(isTarget).
DESIGN_TARGETS = {
    "L1": "현장 plastic AP50 ≥ 0.70, 오염 비율 MAE ≤ 5%p",
    "L2": "nDCG@10 ≥ 0.75, Recall@10 ≥ 0.92",
    "L3": "첫 시도 ≥ 0.85, 충실도 ≥ 0.95, 나간 글 숫자 환각 0",
    "L4": "pass^1 ≥ 0.80, 위반 0",
    "L5": "정규화 후회 ≤ 0.05",
    "L6": "코드로 막는 목표의 ASR 0, 노출 0",
    "L7": "기준마다 평균 ≥ 4.0/5",
}
HEADLINE = {
    "pass1": ("L4", "pass^1"),
    "pass4": ("L4", "pass^4"),
    "outgoingHallucination": ("L3", "outgoingHallucination"),
    "injectionGated": ("L6i", "asr_code_gated"),
    "exposures": ("L6c", "exposures"),
}


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def commit_label() -> str | None:
    """HEAD의 짧은 해시. 평가가 만드는 파일 말고 고친 파일이 있으면 '+수정본'을 붙인다."""
    head = _git("rev-parse", "--short", "HEAD")
    changed = _git(
        "status",
        "--porcelain",
        "--untracked-files=no",
        "--",
        ".",
        ":!docs/v3/eval",
        ":!docs/v3/eval-report.md",
    )
    if head and changed:
        head += "+수정본"
    return head


def llm_factory_for(llm: str, env: Any = None) -> Callable[..., Any] | None:
    """rule이면 None(규칙 정책). foundry면 환경 변수의 Foundry 배포(설정이 빠지면 멈춘다)."""
    if llm == "rule":
        return None
    from app.v3.agents.factory import build_llm_factory

    return build_llm_factory(llm, os.environ if env is None else env, strict=True)


def _skipped_layer(code: str, title: str, reason: str) -> dict[str, Any]:
    return stats.layer(
        code,
        title,
        unit="-",
        n=0,
        status="not_measured",
        metrics=[],
        not_measured_items=[reason],
        notes=[f"목표(결과 아님): {DESIGN_TARGETS.get(code[:2], '-')}"],
    )


def l1_layer() -> dict[str, Any]:
    return _skipped_layer(
        "L1",
        "인식(탐지 모델)",
        "공장 현장 사진셋(200장)과 오염 비율 정답이 아직 없다. 공개 셋 결과는 backend 밖 "
        "resource-ai-vision/ml/reports에 있고 이 실행에서 재현하지 않았다",
    )


def l7_layer() -> dict[str, Any]:
    return _skipped_layer(
        "L7",
        "사람·LLM 판정",
        "루브릭, 판정 프롬프트, 사람 라벨 120건(이중 채점)이 아직 없다. 판정 모델도 정하지 않았다",
    )


def run_suite(
    *,
    layers: tuple[str, ...] = LAYERS,
    split: str = "test",
    repeats: int = 1,
    llm: str = "rule",
    budget_calls: int | None = None,
    resamples: int = stats.RESAMPLES,
    keep_raw: bool = True,
    progress: Callable[[str], None] | None = None,
    llm_factory: Callable[..., Any] | None = None,
    env: Any = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    say = progress or (lambda _msg: None)
    if llm_factory is None:
        llm_factory = llm_factory_for(llm, env)
    meter = trials.Meter(llm_factory, budget_calls)
    factory = meter.factory
    version = sets.CURRENT_TEST if split == "test" else "dev"
    manifest = sets.read_manifest(sets.CURRENT_TEST) if split == "test" else None
    out_layers: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []

    def budget_left(code: str, title: str) -> bool:
        if meter.over_budget():
            out_layers.append(_skipped_layer(code, title, trials.BUDGET_SKIPPED))
            return False
        return True

    if "L1" in layers:
        out_layers.append(l1_layer())
    if "L2" in layers:
        say("L2 검색")
        out_layers.append(retrieval.run_layer(split, resamples=resamples, env=env))
    outcome = None
    if ("L3" in layers or "L4" in layers) and budget_left("L4", "에이전트 과업 성공(끝 상태)"):
        say(f"L4 시나리오 {split} × {repeats}")
        cases = sets.load_split(split)
        outcome = trials.run_trials(cases, repeats=repeats, keep_raw=keep_raw, meter=meter)
        extra_metrics, extra_tables, notes = [], [], []
        if split == "test":
            say("L4 변형 80")
            base = {t["caseId"]: t for t in outcome["trials"] if t["repeat"] == 0}
            results = metamorphic.run_variants(
                sets.load_variants(), base, options={"llm_factory": factory}, meter=meter
            )
            variant = metamorphic.variant_metrics(results, resamples=resamples)
            extra_metrics, extra_tables = variant["metrics"], variant["tables"]
        else:
            notes.append("변형(metamorphic)은 시험셋에만 있다")
        opened = [c["caseId"] for c in outcome["cases"] if c["open"]]
        fixes = sets.errata() if split == "test" else {}
        if opened:
            notes.append(
                f"open 사례(설계 판단 필요) {', '.join(opened)}는 성공률에 그대로 들어 있다"
            )
        for case_id in opened:
            if case_id in fixes:
                notes.append(f"{case_id} 정오표(sets/README.md): {fixes[case_id]}")
        if "L4" in layers:
            out_layers.append(
                trials.l4_layer(
                    outcome,
                    resamples=resamples,
                    extra_metrics=extra_metrics,
                    extra_tables=extra_tables,
                    notes=notes,
                )
            )
        if "L3" in layers:
            out_layers.append(trials.l3_layer(outcome, resamples=resamples))
        raw = [
            {"caseId": t["caseId"], "repeat": t["repeat"], **(t.get("raw") or {})}
            for t in outcome["trials"]
        ]
    if "L5" in layers and budget_left("L5", "협상 품질"):
        say("L5 협상")
        out_layers.append(negotiation.run_layer(split, llm_factory=factory, resamples=resamples))
    if "L6" in layers:
        if budget_left("L6c", "안전: 조직 간 노출"):
            say("L6 노출 카나리아·탐침")
            out_layers.append(leakage.run_layer(split, llm_factory=factory, resamples=resamples))
        if budget_left("L6i", "안전: 간접 주입·개인정보"):
            say("L6 주입·개인정보")
            out_layers.append(
                injection.run_layer(split, llm_factory=factory, resamples=resamples, env=env)
            )
    if "L7" in layers:
        out_layers.append(l7_layer())
    out_layers.sort(key=lambda layer: layer["layer"])
    model = (
        {"provider": "rule", "name": RULE_LABEL}
        if llm_factory is None
        else _model_names(llm_factory)
    )
    header = {
        "date": None,
        "commit": commit_label(),
        "split": split,
        "setVersion": version,
        "manifestHash": sets.manifest_hash(manifest) if manifest else None,
        "policy": llm,
        "isModel": llm_factory is not None,
        "companyModel": model.get("company"),
        "vendorModel": model.get("vendor"),
        "judgeModel": None,
        "repeats": repeats,
        "budgetCalls": budget_calls,
        "modelCalls": meter.calls,
        "promptTokens": meter.prompt_tokens,
        "completionTokens": meter.completion_tokens,
        "costKrw": None,
        "elapsedSeconds": round(time.perf_counter() - started, 1),
        "resamples": resamples,
        "seed": stats.SEED,
        "reviewState": "초안 기대값 기준(전문가 검토 전)",
    }
    return {"header": header, "layers": out_layers, "raw": raw}


def _model_names(factory: Callable[..., Any]) -> dict[str, Any]:
    out = {}
    for role in ("company", "vendor"):
        try:
            llm = factory(role, "org_eval", None)
        except Exception as error:  # noqa: BLE001 - 이름만 읽는다
            out[role] = {"provider": "?", "name": f"이름을 읽지 못함: {type(error).__name__}"}
            continue
        if llm is None:
            out[role] = {"provider": "rule", "name": RULE_LABEL}
        else:
            out[role] = {
                "provider": getattr(llm, "provider", ""),
                "name": getattr(llm, "model", ""),
            }
    return out


# ── 저장한 결과에서 다시 만들기(모델을 다시 부르지 않는다) ─────────────────


def _interval(m: dict[str, Any]) -> None:
    """지표 한 칸의 신뢰구간을 지금 규칙(stats)으로 다시 낸다. 부트스트랩 구간은 그대로 둔다."""
    if m.get("ci") is None or m.get("value") is None or not m.get("n"):
        return
    if stats.is_proportion(m["key"]):
        m["ci"] = list(stats.wilson_ci(m["value"], m["n"]) or ())
        m["ciMethod"] = stats.WILSON
    else:
        m["ciMethod"] = m.get("ciMethod") or stats.BOOTSTRAP


def recompute_intervals(result: dict[str, Any]) -> dict[str, Any]:
    """저장한 리포트(<날짜>-<split>-<llm>.json)의 비율 지표 신뢰구간을 Wilson 구간으로 다시 낸다.

    값과 n은 저장된 그대로다(모델을 다시 부르지 않는다). 범주별 성공률 표와 L2의 방식별 비교 표도
    같은 규칙으로 다시 쓴다. 고친 result를 돌려준다(제자리에서 고친다).
    """
    for layer in result["layers"]:
        for m in layer["metrics"]:
            _interval(m)
        by_system = (layer.get("details") or {}).get("bySystem") or {}
        for rows in by_system.values():
            for m in rows:
                _interval(m)
        for table in layer["tables"]:
            if table.get("title") == "범주별 성공률":
                for row in table["rows"]:
                    if row[1] and row[2] is not None:
                        low, high = stats.wilson_ci(row[2], row[1]) or (None, None)
                        row[3] = [round(low, 3), round(high, 3)]
            elif layer["layer"] == "L2" and str(table.get("title")).startswith("방식별 비교"):
                for row in table["rows"]:
                    system = str(row[0]).split(" ", 1)[0]
                    if system in by_system:
                        row[1:] = [retrieval.cell(m) for m in by_system[system]]
    return result


def rebuild_from_raw(raw_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    """<stem>.raw.jsonl과 같은 자리의 <stem>.json을 읽어 신뢰구간을 다시 낸 결과를 돌려준다.

    (결과, 원 출력 행, <stem>.json 경로). 원 출력은 바꾸지 않고, 행 수가 L4 실행 수와 같은지 본다.
    """
    raw_path = Path(raw_path)
    if not raw_path.name.endswith(".raw.jsonl"):
        raise ValueError("--from-raw에는 <날짜>-<split>-<llm>.raw.jsonl을 줍니다")
    json_path = raw_path.with_name(raw_path.name.removesuffix(".raw.jsonl") + ".json")
    if not json_path.exists():
        raise ValueError(f"같은 자리에 저장한 리포트가 없습니다: {json_path.name}")
    result = json.loads(json_path.read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in raw_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    runs = next(
        (
            m.get("runs") or m.get("n")
            for layer in result["layers"]
            if layer["layer"] == "L4"
            for m in layer["metrics"]
            if m["key"] == "pass^1"
        ),
        None,
    )
    if runs is not None and rows and len(rows) != runs:
        raise ValueError(f"원 출력 {len(rows)}행이 리포트의 L4 실행 {runs}번과 다릅니다")
    recompute_intervals(result)
    result["header"]["rebuiltFrom"] = {
        "raw": _rel(raw_path),
        "sha256": _sha256(raw_path),
        "rows": len(rows),
        "note": "저장한 결과에서 신뢰구간만 다시 냈다. 모델을 다시 부르지 않았다.",
    }
    return result, rows, json_path


# ── Markdown ─────────────────────────────────────────────────────────


def _fmt(value: Any) -> str:
    if value is None:
        return stats.NOT_MEASURED
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        return f"{value:.3f}".rstrip("0").rstrip(".") if value != int(value) else f"{value:.0f}"
    return str(value)


def _ci(ci: list[float] | None) -> str:
    return "-" if not ci else f"[{_fmt(float(ci[0]))}, {_fmt(float(ci[1]))}]"


def _n(m: dict[str, Any]) -> str:
    if m.get("n") is None:
        return "-"
    text = str(m["n"])
    if m.get("runs") and m["runs"] != m["n"]:
        text += f" (실행·항목 {m['runs']})"
    if m.get("smallSample"):
        text += " 표본 적음"
    return text


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(result: dict[str, Any]) -> str:
    h = result["header"]
    policy = RULE_LABEL if not h["isModel"] else f"{h['policy']} (모델)"
    lines = [
        f"# 평가 리포트 {h['date']} · {h['split']} · {h['policy']}",
        "",
        (
            "`scripts/v3/eval/run_all.py`가 만들었다. 손으로 고치지 않는다. 재지 못한 칸은 "
            f'"{stats.NOT_MEASURED}"이다. 목표는 평가 설계 3장의 **목표이고 결과가 아니다**.'
        ),
        "",
    ]
    if not h["isModel"]:
        lines += [
            (
                "> **규칙 정책(모델 미연결) 실행이다.** 에이전트는 정해진 규칙으로 쓰고 모델을 부르지 "
                "않는다. 여기의 숫자는 코드(검증기, 가드레일, 조직 필터, 배정)의 회귀 시험 결과이고 AI "
                "품질이 아니다. 실제 모델 결과는 `--llm foundry`로 따로 돈다."
            ),
            "",
        ]
    lines += [
        "| 항목 | 값 |",
        "|---|---|",
        f"| 날짜 | {h['date']} |",
        f"| backend 커밋 | {h['commit'] or '-'} |",
        f"| 평가셋 판 | {h['setVersion']} |",
        f"| 매니페스트 해시 | {h['manifestHash'] or '-(개발셋)'} |",
        f"| 에이전트 구현 | {policy} |",
        f"| 회사 측 모델 | {_model_text(h['companyModel'])} |",
        f"| 업체 측 모델 | {_model_text(h['vendorModel'])} |",
        f"| 판정 모델 | {h['judgeModel'] or '없음(L3 의미 지표·L7 미측정)'} |",
        f"| 반복 수 n | {h['repeats']} |",
        f"| 모델 호출 · 입력/출력 토큰 | {h['modelCalls']} · {h['promptTokens']}/{h['completionTokens']} |",
        f"| 비용(원) | {h['costKrw'] if h['costKrw'] is not None else stats.NOT_MEASURED} |",
        f"| 호출 예산 | {h['budgetCalls'] if h['budgetCalls'] is not None else '없음'} |",
        f"| 걸린 시간 | {h['elapsedSeconds']}초 |",
        (
            "| 신뢰구간 | 95%. 비율 지표는 Wilson 점수 구간(Wilson 1927, n은 묶음 수), "
            f"nDCG·MRR·후회·턴 수는 퍼센타일 부트스트랩 {h['resamples']}번, 시드 {h['seed']} |"
        ),
        f"| 기대값 상태 | {h['reviewState']} |",
        "",
        "## 층별 요약",
        "",
        "| 층 | 상태 | n | 설계 목표(결과 아님) |",
        "|---|---|---|---|",
    ]
    status_text = {"measured": "측정", "partial": "일부 측정", "not_measured": stats.NOT_MEASURED}
    for layer in result["layers"]:
        lines.append(
            f"| {layer['layer']} {layer['title']} | {status_text[layer['status']]} | "
            f"{layer['n']} {layer['unit']} | {DESIGN_TARGETS.get(layer['layer'][:2], '-')} |"
        )
    lines.append("")
    for layer in result["layers"]:
        lines += [f"## {layer['layer']} {layer['title']}", ""]
        if layer["metrics"]:
            lines += [
                "| 지표 | 값 | 95% CI | n (묶음 수) | 목표 | 판정 |",
                "|---|---|---|---|---|---|",
            ]
            for m in layer["metrics"]:
                goal = (m.get("target") or {}).get("text") or "-"
                lines.append(
                    f"| {_cell(m['label'])} | {_fmt(m['value'])} | {_ci(m['ci'])} | {_n(m)} | "
                    f"{_cell(goal)} | {m['verdict']} |"
                )
            notes = [m for m in layer["metrics"] if m.get("note")]
            if notes:
                lines.append("")
                lines += [f"- {_cell(m['label'])}: {_cell(m['note'])}" for m in notes]
            lines.append("")
        for table in layer["tables"]:
            if not table["rows"]:
                continue
            lines += [
                f"**{table['title']}**",
                "",
                "| " + " | ".join(table["columns"]) + " |",
                "|" + "---|" * len(table["columns"]),
            ]
            for row in table["rows"]:
                lines.append(
                    "| " + " | ".join(_cell(_fmt(c) if c is not None else "-") for c in row) + " |"
                )
            lines.append("")
        if layer["notMeasured"]:
            lines += ["미측정:", ""]
            lines += [f"- {_cell(x)}" for x in layer["notMeasured"]]
            lines.append("")
        if layer["notes"]:
            lines += [f"- {_cell(x)}" for x in layer["notes"]]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _model_text(model: dict[str, Any] | None) -> str:
    if not model:
        return RULE_LABEL
    return " · ".join(x for x in (model.get("provider"), model.get("name")) if x) or "-"


# ── latest.json (신뢰 페이지용) ──────────────────────────────────────


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


METRIC_FIELDS = ("key", "value", "ci", "ciMethod", "n", "runs", "target", "verdict", "smallSample")


def public_metric(code: str, m: dict[str, Any]) -> dict[str, Any]:
    """신뢰 페이지에 싣는 지표 한 칸. label·explain은 쉬운 말, technicalLabel은 리포트의 이름이다."""
    label, explain = public.metric_text(code, m["key"], m["label"])
    out = {k: m.get(k) for k in METRIC_FIELDS}
    if out["ci"] is not None and out["ciMethod"] is None:  # ciMethod 전의 리포트
        out["ciMethod"] = stats.WILSON if stats.is_proportion(m["key"]) else stats.BOOTSTRAP
    return {**out, "label": label, "explain": explain or None, "technicalLabel": m["label"]}


def summary_for_latest(result: dict[str, Any], files: list[Path]) -> dict[str, Any]:
    """신뢰 페이지용 요약. 이름과 풀이는 쉬운 말이고(public.py), 층 코드·지표 키는 기계 ID로 따로 있다."""
    h = result["header"]
    layers = {}
    for layer in result["layers"]:
        title, explain = public.layer_text(layer["layer"], layer["title"])
        layers[layer["layer"]] = {
            "title": title,
            "explain": explain,
            "technicalTitle": layer["title"],
            "status": layer["status"],
            "n": layer["n"],
            "unit": layer["unit"],
            "metrics": [public_metric(layer["layer"], m) for m in layer["metrics"]],
            "notMeasured": public.not_measured_text(layer["notMeasured"]),
            "notMeasuredTechnical": layer["notMeasured"],
        }
    headline = {}
    for key, (code, metric_key) in HEADLINE.items():
        found = next(
            (
                m
                for layer in result["layers"]
                if layer["layer"] == code
                for m in layer["metrics"]
                if m["key"] == metric_key
            ),
            None,
        )
        headline[key] = (
            None
            if found is None
            else {
                k: v
                for k, v in public_metric(code, found).items()
                if k
                in (
                    "key",
                    "label",
                    "explain",
                    "technicalLabel",
                    "value",
                    "ci",
                    "ciMethod",
                    "n",
                    "target",
                    "verdict",
                )
            }
        )
    date = h["date"]
    return {
        "date": date,
        # 공개 신뢰 페이지에는 모델 이름 대신 "AI 판(평가일)"을 쓴다(평가 설계 17장, DESIGN.md)
        "aiVersion": f"AI 판({date})" if h["isModel"] else f"{PUBLIC_RULE_LABEL} · {date}",
        "isModel": h["isModel"],
        "policy": h["policy"],
        "split": h["split"],
        "setVersion": h["setVersion"],
        "manifestHash": h["manifestHash"],
        "commit": h["commit"],
        "repeats": h["repeats"],
        "reviewState": h["reviewState"],
        "nPerLayer": {k: v["n"] for k, v in layers.items()},
        "headline": headline,
        "layers": layers,
        "targetsAreNotResults": True,
        "designTargets": DESIGN_TARGETS,
        "staleAfterDays": STALE_DAYS,
        "ciNote": CI_NOTE,
        "rebuiltFrom": h.get("rebuiltFrom"),
        "sources": [{"path": _rel(p), "sha256": _sha256(p)} for p in files if p.exists()],
    }


HISTORY_LIMIT = 10
SUPERSEDED_REASON = "같은 평가셋을 나중 코드로 다시 잰 결과로 바뀌었습니다."


def superseded(old: dict[str, Any], new: dict[str, Any], reason: str = SUPERSEDED_REASON) -> dict:
    """지난 실행의 요약(현재 결과가 아니다). 신뢰 페이지가 지금 결과처럼 보이면 안 된다."""
    return {
        "current": False,
        "split": old.get("split"),
        "date": old.get("date"),
        "commit": old.get("commit"),
        "policy": old.get("policy"),
        "isModel": old.get("isModel"),
        "supersededBy": {"date": new.get("date"), "commit": new.get("commit")},
        "reason": reason,
        "headline": {
            key: None
            if item is None
            else {
                "label": public.metric_text(*HEADLINE.get(key, ("", key)), str(item.get("label")))[
                    0
                ],
                "value": item.get("value"),
            }
            for key, item in (old.get("headline") or {}).items()
        },
    }


def _headline_values(run: dict[str, Any]) -> dict[str, Any]:
    return {k: (v or {}).get("value") for k, v in (run.get("headline") or {}).items()}


def update_latest(path: Path, result: dict[str, Any], files: list[Path]) -> dict[str, Any]:
    """latest.json의 runs[split]을 이번 실행으로 바꾼다. 신뢰 페이지는 primary(시험셋)를 먼저 본다.

    바뀐 실행의 주요 값이 이번과 다르면 history에 current: false로 남긴다(지난 결과가 지금 결과로 보이지
    않게, 그러나 숨기지도 않게). 같은 값이면 남기지 않는다.
    """
    current: dict[str, Any] = {}
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
    runs = dict(current.get("runs") or {})
    history = list(current.get("history") or [])
    split = result["header"]["split"]
    new = summary_for_latest(result, files)
    old = runs.get(split)
    if old and _headline_values(old) != _headline_values(new):
        history.insert(0, superseded(old, new))
    runs[split] = new
    latest = {
        "note": (
            "평가 실행의 최신 요약. 신뢰 페이지(평가 설계 17장)가 읽는다. 목표(target.isTarget)는 결과가 "
            "아니다. isModel이 false인 실행은 모델을 연결하지 않은 기본 규칙의 회귀 시험이고 AI 결과로 "
            f"보이면 안 된다. 날짜에서 {STALE_DAYS}일이 지나면 '오래된 결과'로 표시한다. label·explain·"
            "title은 공개 화면용 쉬운 말이고 technicalLabel·technicalTitle·key·층 코드는 기술 이름이다. "
            "history는 지난 결과(current: false)이고 지금 결과가 아니다."
        ),
        "primary": "test" if "test" in runs else next(iter(runs)),
        "runs": dict(sorted(runs.items())),
        "history": history[:HISTORY_LIMIT],
    }
    path.write_text(json.dumps(latest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return latest
