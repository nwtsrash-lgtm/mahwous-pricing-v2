"""app.py — موجّه «مهووس v2» (Router + DI فقط).

≤150 سطراً، بلا منطق عمل. الملف الوحيد الذي يستورد Streamlit علوياً؛
كل وحدة أخرى تستورده كسولاً. الموجّه: شريط جانبي يوزّع على صفحات رفيعة،
وحاوية اعتماديات تحقن الخدمات. لا st.rerun خارج fragment.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st  # الاستيراد العلوي الوحيد لـ Streamlit في المشروع كله

from bootstrap import Container, build_container, run_analysis
from core.exceptions import DataLossError
from ui.pages import (
    approved,
    dashboard,
    excluded,
    missing,
    price_lower,
    price_raise,
    processed,
    review,
)
from ui.state_manager import AppState, StateStore, StreamlitStore

st.set_page_config(
    page_title="مهووس — التسعير الذكي v2", page_icon="🧪", layout="wide",
)


@st.cache_resource(show_spinner=False)
def _container() -> Container:
    """حاوية الاعتماديات (تُبنى مرة واحدة لكل جلسة خادم)."""
    return build_container()


def _read_upload(uploaded: Any) -> Any:
    """يقرأ الكتالوج المرفوع (CSV/Excel)."""
    import pandas as pd

    if str(getattr(uploaded, "name", "")).endswith(".xlsx"):
        return pd.read_excel(uploaded)
    return pd.read_csv(uploaded)


def _make_analyze(
    state: AppState, store: StateStore, container: Container,
) -> Callable[[Any], None]:
    """يبني رد نداء التحليل (يقرأ → يشغّل → يخزّن، مع حارس حفظ البيانات)."""

    def _run(uploaded: Any) -> None:
        try:
            result, split, missing_df = run_analysis(container, _read_upload(uploaded))
            state.analysis_results = result
            state.sections = split.sections
            state.missing_df = missing_df
            st.toast("✅ اكتمل التحليل", icon="✅")
        except DataLossError as exc:
            st.error(f"❌ خرق حفظ البيانات: {exc}")
        except Exception as exc:  # عرض الخطأ بدل التعطّل الصامت
            st.error(f"تعذّر التحليل: {exc}")
        state.save(store)

    return _run


def _dashboard(state: AppState, store: StateStore, container: Container) -> None:
    dashboard.render(state, on_analyze=_make_analyze(state, store, container))


def _missing(state: AppState, store: StateStore, container: Container) -> None:
    missing.render(
        state, state.missing_df,
        ai_service=container.ai, export_service=container.export,
    )


def _processed(state: AppState, store: StateStore, container: Container) -> None:
    processed.render(state, ai_service=container.ai)


def _section(page: Any) -> Callable[..., None]:
    """مهايئ موحّد للأقسام السعرية/المراجعة/المستبعد."""

    def _render(state: AppState, store: StateStore, container: Container) -> None:
        page.render(state, state.sections)

    return _render


PAGES: dict[str, Callable[[AppState, StateStore, Container], None]] = {
    "📊 لوحة التحكم": _dashboard,
    "🔴 سعر أعلى": _section(price_raise),
    "🟢 سعر أقل": _section(price_lower),
    "✅ موافق عليها": _section(approved),
    "🔍 منتجات مفقودة": _missing,
    "⚠️ تحت المراجعة": _section(review),
    "⚪ مستبعد": _section(excluded),
    "✅ تمت المعالجة": _processed,
}


def _sidebar(state: AppState) -> str:
    """شريط جانبي لاختيار القسم."""
    st.sidebar.title("🧪 مهووس v2")
    labels = list(PAGES.keys())
    index = labels.index(state.current_page) if state.current_page in labels else 0
    return st.sidebar.radio("الأقسام", labels, index=index)


def main() -> None:
    """نقطة الدخول: حمّل الحالة → وزّع على الصفحة → احفظ."""
    store = StreamlitStore()
    state = AppState.load(store)
    container = _container()
    choice = _sidebar(state)
    state.current_page = choice
    PAGES[choice](state, store, container)
    state.save(store)


if __name__ == "__main__":
    main()
