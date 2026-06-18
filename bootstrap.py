"""bootstrap.py — جذر التركيب (DI Container) + منسّق التحليل.

يبني حاوية تحقن الخدمات عديمة الحالة (تصنيف/تسعير/تدقيق/AI/تصدير) مرة واحدة،
ويوفّر مصانع للخدمات المرتبطة بالكتالوج (مطابقة/مفقودات). لا Streamlit هنا
(يبقى الاستيراد العلوي لـ Streamlit حصراً في app.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

import pandas as pd

from conf.settings import Settings
from core.enums import SectionType
from core.models import AnalysisResult
from infrastructure.db_manager import DatabaseManager
from services.audit_service import AuditService
from services.ai_service import AIService
from services.classification_service import ClassificationService, SplitResult
from services.export_service import ExportService
from services.matching_service import MatchingService
from services.missing_service import MissingService
from services.pricing_service import PricingService


@dataclass(frozen=True)
class Container:
    """حاوية الاعتماديات: خدمات مفردة + مصانع مرتبطة بالكتالوج."""

    settings: Settings
    db: DatabaseManager
    classification: ClassificationService
    pricing: PricingService
    audit: AuditService
    ai: AIService
    export: ExportService

    def matching_for(self, our_names: Iterable[str]) -> MatchingService:
        """يبني خدمة مطابقة لكتالوجنا الحالي."""
        return MatchingService(list(our_names))

    def missing_for(self, matching: MatchingService) -> MissingService:
        """يبني خدمة مفقودات تعتمد على خدمة مطابقة جاهزة."""
        return MissingService(matching)


def build_container(settings: Optional[Settings] = None) -> Container:
    """يهيّئ الحاوية بالكامل (المدخل الوحيد لتركيب النظام)."""
    settings = settings or Settings.load()
    return Container(
        settings=settings,
        db=DatabaseManager(settings.db_path),
        classification=ClassificationService(),
        pricing=PricingService(),
        audit=AuditService(),
        ai=AIService(settings=settings),
        export=ExportService(),
    )


def run_analysis(
    container: Container,
    results_df: pd.DataFrame,
    *,
    our_names: Optional[Iterable[str]] = None,
    missing_candidates: Optional[list[dict[str, Any]]] = None,
) -> tuple[AnalysisResult, SplitResult, Optional[pd.DataFrame]]:
    """يشغّل: تصنيف → (مفقودات اختيارية) → تدقيق. يفرض قانون حفظ البيانات.

    يُعيد (نتيجة التحليل، التوزيع، DataFrame المفقودات أو None).
    يرفع ``DataLossError`` إذا اختلّ التوازن (gap≠0 أو تكرار≠0).
    """
    split = container.classification.classify(results_df)
    missing_df: Optional[pd.DataFrame] = None
    missing_count = 0
    if our_names is not None and missing_candidates:
        matching = container.matching_for(our_names)
        rows = container.missing_for(matching).compute(missing_candidates)
        missing_df = MissingService.to_dataframe(rows)
        missing_count = len(missing_df)
    report = container.audit.reconcile(split, missing_df)
    counts = {SectionType(key): value for key, value in split.counts().items()}
    counts[SectionType.MISSING] = missing_count
    result = AnalysisResult(
        section_counts=counts,
        total=split.total_in,
        missing_count=missing_count,
        reconciliation=report,
    )
    result.assert_conservation()  # يرفع DataLossError عند أي خرق
    return result, split, missing_df
