"""ui/pages/missing.py — قسم 🔍 منتجات مفقودة (الأكثر تعقيداً، غلاف رفيع).

يفصل «مؤكد مفقود (green)» عن «تحت المراجعة (review)»، يتيح تحقّق AI للمراجعة،
ويجهّز تصدير سلة. كل العمل في الخدمات المحقونة — لا منطق هنا.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from conf.constants import COL_COMP_NAME
from ui.components.action_bar import render_action_bar
from ui.components.pagination import paginate, render_pagination
from ui.components.product_card import render_product_card
from core.enums import ActionType
from ui.state_manager import AppState

_CONF_COL = "مستوى_الثقة"


def split_missing(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """يفصل (مؤكد مفقود green، تحت المراجعة review). خالص وقابل للاختبار."""
    if df is None or df.empty or _CONF_COL not in df.columns:
        empty = pd.DataFrame()
        return (df if isinstance(df, pd.DataFrame) else empty), empty
    level = df[_CONF_COL].astype(str)
    return df[level == "green"], df[level == "review"]


def _render_review(review_df: pd.DataFrame, ai_service: Optional[Any]) -> None:
    """قسم المراجعة + زرّ تحقّق AI (لا حذف صامت عند فشل AI)."""
    import streamlit as st

    if review_df.empty:
        return
    st.warning(f"⚠️ {len(review_df)} منتجاً بحاجة تأكيد")
    if st.button("🤖 تحقّق AI من المراجعة", disabled=ai_service is None):
        st.session_state["_missing_ai_requested"] = True


def _render_export(green_df: pd.DataFrame, export_service: Optional[Any]) -> None:
    """تجهيز تصدير سلة (CSV/XLSX) للمفقودات المؤكدة."""
    import streamlit as st

    if green_df.empty or export_service is None:
        return
    csv_text = export_service.to_csv(green_df)
    st.download_button("📥 تنزيل CSV", csv_text, "missing.csv", "text/csv")


def render(
    state: AppState,
    missing_df: pd.DataFrame,
    *,
    ai_service: Optional[Any] = None,
    export_service: Optional[Any] = None,
) -> None:
    """يعرض قسم المفقودات كاملاً."""
    import streamlit as st

    st.header("🔍 منتجات مفقودة")
    if missing_df is None or missing_df.empty:
        st.info("لا منتجات مفقودة")
        return
    green_df, review_df = split_missing(missing_df)
    st.caption(f"مؤكد مفقود: {len(green_df)} · تحت المراجعة: {len(review_df)}")
    _render_review(review_df, ai_service)
    _render_export(green_df, export_service)
    page = int(st.session_state.get("missing_page", 1))
    view = paginate(missing_df, page, per_page=12)
    st.caption(view.caption)
    for _, row in view.items.iterrows():
        with st.container(border=True):
            render_product_card(row, detailed=True)
    render_pagination(view, "missing")
    action = render_action_bar(view.items, COL_COMP_NAME, "missing")
    if action.action is ActionType.HIDE:
        for name in action.keys:
            state.hide(name)
