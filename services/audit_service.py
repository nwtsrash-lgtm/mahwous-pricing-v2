"""services/audit_service.py — مدقّق حفظ البيانات (نقل _reconciliation_check).

نقل دقيق لمدقّق التسوية (app.py:516-614):
  1) الفجوة: ``len(all) == مجموع الأقسام الخمسة`` (لا ضياع).
  2) التكرار: لا منتج منافس في قسم سعري وفي المفقودات معاً (تطابق بالاسم المطبّع).

#PRESERVED_LOGIC: يبني ``ReconciliationReport`` الذي يفرض القانون عبر
``assert_balanced`` (يرفع DataLossError عند gap≠0 أو تكرار≠0).
"""
from __future__ import annotations

import pandas as pd

from conf.constants import COL_COMP_NAME
from core.models import ReconciliationReport
from services.classification_service import SECTION_KEYS, SplitResult

# الأقسام السعرية التي تُفحص ضد المفقودات (app.py:541).
_PRICE_SECTIONS = ("price_raise", "price_lower", "approved")


def _normalized_keys(
    df: pd.DataFrame, *, exclude_failed: bool = False, col: str = COL_COMP_NAME,
) -> set[str]:
    """مفاتيح أسماء المنافسين مطبّعة (strip+lower) مع تنقية الفراغ/nan."""
    if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
        return set()
    series = df[col].fillna("").astype(str).str.strip().str.lower()
    series = series[(series != "") & (series != "nan")]
    if exclude_failed:
        series = series[~series.str.startswith("❌")]
    return set(series.tolist())


def dedup_missing_vs_matched(
    sections: dict[str, pd.DataFrame], missing_df: pd.DataFrame | None,
) -> tuple[pd.DataFrame | None, int]:
    """يزيل من المفقودات أي منتج منافس مطابَق في قسم سعري (مصدر الحقيقة الحاسم).

    #PRESERVED_LOGIC: نقل دقيق لـ ``_dedup_missing_vs_matched`` (app.py:1184-1222)
    — تُستدعى في كل مسارات التحليل قبل التدقيق: المطابقة السعرية حاسمة، فأي منافس
    وقع في 🔴/🟢/✅ (عدا ❌) لا يُعدّ مفقوداً. يُعيد (المفقودات بعد التنقية، عدد المُزال).
    """
    if (not isinstance(missing_df, pd.DataFrame) or missing_df.empty
            or COL_COMP_NAME not in missing_df.columns):
        return missing_df, 0
    matched: set[str] = set()
    for key in _PRICE_SECTIONS:
        matched |= _normalized_keys(sections.get(key, pd.DataFrame()), exclude_failed=True)
    if not matched:
        return missing_df, 0
    keys = missing_df[COL_COMP_NAME].fillna("").astype(str).str.strip().str.lower()
    keep = ~keys.isin(matched)
    removed = int((~keep).sum())
    if removed == 0:
        return missing_df, 0
    return missing_df[keep].reset_index(drop=True), removed


def duplicate_check(
    sections: dict[str, pd.DataFrame], missing_df: pd.DataFrame | None,
) -> tuple[int, list[str]]:
    """عدد المنتجات المكرّرة بين الأقسام السعرية والمفقودات. #PRESERVED_LOGIC app.py:535-580."""
    if missing_df is None:
        return 0, []
    price_keys: set[str] = set()
    for key in _PRICE_SECTIONS:
        price_keys |= _normalized_keys(sections.get(key, pd.DataFrame()), exclude_failed=True)
    missing_keys = _normalized_keys(missing_df, exclude_failed=False)
    overlap = price_keys & missing_keys
    return len(overlap), sorted(overlap)[:10]


class AuditService:
    """خدمة التدقيق: تنتج تقرير حفظ بيانات وتفرضه."""

    def reconcile(
        self, split_result: SplitResult, missing_df: pd.DataFrame | None = None,
    ) -> ReconciliationReport:
        """يبني تقرير التسوية من نتيجة التوزيع + المفقودات."""
        dup_count, dup_details = duplicate_check(split_result.sections, missing_df)
        return split_result.report(dup_count, tuple(dup_details))

    def reconcile_counts(
        self,
        all_count: int,
        bucket_counts: dict[str, int],
        duplicate_count: int = 0,
        duplicate_details: tuple[str, ...] = (),
    ) -> ReconciliationReport:
        """نسخة منخفضة المستوى للتسوية من الأعداد مباشرةً."""
        counts = {k: int(bucket_counts.get(k, 0)) for k in SECTION_KEYS}
        return ReconciliationReport.from_sections(
            all_count, counts, duplicate_count, list(duplicate_details),
        )

    def assert_conserved(self, report: ReconciliationReport) -> None:
        """يفرض القانون: يرفع DataLossError عند أي خرق."""
        report.assert_balanced()
