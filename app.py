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

from bootstrap import (
    Container,
    build_container,
    run_missing_analysis,
    run_pricing_analysis,
)
from ui.pages import (
    approved,
    dashboard,
    excluded,
    missing,
    price_lower,
    price_raise,
    processed,
    review,
    scraper,
)
from ui.state_manager import AppState, StateStore, StreamlitStore

st.set_page_config(
    page_title="مهووس — التسعير الذكي v2", page_icon="🧪", layout="wide",
)


@st.cache_resource(show_spinner=False)
def _container() -> Container:
    """حاوية الاعتماديات (تُبنى مرة واحدة لكل جلسة خادم)."""
    return build_container()


def _make_analyze(
    state: AppState, store: StateStore, container: Container,
) -> Callable[[Any], None]:
    """رد نداء التحليل الكامل: كتالوج → مفقودات → تسعير → أقسام + تدقيق → تخزين."""

    def _run(uploaded: Any) -> None:
        from core.enums import SectionType
        from services.catalog_service import load_catalog

        try:
            with st.spinner("⏳ تحميل الكتالوج وكشف المفقودات (~دقيقتان لأول مرة)…"):
                our_df = load_catalog(uploaded)
                state.our_catalog = our_df
                missing_df, mstats = run_missing_analysis(container, our_df)
            state.missing_df = missing_df

            with st.spinner("⏳ التحليل السعري الكامل: مطابقة المنافسين (~دقائق لأول مرة)…"):
                sections, result, missing_clean, _astats = run_pricing_analysis(
                    container, our_df, missing_df=missing_df,
                )
            state.sections = sections
            state.missing_df = missing_clean  # بعد إزالة المطابَق سعرياً (مصدر حقيقة واحد)
            state.analysis_results = result  # يحمل تقرير التدقيق ⇒ البانر يصدق

            counts = result.section_counts
            st.toast(
                f"✅ تحليل مكتمل · 🔴 {counts.get(SectionType.PRICE_RAISE, 0):,} "
                f"· 🟢 {counts.get(SectionType.PRICE_LOWER, 0):,} "
                f"· 🔍 {mstats.get('confirmed_missing', 0):,} مفقود مؤكد",
                icon="✅",
            )
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


def _scraper(state: AppState, store: StateStore, container: Container) -> None:
    scraper.render(state, container.scraper)


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
    "🕷️ كشط المنافسين": _scraper,
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
