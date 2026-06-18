"""tests/test_matching_service.py — اختبارات نواة المطابقة (P2).

تتحقّق من بدائيات التطبيع، الحُرّاس، قرار الملكية الثلاثي، والمطابقة الكاملة.
"""
import pytest

from services.matching_service import (
    MatchingService,
    Ownership,
    ar_skeleton,
    decide_ownership,
    gender_conflict,
    load_engine_kernel,
    miss_bare,
    miss_toks,
    size_ok,
    skel_toks,
)

KERNEL = load_engine_kernel()


def test_miss_bare_arabic_normalization() -> None:
    # مثال البرومبت: يجب أن يكون قابلاً للاختبار بمعزل
    bare = miss_bare("عطر شانيل N°5 او دو بارفيوم 100مل للنساء", KERNEL)
    assert "chanel" in bare
    assert "100" not in bare and "مل" not in bare  # حُذفت الأرقام والوحدات


def test_miss_toks_keeps_top4_len4() -> None:
    toks = miss_toks("aa bbbb cccc dddd eeee ffff")
    assert toks == ["bbbb", "cccc", "dddd", "eeee"]


def test_ar_skeleton_and_skel_toks() -> None:
    # تُزال الحروف الضعيفة؛ يبقى ما طوله ≥2 بعد التجريد
    assert ar_skeleton("كاشاريل") == ar_skeleton("كاشريل")  # نفس الهيكل
    toks = skel_toks("كاشاريل لوما روز")
    assert len(toks) <= 6 and all(len(t) >= 3 for t in toks)


def test_size_guard() -> None:
    assert size_ok(100, 105)            # ضمن التسامح 8.0
    assert not size_ok(50, 100)         # فرق كبير
    assert size_ok(0, 100) and size_ok(100, 0)  # غياب حجم ⇒ متوافق


def test_gender_conflict_requires_both_explicit() -> None:
    assert gender_conflict("رجالي", "نسائي")
    assert not gender_conflict("", "نسائي")
    assert not gender_conflict("رجالي", "")


@pytest.mark.parametrize(
    "score,has,size_okay,clash,expected",
    [
        (90, True, True, False, Ownership.OWNED),
        (70, True, True, False, Ownership.REVIEW),
        (40, True, True, False, Ownership.MISSING),
        (90, True, False, False, Ownership.REVIEW),   # حجم مختلف
        (70, True, True, True, Ownership.REVIEW),      # تعارض جنس
    ],
)
def test_decide_ownership_branches(score, has, size_okay, clash, expected) -> None:
    assert decide_ownership(score, has, size_okay, clash)[0] == expected


def test_decide_ownership_reasons() -> None:
    assert decide_ownership(90, True, False, False)[1] == "متوفّر بحجم مختلف"
    assert decide_ownership(70, True, True, True)[1] == "جنس مختلف — تأكيد بشري"
    assert decide_ownership(70, True, True, False)[1] == "بانتظار التحقق"


def test_end_to_end_owned_and_missing() -> None:
    svc = MatchingService([
        "عطر شانيل شانس او تندر او دو بارفيوم 100 مل للنساء",
        "ديور سوفاج او دو تواليت 100مل",
    ])
    owned = svc.evaluate("شانيل شانس او تندر 100ml")
    missing = svc.evaluate("زجاجة ماء معدني عشوائية تماما")
    assert owned.ownership == Ownership.OWNED and owned.score >= 82
    assert missing.ownership == Ownership.MISSING
