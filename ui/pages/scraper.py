"""ui/pages/scraper.py — 🕷️ كشط المنافسين (إدارة الروابط + الكشط، غلاف رفيع).

يعرض المتاجر المُعرَّفة، يتيح إضافة متجر عبر رابط محلي، حذف متجر، وكشطه
وحفظ منتجاته. كل المنطق في `ScraperService` — الصفحة وصل فقط.
"""
from __future__ import annotations

from typing import Any

from services.scraper_service import Competitor, ScraperService
from ui.state_manager import AppState


def _render_add(scraper: ScraperService) -> None:
    """نموذج إضافة متجر منافس جديد."""
    import streamlit as st

    with st.popover("➕ إضافة متجر منافس"):
        st.caption("الصق رابط المتجر من محلي، مثل:")
        st.code("https://mahally.com/stores/216339537/")
        url = st.text_input("رابط محلي", key="sc_new_url")
        name = st.text_input("اسم المتجر", key="sc_new_name")
        if st.button("✅ إضافة", key="sc_add"):
            try:
                comp = scraper.add_competitor(name, url)
                st.success(f"أُضيف «{comp.name}» (#{comp.mahally_store_id})")
            except Exception as exc:
                st.error(str(exc))


def _remove_cb(scraper: ScraperService, store_id: int) -> None:
    scraper.remove_competitor(store_id)


def _render_row(scraper: ScraperService, comp: Competitor) -> None:
    """سطر متجر واحد: كشط + حذف."""
    import streamlit as st

    col1, col2, col3 = st.columns([4, 1, 1])
    col1.write(f"🏪 **{comp.name}** — `#{comp.mahally_store_id}`")
    if col2.button("🔄 كشط", key=f"sc_run_{comp.mahally_store_id}"):
        with st.spinner(f"جارٍ كشط «{comp.name}»…"):
            try:
                saved = scraper.scrape_and_save(comp.mahally_store_id, comp.name)
                st.success(f"✅ حُفظ {saved:,} منتجاً من «{comp.name}»")
            except Exception as exc:
                st.error(f"تعذّر الكشط: {exc}")
    col3.button("🗑️", key=f"sc_rm_{comp.mahally_store_id}",
                on_click=_remove_cb, args=(scraper, comp.mahally_store_id))


def render(state: AppState, scraper: ScraperService) -> None:
    """يعرض صفحة كشط المنافسين كاملة."""
    import streamlit as st

    st.header("🕷️ كشط المنافسين")
    competitors = scraper.list_competitors()
    st.metric("🏪 متاجر مُعرَّفة", len(competitors))
    _render_add(scraper)
    st.divider()
    if not competitors:
        st.info("لا متاجر مُعرَّفة — أضِف متجراً عبر الزر أعلاه")
        return
    for comp in competitors:
        _render_row(scraper, comp)
