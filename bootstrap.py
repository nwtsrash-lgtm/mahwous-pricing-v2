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
from services.scraper_service import ScraperService


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
    scraper: ScraperService

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
        scraper=ScraperService(),
    )


def run_missing_analysis(
    container: Container,
    our_df: pd.DataFrame,
    *,
    use_cache: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """يشغّل كشف المفقودات الحقيقي ضدّ قاعدة المنافسين (~129K) مع كاش F4v2.

    يُعيد (DataFrame المفقودات، إحصاءات). #PRESERVED_LOGIC: مسار
    _compute_missing_from_store (مرشّحون من CompetitorIntelligence ثم تصنيف).
    """
    import os
    import sys

    from conf.constants import COMPETITOR_DB_PATH, MISSING_CACHE_PATH, PROJECT_ROOT
    from services.catalog_service import name_column
    from services.missing_service import (
        MissingService,
        load_cache,
        missing_signature,
        save_cache,
    )

    db_path = str(COMPETITOR_DB_PATH)
    cache_path = str(MISSING_CACHE_PATH)
    signature = (
        missing_signature(len(our_df), os.path.getsize(db_path))
        if os.path.exists(db_path) else ""
    )
    if use_cache:
        cached = load_cache(cache_path, signature)
        if cached is not None:
            return cached, {"cached": True, "rows": len(cached)}
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"قاعدة المنافسين غير موجودة: {db_path}")
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from engines.competitor_intelligence import CompetitorIntelligence  # type: ignore

    ncol = name_column(our_df)
    names = our_df[ncol].dropna().astype(str)
    names = names[names.str.strip() != ""].tolist()
    matching = container.matching_for(names)
    candidates, _total = CompetitorIntelligence(db_path=db_path).find_missing_products(
        our_df, page=0, per_page=1_000_000,
    )
    rows = container.missing_for(matching).compute(candidates)
    missing_df = MissingService.to_dataframe(rows)
    if use_cache and signature:
        save_cache(cache_path, signature, missing_df)
    green = int((missing_df.get("مستوى_الثقة") == "green").sum()) if not missing_df.empty else 0
    return missing_df, {
        "cached": False, "rows": len(missing_df),
        "candidates": len(candidates), "our_products": len(names),
        "confirmed_missing": green, "review": len(missing_df) - green,
    }


def load_competitor_dfs(db_path: str) -> dict[str, pd.DataFrame]:
    """يحمّل منافسي القاعدة (~129K) كـ ``{اسم المتجر: DataFrame}`` للتحليل السعري.

    #PRESERVED_LOGIC: مطابق لمسار تحميل DB في app.py:4159-4183 — يقرأ
    ``competitor_products_store`` (السعر>0) ثم يعيد تسمية الأعمدة لما يتوقّعه
    المحرّك (product_name→المنتج، price→السعر، image_url→صورة المنتج،
    product_url→رابط المنتج، competitor→المنافس) ويجمّع حسب المتجر.
    """
    import os
    import sqlite3

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"قاعدة المنافسين غير موجودة: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT * FROM competitor_products_store WHERE price > 0", conn,
        )
    finally:
        conn.close()
    rename = {
        "product_name": "المنتج",
        "price": "السعر",
        "image_url": "صورة المنتج",
        "product_url": "رابط المنتج",
        "competitor": "المنافس",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    group_col = "المنافس" if "المنافس" in df.columns else "competitor"
    if group_col not in df.columns:
        return {"كل المنتجات": df.reset_index(drop=True)}
    return {
        str(name): group.reset_index(drop=True)
        for name, group in df.groupby(group_col, sort=False)
    }


def run_pricing_analysis(
    container: Container,
    our_df: pd.DataFrame,
    *,
    use_ai: Optional[bool] = None,
    use_cache: bool = True,
    missing_df: Optional[pd.DataFrame] = None,
) -> tuple[dict[str, pd.DataFrame], AnalysisResult, Optional[pd.DataFrame], dict[str, Any]]:
    """التحليل السعري الكامل: المحرّك → تصنيف → تنقية المفقودات → تدقيق → نتيجة.

    يشغّل ``engines.engine.run_full_analysis(our_df, comp_dfs)`` (مع كاش قرصي
    للمخرجات الثقيلة)، يصنّف النتيجة، **يزيل من المفقودات أي منافس مطابَق سعرياً**
    (مصدر الحقيقة الحاسم — app.py:1184-1222)، ثم يدقّق. يُعيد:
    (أقسام جاهزة للعرض، AnalysisResult مع تقرير التدقيق، المفقودات بعد التنقية، إحصاءات).

    ``use_ai=None`` ⇒ يُفعَّل تلقائياً فقط إن وُجدت مفاتيح ذكاء اصطناعي
    (النطاق الرمادي 60–84% بلا مفاتيح ⇒ مراجعة، بلا تكلفة). #PRESERVED_LOGIC.
    """
    import os
    import sys

    from conf.constants import (
        COMPETITOR_DB_PATH,
        PRICING_CACHE_PATH,
        PRICING_CACHE_VERSION,
        PROJECT_ROOT,
    )
    from core.enums import SectionType
    from services.missing_service import load_cache, save_cache

    if use_ai is None:
        use_ai = container.settings.any_ai_configured

    db_path = str(COMPETITOR_DB_PATH)
    cache_path = str(PRICING_CACHE_PATH)
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    signature = f"{PRICING_CACHE_VERSION}|{len(our_df)}|{db_size}|{int(bool(use_ai))}"

    results_df: Optional[pd.DataFrame] = (
        load_cache(cache_path, signature) if use_cache else None
    )
    audit_stats: dict[str, Any] = {}
    if results_df is not None:
        audit_stats = {"cached": True, "rows": len(results_df)}
    else:
        comp_dfs = load_competitor_dfs(db_path)
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from engines.engine import run_full_analysis  # type: ignore  #PRESERVED_LOGIC

        results_df, audit_stats = run_full_analysis(
            our_df, comp_dfs, use_ai=bool(use_ai),
        )
        audit_stats = dict(audit_stats or {})
        audit_stats["cached"] = False
        audit_stats["competitors"] = len(comp_dfs)
        if use_cache and signature:
            save_cache(cache_path, signature, results_df)

    # ── تصنيف ثم تنقية المفقودات من المطابَق سعرياً (مصدر الحقيقة الحاسم) ──
    from services.audit_service import dedup_missing_vs_matched

    split = container.classification.classify(results_df)
    missing_clean, removed = dedup_missing_vs_matched(split.sections, missing_df)
    report = container.audit.reconcile(split, missing_clean)

    counts = {SectionType(key): value for key, value in split.counts().items()}
    missing_confirmed = 0
    if (missing_clean is not None and not missing_clean.empty
            and "مستوى_الثقة" in missing_clean.columns):
        missing_confirmed = int((missing_clean["مستوى_الثقة"] == "green").sum())
    counts[SectionType.MISSING] = missing_confirmed

    result = AnalysisResult(
        section_counts=counts,
        total=split.total_in,
        missing_count=missing_confirmed,
        reconciliation=report,
    )
    audit_stats["missing_deduped"] = removed
    return split.sections, result, missing_clean, audit_stats


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
