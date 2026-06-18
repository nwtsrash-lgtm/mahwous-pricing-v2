"""tests/test_classification_service.py — اختبارات التوزيع وحفظ البيانات (P2)."""
import pandas as pd
import pytest

from core.exceptions import DataLossError
from services.classification_service import ClassificationService, split_results


def _df(decisions: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"القرار": decisions, "المنتج": [f"p{i}" for i in range(len(decisions))]})


def test_exclusive_distribution_and_conservation() -> None:
    df = _df([
        "🔴 سعر أعلى", "🟢 سعر أقل", "✅ موافق",
        "⚠️ تحت المراجعة", "🔍 منتجات مفقودة", "⚪ مستبعد",
    ])
    result = ClassificationService().classify(df)
    counts = result.counts()
    assert counts["price_raise"] == 1
    assert counts["price_lower"] == 1
    assert counts["approved"] == 1
    assert counts["review"] == 2            # ⚠️ و🔍 كلاهما مراجعة
    assert counts["excluded"] == 1
    assert result.gap == 0 and result.total_in == 6


def test_safety_net_unknown_decision_goes_excluded() -> None:
    df = _df(["🔴 سعر أعلى", "قرار غريب غير معروف", ""])
    result = ClassificationService().classify(df)
    assert result.counts()["excluded"] == 2  # المجهول + الفارغ
    assert result.gap == 0


def test_lower_matches_text_contains() -> None:
    # «سعر أقل» نصاً (بلا 🟢) يجب أن يقع في price_lower (#PRESERVED_LOGIC)
    res = split_results(_df(["مطابقة بسعر أقل من المنافس"]))
    assert len(res["price_lower"]) == 1


def test_empty_dataframe_returns_empty_sections() -> None:
    result = ClassificationService().classify(pd.DataFrame())
    assert result.total_in == 0 and result.gap == 0
    assert all(v == 0 for v in result.counts().values())


def test_strict_raises_on_duplicate_overlap() -> None:
    # قرار يبدأ 🔴 ويحوي «سعر أقل» ⇒ يظهر في قسمين ⇒ خرق حفظ البيانات
    df = _df(["🔴 سعر أقل"])
    with pytest.raises(DataLossError):
        ClassificationService().classify(df, strict=True)


def test_missing_column_is_tolerated() -> None:
    df = pd.DataFrame({"المنتج": ["a", "b"]})  # لا عمود قرار
    result = ClassificationService().classify(df)
    assert result.counts()["excluded"] == 2 and result.gap == 0
