"""tests/test_integration.py — اختبار التكامل الشامل (P5).

يتحقّق من السلسلة الكاملة: كتالوج → مطابقة → تسعير → تصنيف → تدقيق،
مع فرض «قانون حفظ البيانات» (gap=0، تكرار=0) عبر الحاوية والمنسّق.
"""
import pandas as pd
import pytest

from bootstrap import build_container, run_analysis
from core.enums import SectionType
from core.exceptions import DataLossError
from services.matching_service import MatchingService, Ownership
from services.pricing_service import PricingService


def _balanced_results() -> pd.DataFrame:
    return pd.DataFrame({
        "القرار": ["🔴 سعر أعلى", "🟢 سعر أقل", "✅ موافق",
                   "⚠️ تحت المراجعة", "⚪ مستبعد"],
        "منتج_المنافس": ["عطر أ", "عطر ب", "عطر ج", "عطر د", "عطر هـ"],
        "السعر": [120, 90, 100, 0, 0],
        "سعر_المنافس": [100, 110, 100, 0, 0],
    })


def test_container_wires_all_services() -> None:
    c = build_container()
    assert all([c.classification, c.pricing, c.audit, c.ai, c.export, c.db])
    assert isinstance(c.matching_for(["x"]), MatchingService)


def test_end_to_end_zero_data_loss() -> None:
    container = build_container()
    result, split, missing_df = run_analysis(container, _balanced_results())
    report = result.reconciliation
    assert report.gap == 0 and report.duplicate_count == 0 and report.is_balanced
    result.assert_conservation()                      # لا يرفع
    assert result.total == 5
    assert result.section_counts[SectionType.PRICE_RAISE] == 1
    assert result.section_counts[SectionType.EXCLUDED] == 1
    assert missing_df is None


def test_full_pipeline_match_price_classify_conserved() -> None:
    """منتجنا + منافسون → قرارات → تصنيف → تدقيق متوازن."""
    matching = MatchingService(["عطر شانيل شانس او تندر 100 مل"])
    pricing = PricingService()
    competitors = [("شانيل شانس او تندر 100ml", 230), ("منتج مجهول تماما لا نملكه", 100)]
    rows = []
    for cname, cprice in competitors:
        outcome = matching.evaluate(cname)
        if outcome.ownership is Ownership.OWNED:
            decision, _ = pricing.evaluate("S1", our_price=250, comp_price=cprice)
            rows.append({"القرار": decision.decision, "المنتج": "عطر شانيل",
                         "منتج_المنافس": cname, "السعر": 250, "سعر_المنافس": cprice})
        else:
            rows.append({"القرار": "🔍 منتجات مفقودة", "المنتج": "",
                         "منتج_المنافس": cname, "السعر": 0, "سعر_المنافس": cprice})
    result, split, _ = run_analysis(build_container(), pd.DataFrame(rows))
    assert result.reconciliation.is_balanced
    assert split.counts()["price_raise"] == 1         # شانيل المملوك: 250>230 ⇒ أعلى
    assert split.counts()["review"] == 1              # 🔍 ⇒ مراجعة


def test_run_analysis_raises_on_duplicate_overlap() -> None:
    # قرار يبدأ 🔴 ويحوي «سعر أقل» ⇒ صفّ في قسمين ⇒ خرق حفظ البيانات
    bad = pd.DataFrame({"القرار": ["🔴 سعر أقل"], "منتج_المنافس": ["x"], "السعر": [1]})
    with pytest.raises(DataLossError):
        run_analysis(build_container(), bad)


def test_missing_integration_and_duplicate_guard() -> None:
    """مفقود يتطابق اسمه مع قسم سعري ⇒ يكشفه المدقّق ويرفع DataLossError."""
    shared = "عطر فلورال نادر اصلي 100 مل"
    results = pd.DataFrame({
        "القرار": ["🔴 سعر أعلى"], "منتج_المنافس": [shared],
        "السعر": [300], "سعر_المنافس": [250],
    })
    candidates = [{"product_name": shared, "min_price": 250, "competitor_count": 1}]
    with pytest.raises(DataLossError):
        run_analysis(
            build_container(), results,
            our_names=["شامبو للشعر الجاف"], missing_candidates=candidates,
        )
