"""core/exceptions.py — استثناءات النطاق المخصّصة.

هرم استثناءات بجذر واحد ``PricingError`` يحمل سياقاً اختيارياً للتشخيص.
القاعدة: لا ``except:`` عارية في أي مكان — كل خطأ يُغلَّف باستثناء ذي معنى
وسياق كافٍ لإعادة بناء ما حدث.

أهمها ``DataLossError`` الذي يفرض «قانون حفظ البيانات»:
``sum(sections) == len(all)`` ولا تكرار بين الأقسام.
"""
from __future__ import annotations

from typing import Any


class PricingError(Exception):
    """الجذر لكل أخطاء النظام. يحمل ``context`` للتشخيص."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message: str = message
        self.context: dict[str, Any] = context

    def __str__(self) -> str:
        if self.context:
            return f"{self.message} | context={self.context}"
        return self.message


class ConfigError(PricingError):
    """خطأ في الإعدادات أو الثوابت (مفتاح ناقص، قيمة غير صالحة)."""


class RepositoryError(PricingError):
    """خطأ في طبقة الوصول للبيانات (SQLite / ملفات)."""


class MatchingError(PricingError):
    """خطأ في منطق المطابقة الضبابية (_miss_bare / blocking / skeleton)."""


class ClassificationError(PricingError):
    """خطأ في توزيع المنتجات على الأقسام (_split_results)."""


class MissingDetectionError(PricingError):
    """خطأ في كشف المنتجات المفقودة (_compute_missing_from_store)."""


class AIServiceError(PricingError):
    """خطأ في خدمة الذكاء الاصطناعي (تدوير المفاتيح / batching)."""


class ExportError(PricingError):
    """خطأ في التصدير (Make.com / Salla / CSV / Excel)."""


class DataLossError(ClassificationError):
    """يُرفع عند خرق «قانون حفظ البيانات».

    يُرفع حين ``gap != 0`` (ضياع/تكرار) أو ``duplicate_count != 0``
    (منتج منافس في قسم سعري وفي المفقودات معاً).

    #PRESERVED_LOGIC: حارس ``_reconciliation_check`` (app.py:516) —
    ``gap_ok = gap == 0`` و``duplicate_ok = duplicate_count == 0``.
    """

    def __init__(
        self,
        gap: int,
        all_count: int,
        sum_buckets: int,
        duplicate_count: int = 0,
        **context: Any,
    ) -> None:
        message = (
            f"خرق حفظ البيانات: gap={gap} "
            f"(الكل={all_count}, مجموع الأقسام={sum_buckets}, "
            f"تكرار={duplicate_count})"
        )
        super().__init__(
            message,
            gap=gap,
            all_count=all_count,
            sum_buckets=sum_buckets,
            duplicate_count=duplicate_count,
            **context,
        )
        self.gap: int = gap
        self.all_count: int = all_count
        self.sum_buckets: int = sum_buckets
        self.duplicate_count: int = duplicate_count
