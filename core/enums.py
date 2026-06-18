"""core/enums.py — تعدادات النطاق الأساسية لنظام التسعير الذكي «مهووس».

تعرّف هذه الوحدة المفردات الثابتة التي تعبر كل طبقات المعمارية:
- ``SectionType``   : الأقسام الستة الحصرية لتوزيع المنتجات.
- ``ConfidenceLevel``: مستوى ثقة المطابقة المشتق من درجة التشابه.
- ``ActionType``    : إجراءات المستخدم/النظام على منتج.
- ``ItemType``      : تصنيف المنتج (عطر مقابل غير عطر) المستخدم في فلترة المفقودات.
- ``Gender``        : جنس المنتج المستخدم في حارس تعارض الجنس.

طبقة صفر: لا تستورد أي وحدة داخلية لتجنّب الاستيراد الدائري
(``constants`` و``models`` يستوردان منها، لا العكس).
"""
from __future__ import annotations

from enum import Enum


class SectionType(str, Enum):
    """الأقسام الستة الحصرية. كل منتج يقع في قسم واحد فقط لا غير.

    #PRESERVED_LOGIC: مطابق لمفاتيح القاموس الراجع من ``_split_results``
    (app.py:392) مع إضافة ``MISSING`` المُحسوب في مسار منفصل.
    """

    PRICE_RAISE = "price_raise"  # 🔴 سعرنا أعلى من المنافس
    PRICE_LOWER = "price_lower"  # 🟢 سعرنا أقل من المنافس
    APPROVED = "approved"        # ✅ موافق عليها (السعر مناسب)
    MISSING = "missing"          # 🔍 منتجات منافس ليست في كتالوجنا
    REVIEW = "review"            # ⚠️ تحت المراجعة (ثقة متوسطة)
    EXCLUDED = "excluded"        # ⚪ مستبعد (لا يوجد تطابق)

    @property
    def prefix(self) -> str:
        """البادئة الرمزية التي يبدأ بها نص (القرار) لهذا القسم."""
        return _SECTION_PREFIX[self]


# #PRESERVED_LOGIC: البادئات الحيّة من _split_results (app.py:473-477).
_SECTION_PREFIX: dict[SectionType, str] = {
    SectionType.PRICE_RAISE: "🔴",
    SectionType.PRICE_LOWER: "🟢",
    SectionType.APPROVED: "✅",
    SectionType.MISSING: "🔍",
    SectionType.REVIEW: "⚠️",
    SectionType.EXCLUDED: "⚪",
}


class ConfidenceLevel(str, Enum):
    """مستوى ثقة المطابقة، مشتق من درجة التشابه عبر عتبتي 82/65."""

    CONFIRMED = "confirmed"  # ≥ 82: «نملكه» (إخفاء آمن)
    REVIEW = "review"        # 65..82: «محتمل موجود» (يبقى للمراجعة)
    NONE = "none"            # < 65: لا تطابق

    @classmethod
    def from_score(
        cls,
        score: float,
        confirmed: float = 82.0,
        review: float = 65.0,
    ) -> "ConfidenceLevel":
        """يصنّف درجة تشابه إلى مستوى ثقة.

        #PRESERVED_LOGIC: العتبات الحيّة 82 (CONFIRMED) و65 (REVIEW)
        من config.py:127-128 و app.py:745/894. القيم الافتراضية هنا
        نسخة احتياطية؛ يُمرّر النظام القيم من ``constants`` صراحةً.
        """
        if score >= confirmed:
            return cls.CONFIRMED
        if score >= review:
            return cls.REVIEW
        return cls.NONE


class ActionType(str, Enum):
    """إجراء يتّخذه المستخدم أو النظام على منتج."""

    RAISE = "raise"                # رفع سعرنا
    LOWER = "lower"                # خفض سعرنا
    APPROVE = "approve"            # اعتماد السعر الحالي
    HIDE = "hide"                  # حذف ناعم (soft-delete) قابل للتراجع
    UNHIDE = "unhide"              # تراجع عن الحذف الناعم
    SEND_TO_MAKE = "send_to_make"  # إرسال إلى Make.com
    EXPORT = "export"              # تصدير (Salla/CSV/Excel)
    REVIEW = "review"              # إحالة للمراجعة


class ItemType(str, Enum):
    """تصنيف نوع المنتج المستخدم في فلترة المفقودات.

    #PRESERVED_LOGIC: الفئات غير العطرية تُسقط من المفقودات
    (_compute_missing_from_store، app.py:726).
    """

    PERFUME = "perfume"
    DEODORANT = "deodorant"
    BODY_MIST = "body_mist"
    LOTION = "lotion"
    SOAP = "soap"
    GEL = "gel"
    GIFT_SET = "gift_set"
    SAMPLE = "sample"
    TESTER = "tester"
    UNKNOWN = "unknown"

    @property
    def is_perfume(self) -> bool:
        """هل المنتج عطر حقيقي يُحتسب في المفقودات؟"""
        return self is ItemType.PERFUME

    @property
    def droppable_from_missing(self) -> bool:
        """هل يُسقَط هذا النوع من قائمة المفقودات (غير عطر / طقم / عيّنة)؟"""
        return self in _DROPPABLE_TYPES


_DROPPABLE_TYPES: frozenset[ItemType] = frozenset({
    ItemType.DEODORANT, ItemType.BODY_MIST, ItemType.LOTION,
    ItemType.SOAP, ItemType.GEL, ItemType.GIFT_SET,
    ItemType.SAMPLE, ItemType.TESTER,
})


class Gender(str, Enum):
    """جنس المنتج. يُستخدم في حارس تعارض الجنس عند المطابقة."""

    MALE = "male"
    FEMALE = "female"
    UNISEX = "unisex"
    UNKNOWN = "unknown"

    @property
    def is_explicit(self) -> bool:
        """هل الجنس محدّد صراحةً (ذكر/أنثى) لا مجهول/للجنسين؟

        #PRESERVED_LOGIC: حارس الجنس يعمل فقط حين يكون لكلا المنتجين
        جنس صريح ومختلف ⇒ لا يُخفى أبداً، يُحال للمراجعة (الشرط في app.py).
        """
        return self in (Gender.MALE, Gender.FEMALE)
