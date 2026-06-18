"""ui/components/pagination.py — ترقيم موحّد (حساب خالص + عرض رفيع).

الترقيم منطق خالص قابل للاختبار (تقطيع/عدّ صفحات)، والعرض أزرار أصلية.
لا تمرير لانهائي. #PRESERVED_LOGIC: 25/جدول، 12/بطاقات (config.py:200).
"""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

import pandas as pd

CARDS_PER_PAGE = 12
TABLE_PER_PAGE = 25


@dataclass(frozen=True)
class PageView:
    """نافذة صفحة: العناصر + موضعها + الإجماليات."""

    items: Any
    page: int
    per_page: int
    total: int
    total_pages: int
    start: int
    end: int

    @property
    def caption(self) -> str:
        """نص شفافية: «عرض س-ص من ك»."""
        if self.total == 0:
            return "لا عناصر"
        return f"عرض {self.start + 1}–{self.end} من {self.total}"


def clamp_page(page: int, total_pages: int) -> int:
    """يقصر رقم الصفحة ضمن [1, total_pages]."""
    return max(1, min(int(page or 1), max(1, total_pages)))


def paginate(items: Any, page: int = 1, per_page: int = CARDS_PER_PAGE) -> PageView:
    """يقطع العناصر لصفحة واحدة (يدعم list وDataFrame)."""
    total = len(items)
    total_pages = max(1, ceil(total / per_page)) if per_page > 0 else 1
    page = clamp_page(page, total_pages)
    start = (page - 1) * per_page
    end = min(start + per_page, total)
    if isinstance(items, pd.DataFrame):
        window = items.iloc[start:end]
    else:
        window = items[start:end]
    return PageView(window, page, per_page, total, total_pages, start, end)


def _set_page(page_key: str, value: int) -> None:
    """رد نداء يحدّث الصفحة في الحالة قبل إعادة التشغيل (لا st.rerun)."""
    import streamlit as st

    st.session_state[page_key] = max(1, int(value))


def render_pagination(view: PageView, key: str) -> None:
    """يعرض أزرار التنقّل عبر on_click — يحدّث ``{key}_page`` في الحالة بلا لَبس زمني."""
    import streamlit as st

    page_key = f"{key}_page"
    left, mid, right = st.columns([1, 2, 1])
    left.button("◀ السابق", key=f"{key}_prev", disabled=view.page <= 1,
                on_click=_set_page, args=(page_key, view.page - 1))
    right.button("التالي ▶", key=f"{key}_next", disabled=view.page >= view.total_pages,
                 on_click=_set_page, args=(page_key, view.page + 1))
    mid.caption(f"{view.caption} · صفحة {view.page}/{view.total_pages}")
