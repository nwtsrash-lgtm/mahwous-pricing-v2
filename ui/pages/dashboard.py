"""ui/pages/dashboard.py — لوحة التحكم (رفع + تحليل + مؤشرات + بطاقات أقسام).

غلاف رفيع: المنطق في الخدمات، والتحليل عبر رد نداء محقون (``on_analyze``).
دوال الحالة/المؤشرات خالصة وقابلة للاختبار.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from conf.constants import SECTION_LABELS
from ui.state_manager import AppState


def conservation_status(report: Any) -> tuple[bool, str]:
    """حالة شريط حفظ البيانات (أخضر إن لا ضياع ولا تكرار)."""
    if report is None:
        return True, "ℹ️ لم يُجرَ تحليل بعد"
    if report.is_balanced:
        return True, "✅ حفظ البيانات سليم (لا ضياع ولا تكرار)"
    return False, f"❌ خلل: فجوة={report.gap} · تكرار={report.duplicate_count}"


def kpi_metrics(result: Any) -> list[tuple[str, int]]:
    """مؤشرات الأقسام من نتيجة التحليل (خالص)."""
    counts = getattr(result, "section_counts", None)
    if not counts:
        return []
    return [(SECTION_LABELS[section], count) for section, count in counts.items()]


def _render_upload(state: AppState, on_analyze: Optional[Callable[..., Any]]) -> None:
    """منطقة الرفع + زرّ التحليل."""
    import streamlit as st

    uploaded = st.file_uploader("📤 ارفع كتالوجنا (CSV/Excel)", type=["csv", "xlsx"])
    can_run = uploaded is not None and on_analyze is not None
    if st.button("🚀 ابدأ التحليل", disabled=not can_run or state.is_analysis_running):
        on_analyze(uploaded)  # type: ignore[misc]


def _render_banner(state: AppState) -> None:
    """شريط حفظ البيانات (أخضر/أحمر)."""
    import streamlit as st

    report = getattr(state.analysis_results, "reconciliation", None)
    ok, text = conservation_status(report)
    (st.success if ok else st.error)(text)


def _render_kpis(state: AppState) -> None:
    """بطاقات مؤشرات الأقسام."""
    import streamlit as st

    metrics = kpi_metrics(state.analysis_results)
    if not metrics:
        return
    columns = st.columns(len(metrics))
    for column, (label, count) in zip(columns, metrics):
        column.metric(label, count)


def render(state: AppState, *, on_analyze: Optional[Callable[..., Any]] = None) -> None:
    """يعرض لوحة التحكم كاملة."""
    import streamlit as st

    st.header("📊 لوحة التحكم")
    _render_upload(state, on_analyze)
    _render_banner(state)
    _render_kpis(state)
