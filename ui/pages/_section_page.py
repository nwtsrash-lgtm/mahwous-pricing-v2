"""ui/pages/_section_page.py — العارض العام للأقسام (منطق مشترك رفيع).

يوحّد تدفّق: عنوان → فلاتر → ترقيم → بطاقات → إجراءات جماعية، لأقسام
سعر أعلى/أقل/موافق/مراجعة/مستبعد. لا منطق عمل — يستدعي المكوّنات فقط.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from conf.constants import COL_OUR_NAME
from core.enums import ActionType, SectionType
from ui.components.action_bar import render_action_bar
from ui.components.filter_bar import apply_filters, render_filter_bar
from ui.components.pagination import paginate, render_pagination
from ui.components.product_card import render_product_card
from ui.components.status_badge import section_badge
from ui.state_manager import AppState


def section_dataframe(
    sections: dict[str, pd.DataFrame], section: SectionType,
) -> pd.DataFrame:
    """يستخرج DataFrame القسم (المفتاح = قيمة التعداد). خالص وقابل للاختبار."""
    df = sections.get(section.value)
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _handle_bulk(state: AppState, view_df: pd.DataFrame, key: str) -> None:
    """يعالج الإجراء الجماعي (الحذف الناعم محلياً عبر الحالة)."""
    action = render_action_bar(view_df, COL_OUR_NAME, key)
    if action.action is ActionType.HIDE:
        for name in action.keys:
            state.hide(name)


def render_section_page(
    state: AppState,
    sections: dict[str, pd.DataFrame],
    section: SectionType,
    *,
    detailed: bool = False,
    per_page: int = 12,
) -> None:
    """يعرض قسماً كاملاً (غلاف رفيع يستدعي المكوّنات)."""
    import streamlit as st

    key = section.value
    df = section_dataframe(sections, section)
    label, color, icon = section_badge(section)
    st.subheader(f"{icon} {label}")
    if df.empty:
        st.info("لا منتجات في هذا القسم")
        return
    filters = render_filter_bar(df, key)
    view_df = apply_filters(df, filters)
    if filters.active_chips:
        st.caption(" · ".join(filters.active_chips))
    page = int(st.session_state.get(f"{key}_page", 1))
    view = paginate(view_df, page, per_page)
    st.caption(f"{view.caption} (مفلتر من {len(df)})")
    for _, row in view.items.iterrows():
        with st.container(border=True):
            render_product_card(row, detailed=detailed)
    render_pagination(view, key)
    _handle_bulk(state, view.items, key)
