"""services/classification_service.py — توزيع المنتجات على الأقسام (نقل _split_results).

نقل دقيق للجزء النقي من ``_split_results`` (app.py:392): توزيع حصري لكل منتج
على قسم واحد عبر بادئة عمود (القرار)، مع شبكة أمان تُلحق أي منتج غير مُوزَّع
بـ «مستبعد» (لا فقدان بيانات أبداً).

⚠️ كتلة «Smart Reversion» (app.py:410-462) تقرأ/تكتب ``st.session_state`` ⇒
هي شأن طبقة الحالة لا التصنيف النقي، فلا تُنقل هنا (تبقى الخدمة بلا Streamlit).

#PRESERVED_LOGIC: مطابق لمنطق التوزيع وشبكة الأمان وحارس الشفافية (app.py:464-513).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from conf.constants import COL_DECISION, PRICE_LOWER_CONTAINS
from core.enums import SectionType
from core.exceptions import DataLossError
from core.models import ReconciliationReport

# مفاتيح الأقسام الخمسة التي يوزّعها _split_results (المفقود مسار منفصل).
SECTION_KEYS: tuple[str, ...] = (
    "price_raise", "price_lower", "approved", "review", "excluded",
)

# البادئات الحرفية (#PRESERVED_LOGIC app.py:473-477).
_RAISE = SectionType.PRICE_RAISE.prefix      # 🔴
_LOWER = SectionType.PRICE_LOWER.prefix      # 🟢
_APPROVED = SectionType.APPROVED.prefix      # ✅
_REVIEW = SectionType.REVIEW.prefix          # ⚠️
_MISSING = SectionType.MISSING.prefix        # 🔍
_EXCLUDED = SectionType.EXCLUDED.prefix      # ⚪


def split_results(
    df: pd.DataFrame | None, decision_col: str = COL_DECISION,
) -> dict[str, pd.DataFrame]:
    """يوزّع DataFrame على الأقسام الخمسة + ``all``. نقل نقي لـ _split_results."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        empty = pd.DataFrame()
        return {**{k: empty.copy() for k in SECTION_KEYS}, "all": empty.copy()}

    work = df.copy()
    if decision_col not in work.columns:
        work[decision_col] = ""
    work[decision_col] = work[decision_col].fillna("").astype(str).str.strip()
    dec = work[decision_col]

    # ── توزيع حصري بالبادئة (app.py:472-477) ──
    price_raise = work[dec.str.startswith(_RAISE)]
    price_lower = work[
        dec.str.startswith(_LOWER)
        | dec.str.contains(PRICE_LOWER_CONTAINS, na=False, regex=False)
    ]
    approved = work[dec.str.startswith(_APPROVED)]
    review = work[dec.str.startswith(_REVIEW) | dec.str.startswith(_MISSING)]
    excluded = work[dec.str.startswith(_EXCLUDED)]

    # ── شبكة الأمان: أي منتج غير مُوزَّع → «مستبعد» (app.py:479-489) ──
    distributed: set[int] = set()
    for section in (price_raise, price_lower, approved, review, excluded):
        distributed.update(section.index.tolist())
    orphans = work[~work.index.isin(distributed)]
    if not orphans.empty:
        excluded = pd.concat([excluded, orphans], ignore_index=False)

    return {
        "price_raise": price_raise.reset_index(drop=True),
        "price_lower": price_lower.reset_index(drop=True),
        "approved": approved.reset_index(drop=True),
        "review": review.reset_index(drop=True),
        "excluded": excluded.reset_index(drop=True),
        "all": work.reset_index(drop=True),
    }


@dataclass(frozen=True)
class SplitResult:
    """نتيجة التوزيع: الأقسام الخمسة + الكل + أدوات العدّ والتدقيق."""

    sections: dict[str, pd.DataFrame]
    all_df: pd.DataFrame

    def counts(self) -> dict[str, int]:
        """عدد المنتجات في كل قسم."""
        return {k: len(self.sections[k]) for k in SECTION_KEYS}

    @property
    def total_in(self) -> int:
        return len(self.all_df)

    @property
    def total_out(self) -> int:
        return sum(self.counts().values())

    @property
    def gap(self) -> int:
        """الفجوة = الكل − المجموع (يجب أن تساوي صفراً). #PRESERVED_LOGIC app.py:533."""
        return self.total_in - self.total_out

    def report(
        self,
        duplicate_count: int = 0,
        duplicate_details: tuple[str, ...] = (),
    ) -> ReconciliationReport:
        """يبني تقرير تدقيق حفظ البيانات."""
        return ReconciliationReport.from_sections(
            self.total_in, self.counts(), duplicate_count, list(duplicate_details),
        )


class ClassificationService:
    """خدمة التصنيف: تغلّف ``split_results`` وتفرض قانون حفظ البيانات."""

    def __init__(self, decision_col: str = COL_DECISION) -> None:
        self._decision_col = decision_col

    def classify(self, df: pd.DataFrame | None, *, strict: bool = False) -> SplitResult:
        """يوزّع المنتجات. مع ``strict=True`` يرفع DataLossError عند أي فجوة."""
        raw = split_results(df, self._decision_col)
        result = SplitResult({k: raw[k] for k in SECTION_KEYS}, raw["all"])
        if strict and result.gap != 0:
            raise DataLossError(
                gap=result.gap,
                all_count=result.total_in,
                sum_buckets=result.total_out,
            )
        return result
