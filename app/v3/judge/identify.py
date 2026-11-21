"""① 식별·등급·출처."""

from __future__ import annotations

from app.v3.judge.common import Recorder, grade_of, num
from app.v3.textutil import josa


def identify_stream(registry, *, org_id, today, stream, moisturePct=None):
    rec = Recorder("identify_stream", registry, org_id, today, {"stream": stream, "m": moisturePct})
    measure, photo = stream.get("measure") or {}, stream.get("photo") or {}
    answers = stream.get("answers") or {}
    asked = num(moisturePct) if moisturePct is not None else num(answers.get("moisturePct"))
    output = {
        "streamId": stream.get("id"),
        "wasteItem": stream.get("wasteItem"),
        "material": stream.get("material"),
        "origin": stream.get("origin"),
        "form": stream.get("form"),
        "grade": None,
        "basis": None,
        "photoGrade": None,
    }
    for field, name in (("material", "재질"), ("origin", "출처(PIR·PCR)"), ("form", "형태")):
        if not stream.get(field):
            rec.check(field, "conditional", f"{josa(name, '이가')} 확인되지 않았습니다.")

    def graded(prefix, cont, moist, evidence, label):
        grade, limits = grade_of(rec, stream.get("material"), cont, moist)
        rec.number(f"{prefix}ContaminationPct", cont, "%", f"{label} 오염도", evidence)
        rec.number(f"{prefix}MoisturePct", moist, "%", f"{label} 수분", evidence)
        return grade, limits

    m_cont, m_moist = num(measure.get("contaminationPct")), num(measure.get("moisturePct"))
    p_cont = num(photo.get("contaminationPct"))
    limits: list[str] = []
    # 측정값이 있는지와 등급을 낼 수 있는지는 다르다(공통 등급 기준이 보류면 grade는 None이어도 측정값은 쓴다)
    measured = m_cont is not None and m_moist is not None
    if measured:
        output["grade"], limits = graded(
            "measured", m_cont, m_moist, measure.get("evidence"), "측정"
        )
        output["basis"] = "measured"
    photo_moist = asked
    photo_extra = list(answers.get("evidence") or [])
    if photo_moist is None and measured:
        # 측정값이 함께 있으면 견주기 위해 측정 수분을 쓴다(intake.grade_model.grade_stream과 같다)
        photo_moist, photo_extra = m_moist, list(measure.get("evidence") or [])
    if p_cont is not None and photo_moist is not None:
        evidence = [*(photo.get("evidence") or []), *photo_extra]
        photo_grade, photo_limits = graded("photo", p_cont, photo_moist, evidence, "사진 추정")
        output["photoGrade"] = photo_grade
        if not measured:
            output["grade"], output["basis"], limits = photo_grade, "photo_estimate", photo_limits
        elif None not in (photo_grade, output["grade"]) and photo_grade != output["grade"]:
            rec.check(
                "grade_disagreement",
                "conditional",
                "측정 등급과 사진 추정 등급이 다릅니다. 측정 등급을 우선했습니다.",
                "측정한 시료와 사진의 로트가 같은지 확인해 주세요.",
                [*(measure.get("evidence") or []), *(photo.get("evidence") or [])],
            )
    elif p_cont is not None and not measured:
        rec.check(
            "moisture",
            "conditional",
            "수분 값이 없어 사진만으로는 등급을 내지 못했습니다.",
            "이 폐기물의 수분 상태를 알려 주세요. 모르면 모른다고 답해도 됩니다.",
            photo.get("evidence"),
        )
    output["gradeEvidence"] = limits
    if output["grade"] is None and measured:
        # 측정값은 업체 매입 기준(오염도·수분 상한)과 바로 대조한다. 공통 등급만 보류다
        rec.check(
            "quality",
            "conditional",
            "측정값은 있지만 공통 품질 등급 기준이 검증되지 않아 등급 산정을 보류했습니다. "
            "업체 매입 기준과 반입 검수로 대조합니다.",
        )
        return rec.result("conditional", output)
    if output["grade"] is None:
        if not rec.checks or all(c["key"] != "moisture" for c in rec.checks):
            rec.check("quality", "conditional", "측정값과 사진 추정값이 모두 없습니다.")
        return rec.result("needs_information", output)
    return rec.result("conditional" if rec.checks else "calculated", output)
