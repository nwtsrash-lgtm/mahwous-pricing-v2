"""ui/pages/processed.py — ✅ تمت المعالجة (سجلّ + تراجع + ملخص AI).

غلاف رفيع: يعرض المعالَجة، يتيح التراجع (إلغاء الحذف الناعم/المعالجة)،
وملخصاً إدارياً عبر خدمة AI محقونة.
"""
from __future__ import annotations

from typing import Any, Optional

from ui.state_manager import AppState

_SUMMARY_SYSTEM = "أنت مدير عمليات متجر. لخّص الإجراءات في 3 نقاط محفّزة موجزة."


def processed_rows(state: AppState, limit: int = 50) -> list[str]:
    """قائمة المعرّفات المعالَجة (خالص، مقصوصة)."""
    return sorted(state.processed_price_skus)[:limit]


def _undo(state: AppState, sku: str) -> None:
    """رد نداء التراجع عن معالجة منتج."""
    state.processed_price_skus.discard(sku)


def _render_summary(state: AppState, ai_service: Optional[Any]) -> None:
    """ملخص إداري عبر AI (اختياري)."""
    import streamlit as st

    if ai_service is None or not state.processed_price_skus:
        return
    if st.button("📝 ملخص AI للإجراءات"):
        prompt = "الإجراءات المنفّذة: " + "، ".join(processed_rows(state, 30))
        result = ai_service.call(prompt, _SUMMARY_SYSTEM)
        (st.info if result.success else st.warning)(
            result.response or "تعذّر توليد الملخص")


def render(state: AppState, *, ai_service: Optional[Any] = None) -> None:
    """يعرض صفحة المعالَجة كاملة."""
    import streamlit as st

    st.header("✅ تمت المعالجة")
    rows = processed_rows(state)
    st.caption(f"عدد المعالَج: {len(state.processed_price_skus)}")
    if not rows:
        st.info("لا منتجات معالَجة بعد")
        return
    for sku in rows:
        left, right = st.columns([4, 1])
        left.write(f"🔖 {sku}")
        right.button("↩️ تراجع", key=f"undo_{sku}",
                     on_click=_undo, args=(state, sku))
    _render_summary(state, ai_service)
