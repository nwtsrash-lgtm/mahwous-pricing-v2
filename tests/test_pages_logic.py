"""tests/test_pages_logic.py — اختبارات منطق الصفحات الخالص (P4) دون Streamlit."""
import pandas as pd

from core.enums import SectionType
from core.models import AnalysisResult, ReconciliationReport
from ui.pages._section_page import section_dataframe
from ui.pages.dashboard import conservation_status, kpi_metrics
from ui.pages.missing import split_missing
from ui.pages.processed import processed_rows
from ui.state_manager import AppState


def test_section_dataframe_extracts_by_enum_value() -> None:
    sections = {"price_raise": pd.DataFrame({"x": [1, 2]}), "approved": pd.DataFrame()}
    assert len(section_dataframe(sections, SectionType.PRICE_RAISE)) == 2
    assert section_dataframe(sections, SectionType.MISSING).empty  # مفتاح غائب


def test_conservation_status_states() -> None:
    assert conservation_status(None)[0] is True
    ok = ReconciliationReport.from_sections(
        10, {"price_raise": 4, "price_lower": 3, "approved": 1,
             "review": 1, "excluded": 1})
    assert conservation_status(ok)[0] is True
    bad = ReconciliationReport.from_sections(
        10, {"price_raise": 4, "price_lower": 3, "approved": 1,
             "review": 1, "excluded": 0})
    status, text = conservation_status(bad)
    assert status is False and "فجوة=1" in text


def test_kpi_metrics() -> None:
    result = AnalysisResult(section_counts={
        SectionType.PRICE_RAISE: 5, SectionType.MISSING: 3})
    metrics = kpi_metrics(result)
    assert ("🔴 سعر أعلى", 5) in metrics and ("🔍 منتجات مفقودة", 3) in metrics
    assert kpi_metrics(None) == []


def test_split_missing() -> None:
    df = pd.DataFrame({
        "منتج_المنافس": ["a", "b", "c"],
        "مستوى_الثقة": ["green", "review", "green"],
    })
    green, review = split_missing(df)
    assert len(green) == 2 and len(review) == 1
    # عمود غائب ⇒ الكل يُعاد كـ green-جانب والمراجعة فارغة
    g2, r2 = split_missing(pd.DataFrame({"منتج_المنافس": ["x"]}))
    assert len(g2) == 1 and r2.empty


def test_processed_rows_sorted_and_capped() -> None:
    state = AppState()
    for sku in ("c", "a", "b"):
        state.mark_price_processed(sku)
    assert processed_rows(state) == ["a", "b", "c"]
    assert processed_rows(state, limit=2) == ["a", "b"]
