"""ui/components/action_bar.py — شريط الإجراءات الجماعية (منطق خالص + fragment).

``page_keys`` و``BulkAction`` خالصة قابلة للاختبار؛ العرض ملفوف بـ
``st.fragment`` لتحديث حيّ دون إعادة تشغيل الصفحة كاملة (لا st.rerun في المنطق).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from core.enums import ActionType


@dataclass(frozen=True)
class BulkAction:
    """نتيجة شريط الإجراءات: الإجراء المختار + المفاتيح المستهدفة."""

    action: Optional[ActionType] = None
    keys: list[str] = field(default_factory=list)


def page_keys(view_df: pd.DataFrame, key_col: str) -> list[str]:
    """مفاتيح صفوف الصفحة الحالية (خالص)."""
    if not isinstance(view_df, pd.DataFrame) or view_df.empty \
            or key_col not in view_df.columns:
        return []
    return [str(v).strip() for v in view_df[key_col].tolist() if str(v).strip()]


def render_action_bar(view_df: pd.DataFrame, key_col: str, key: str) -> BulkAction:
    """يعرض إجراءات جماعية (تحديد الكل + حذف/تصدير/إرسال) ويعيد الاختيار."""
    import streamlit as st

    @st.fragment
    def _bar() -> None:
        keys = page_keys(view_df, key_col)
        select_all = st.checkbox(f"تحديد الكل ({len(keys)})", key=f"{key}_all")
        targets = keys if select_all else []
        cols = st.columns(3)
        if cols[0].button("🗑️ حذف ناعم", key=f"{key}_del", disabled=not targets):
            st.session_state[f"{key}_result"] = BulkAction(ActionType.HIDE, targets)
        if cols[1].button("📤 تصدير", key=f"{key}_exp", disabled=not targets):
            st.session_state[f"{key}_result"] = BulkAction(ActionType.EXPORT, targets)
        if cols[2].button("⚡ إرسال Make", key=f"{key}_make", disabled=not targets):
            st.session_state[f"{key}_result"] = BulkAction(
                ActionType.SEND_TO_MAKE, targets)

    _bar()
    return _read_result(key)


def _read_result(key: str) -> BulkAction:
    """يقرأ نتيجة الإجراء من الحالة (يُستهلك مرة)."""
    import streamlit as st

    result = st.session_state.pop(f"{key}_result", None)
    return result if isinstance(result, BulkAction) else BulkAction()
