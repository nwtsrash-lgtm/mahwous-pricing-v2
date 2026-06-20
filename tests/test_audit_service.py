"""tests/test_audit_service.py — اختبارات مدقّق حفظ البيانات (P2)."""
import pandas as pd
import pytest

from core.exceptions import DataLossError
from services.audit_service import (
    AuditService,
    dedup_missing_vs_matched,
    duplicate_check,
)
from services.classification_service import ClassificationService


def _classified(decisions: list[str], comp_names: list[str]):
    df = pd.DataFrame({"القرار": decisions, "منتج_المنافس": comp_names})
    return ClassificationService().classify(df)


def test_balanced_no_missing_is_conserved() -> None:
    split = _classified(["🔴 سعر أعلى", "✅ موافق"], ["عطر ألفا", "عطر بيتا"])
    report = AuditService().reconcile(split)
    assert report.gap == 0 and report.duplicate_count == 0 and report.is_balanced
    AuditService().assert_conserved(report)  # must not raise


def test_duplicate_between_price_and_missing_detected() -> None:
    split = _classified(["🔴 سعر أعلى", "✅ موافق"], ["عطر ألفا", "عطر بيتا"])
    missing_df = pd.DataFrame({"منتج_المنافس": ["عطر ألفا", "عطر جاما"]})
    report = AuditService().reconcile(split, missing_df)
    assert report.gap == 0
    assert report.duplicate_count == 1            # «عطر ألفا» في الجهتين
    assert not report.is_balanced
    with pytest.raises(DataLossError):
        AuditService().assert_conserved(report)


def test_dedup_missing_vs_matched_removes_priced_keeps_rest() -> None:
    """#PRESERVED_LOGIC app.py:1184-1222 — المطابَق سعرياً يُزال من المفقودات."""
    sections = {
        "price_raise": pd.DataFrame({"منتج_المنافس": ["  Oud Royal  ", "❌ فشل"]}),
        "price_lower": pd.DataFrame(),
        "approved": pd.DataFrame({"منتج_المنافس": ["Musk Pure"]}),
    }
    missing = pd.DataFrame({
        "منتج_المنافس": ["oud royal", "musk pure", "عطر فريد مفقود"],
        "مستوى_الثقة": ["review", "green", "green"],
    })
    cleaned, removed = dedup_missing_vs_matched(sections, missing)
    assert removed == 2                                   # oud royal + musk pure
    assert list(cleaned["منتج_المنافس"]) == ["عطر فريد مفقود"]


def test_dedup_missing_vs_matched_noop_when_no_overlap() -> None:
    sections = {"price_raise": pd.DataFrame({"منتج_المنافس": ["X"]})}
    missing = pd.DataFrame({"منتج_المنافس": ["Y", "Z"]})
    cleaned, removed = dedup_missing_vs_matched(sections, missing)
    assert removed == 0 and len(cleaned) == 2


def test_duplicate_check_normalizes_and_excludes_failed() -> None:
    sections = {
        "price_raise": pd.DataFrame({"منتج_المنافس": ["  Oud Royal  ", "❌ فشل"]}),
        "price_lower": pd.DataFrame(),
        "approved": pd.DataFrame(),
    }
    missing = pd.DataFrame({"منتج_المنافس": ["oud royal", "❌ فشل"]})
    count, details = duplicate_check(sections, missing)
    assert count == 1 and details == ["oud royal"]   # ❌ مُستبعد من السعري


def test_reconcile_counts_low_level() -> None:
    report = AuditService().reconcile_counts(
        100, {"price_raise": 40, "price_lower": 30, "approved": 20,
              "review": 5, "excluded": 5},
    )
    assert report.is_balanced and report.gap == 0
