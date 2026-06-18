"""core/models.py — نماذج Pydantic v2 لكل البيانات العابرة بين الطبقات.

كل نموذج مربوط بأسماء الأعمدة العربية الحيّة عبر ``alias`` (مع
``populate_by_name=True``) كي يُبنى مباشرةً من صفوف DataFrame دون إعادة تسمية.
نماذج القيمة ``frozen=True`` (غير قابلة للتغيير) لمنع التعديل العرَضي عبر الطبقات.

#PRESERVED_LOGIC: أسماء الأعمدة مأخوذة حرفياً من app.py (لا تُغيَّر — يعتمدها
تصدير Make.com وSalla). انظر config/constants.py للمصدر الموحّد للأسماء.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.enums import ActionType, ConfidenceLevel, Gender, ItemType, SectionType
from core.exceptions import DataLossError


def _to_float(value: Any) -> float:
    """تحويل آمن إلى ``float`` يكافئ ``pd.to_numeric(errors='coerce').fillna(0)``."""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class Product(BaseModel):
    """منتجنا من الكتالوج. حقوله مربوطة بأعمدة app.py العربية."""

    model_config = ConfigDict(
        populate_by_name=True, frozen=True, str_strip_whitespace=True,
    )

    product_id: str = Field(alias="معرف_المنتج")
    name: str = Field(alias="المنتج")
    price: float = Field(default=0.0, alias="السعر")
    brand: Optional[str] = Field(default=None, alias="الماركة")
    size_ml: Optional[float] = Field(default=None, alias="الحجم")
    gender_text: Optional[str] = Field(default=None, alias="الجنس")
    type_text: Optional[str] = Field(default=None, alias="النوع")
    image: Optional[str] = Field(default=None, alias="الصورة")
    link: Optional[str] = Field(default=None, alias="الرابط")

    @field_validator("price", mode="before")
    @classmethod
    def _coerce_price(cls, v: Any) -> float:
        return _to_float(v)


class CompetitorProduct(BaseModel):
    """منتج منافس من متجر خارجي."""

    model_config = ConfigDict(
        populate_by_name=True, frozen=True, str_strip_whitespace=True,
    )

    store: str = Field(alias="المنافس")
    name: str = Field(alias="منتج_المنافس")
    price: float = Field(default=0.0, alias="سعر_المنافس")
    competitor_id: Optional[str] = Field(default=None, alias="معرف_المنافس")
    image: Optional[str] = Field(default=None, alias="صورة_المنافس")
    link: Optional[str] = Field(default=None, alias="رابط_المنافس")
    size: Optional[str] = Field(default=None, alias="الحجم")
    type_text: Optional[str] = Field(default=None, alias="النوع")

    @field_validator("price", mode="before")
    @classmethod
    def _coerce_price(cls, v: Any) -> float:
        return _to_float(v)


class CompetitorDetail(BaseModel):
    """سطر تفصيلي واحد داخل عمود ``تفاصيل_المنافسين``.

    #PRESERVED_LOGIC: المفاتيح مطابقة حرفياً لـ ``_build_from_old``
    (app.py:694-705) كي يبقى التوافق الخلفي مع البيانات المخزّنة.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    competitor: str = Field(default="", alias="المنافس")
    name: str = Field(default="", alias="اسم_المنتج")
    price: float = Field(default=0.0, alias="السعر")
    image: str = Field(default="", alias="الصورة")
    link: str = Field(default="", alias="الرابط")
    size: str = Field(default="", alias="الحجم")
    type_text: str = Field(default="", alias="النوع")
    detail_id: str = Field(default="", alias="المعرف")

    @field_validator("price", mode="before")
    @classmethod
    def _coerce_price(cls, v: Any) -> float:
        return _to_float(v)


class MatchResult(BaseModel):
    """نتيجة مطابقة منتجنا بمنتج منافس + القرار المشتق."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    product_id: str = Field(alias="معرف_المنتج")
    our_name: str = Field(alias="المنتج")
    our_price: float = Field(default=0.0, alias="السعر")
    competitor_store: Optional[str] = Field(default=None, alias="المنافس")
    competitor_name: Optional[str] = Field(default=None, alias="منتج_المنافس")
    competitor_price: float = Field(default=0.0, alias="سعر_المنافس")
    match_ratio: float = Field(default=0.0, alias="نسبة_التطابق")
    confidence_text: Optional[str] = Field(default=None, alias="مستوى_الثقة")
    decision: str = Field(default="", alias="القرار")
    reason: Optional[str] = Field(default=None, alias="السبب")
    section: Optional[SectionType] = Field(default=None)
    gender: Gender = Field(default=Gender.UNKNOWN)

    @field_validator("our_price", "competitor_price", "match_ratio", mode="before")
    @classmethod
    def _coerce_numbers(cls, v: Any) -> float:
        return _to_float(v)

    @property
    def confidence_level(self) -> ConfidenceLevel:
        """مستوى الثقة المشتق من ``match_ratio`` بعتبات النظام."""
        return ConfidenceLevel.from_score(self.match_ratio)


class PriceDiff(BaseModel):
    """فرق السعر بيننا وبين المنافس + السعر المقترح والإجراء."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    product_id: str = Field(alias="معرف_المنتج")
    our_price: float = Field(default=0.0, alias="السعر")
    competitor_price: float = Field(default=0.0, alias="سعر_المنافس")
    diff: float = Field(default=0.0, alias="الفرق")
    diff_pct: float = Field(default=0.0)
    suggested_price: Optional[float] = Field(default=None, alias="السعر_المقترح")
    action: Optional[ActionType] = Field(default=None)

    @field_validator(
        "our_price", "competitor_price", "diff", "diff_pct", mode="before",
    )
    @classmethod
    def _coerce_numbers(cls, v: Any) -> float:
        return _to_float(v)


class MissingProduct(BaseModel):
    """منتج منافس مفقود من كتالوجنا (مرشّح للإضافة)."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    store: str = Field(alias="المنافس")
    name: str = Field(alias="منتج_المنافس")
    price: float = Field(default=0.0, alias="سعر_المنافس")
    size: Optional[str] = Field(default=None, alias="الحجم")
    type_text: Optional[str] = Field(default=None, alias="النوع")
    link: Optional[str] = Field(default=None, alias="رابط_المنافس")
    image: Optional[str] = Field(default=None, alias="صورة_المنافس")
    competitor_id: Optional[str] = Field(default=None, alias="معرف_المنافس")
    details: list[CompetitorDetail] = Field(
        default_factory=list, alias="تفاصيل_المنافسين",
    )
    count: int = Field(default=1, alias="عدد_المنافسين")
    min_price: Optional[float] = Field(default=None, alias="أقل_سعر")
    max_price: Optional[float] = Field(default=None, alias="أعلى_سعر")
    avg_price: Optional[float] = Field(default=None, alias="متوسط_السعر")

    @field_validator("price", mode="before")
    @classmethod
    def _coerce_price(cls, v: Any) -> float:
        return _to_float(v)


class ReconciliationReport(BaseModel):
    """تقرير مدقّق حفظ البيانات بين الأقسام.

    #PRESERVED_LOGIC: مطابق لمخرجات ``_reconciliation_check`` (app.py:516)
    — ``gap`` و``gap_ok`` و``duplicate_count`` و``duplicate_ok``.
    """

    model_config = ConfigDict(frozen=True)

    all_count: int
    sum_buckets: int
    bucket_counts: dict[str, int] = Field(default_factory=dict)
    duplicate_count: int = 0
    duplicate_details: list[str] = Field(default_factory=list)

    @property
    def gap(self) -> int:
        """الفجوة = عدد الكل − مجموع الأقسام (يجب أن تساوي صفراً)."""
        return self.all_count - self.sum_buckets

    @property
    def gap_ok(self) -> bool:
        return self.gap == 0

    @property
    def duplicate_ok(self) -> bool:
        return self.duplicate_count == 0

    @property
    def is_balanced(self) -> bool:
        """هل النظام متوازن (لا ضياع ولا تكرار)؟"""
        return self.gap_ok and self.duplicate_ok

    def assert_balanced(self) -> None:
        """يرفع ``DataLossError`` إذا اختلّ قانون حفظ البيانات."""
        if not self.is_balanced:
            raise DataLossError(
                gap=self.gap,
                all_count=self.all_count,
                sum_buckets=self.sum_buckets,
                duplicate_count=self.duplicate_count,
                duplicate_details=self.duplicate_details[:10],
            )

    @classmethod
    def from_sections(
        cls,
        all_count: int,
        bucket_counts: dict[str, int],
        duplicate_count: int = 0,
        duplicate_details: Optional[list[str]] = None,
    ) -> "ReconciliationReport":
        """يبني التقرير من عدّ الكل وعدّ كل قسم."""
        return cls(
            all_count=all_count,
            sum_buckets=sum(bucket_counts.values()),
            bucket_counts=dict(bucket_counts),
            duplicate_count=duplicate_count,
            duplicate_details=list(duplicate_details or []),
        )


class AnalysisResult(BaseModel):
    """نتيجة تحليل كاملة: عدّ كل قسم + تقرير التدقيق (نموذج تجميعي قابل للتعديل)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    section_counts: dict[SectionType, int] = Field(default_factory=dict)
    total: int = 0
    missing_count: int = 0
    reconciliation: Optional[ReconciliationReport] = None
    job_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)

    def assert_conservation(self) -> None:
        """يطبّق حارس حفظ البيانات إن توفّر تقرير تدقيق."""
        if self.reconciliation is not None:
            self.reconciliation.assert_balanced()
