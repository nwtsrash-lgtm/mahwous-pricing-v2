"""ui/components/status_badge.py — شارات الحالة والثقة (عرض خالص، بلا HTML).

دوال خالصة تُعيد (نص، لون، أيقونة) قابلة للاختبار، ودالة عرض رفيعة تستخدم
``st.badge`` الأصلية (لا حقن HTML).
"""
from __future__ import annotations

from conf.constants import COLORS, SECTION_LABELS
from core.enums import ConfidenceLevel, SectionType

# ألوان st.badge المسموحة → نربط ألواننا الدلالية بها.
_CONFIDENCE_BADGE: dict[ConfidenceLevel, tuple[str, str, str]] = {
    ConfidenceLevel.CONFIRMED: ("مؤكَّد", "green", "✅"),
    ConfidenceLevel.REVIEW: ("مراجعة", "orange", "⚠️"),
    ConfidenceLevel.NONE: ("بلا تطابق", "gray", "⚪"),
}
_SECTION_COLOR: dict[SectionType, str] = {
    SectionType.PRICE_RAISE: "red",
    SectionType.PRICE_LOWER: "green",
    SectionType.APPROVED: "green",
    SectionType.MISSING: "blue",
    SectionType.REVIEW: "orange",
    SectionType.EXCLUDED: "gray",
}


def confidence_badge(score: float) -> tuple[str, str, str]:
    """(نص، لون، أيقونة) لمستوى ثقة مشتقّ من الدرجة."""
    return _CONFIDENCE_BADGE[ConfidenceLevel.from_score(score)]


def section_badge(section: SectionType) -> tuple[str, str, str]:
    """(نص، لون، أيقونة) لقسم."""
    return SECTION_LABELS[section], _SECTION_COLOR[section], section.prefix


def render_badge(label: str, color: str, icon: str) -> None:
    """عرض شارة أصلية (بلا HTML)."""
    import streamlit as st

    st.badge(label, color=color, icon=icon)
