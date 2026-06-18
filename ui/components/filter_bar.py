"""ui/components/filter_bar.py — شريط فلاتر (تطبيق خالص + عرض رفيع).

``apply_filters`` خالصة على DataFrame وقابلة للاختبار؛ ``render_filter_bar``
تجمع القيم من موسّع مطويّ افتراضياً وتعيد ``Filters``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from conf.constants import COL_BRAND, COL_GENDER, COL_OUR_NAME, COL_OUR_PRICE

_ALL = "الكل"


@dataclass(frozen=True)
class Filters:
    """قيم الفلاتر النشطة."""

    search: str = ""
    brand: str = _ALL
    gender: str = _ALL
    min_price: Optional[float] = None
    max_price: Optional[float] = None

    @property
    def active_chips(self) -> list[str]:
        """رقائق الفلاتر النشطة للعرض الشفّاف."""
        chips: list[str] = []
        if self.search:
            chips.append(f"🔎 {self.search}")
        if self.brand != _ALL:
            chips.append(f"🏷️ {self.brand}")
        if self.gender != _ALL:
            chips.append(f"🚻 {self.gender}")
        if self.min_price is not None or self.max_price is not None:
            chips.append(f"💵 {self.min_price or 0}–{self.max_price or '∞'}")
        return chips


def apply_filters(df: pd.DataFrame, filters: Filters,
                  name_col: str = COL_OUR_NAME) -> pd.DataFrame:
    """يطبّق الفلاتر على DataFrame (خالص، vectorized)."""
    if df is None or df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if filters.search and name_col in df.columns:
        mask &= df[name_col].astype(str).str.contains(
            filters.search, case=False, na=False, regex=False)
    if filters.brand != _ALL and COL_BRAND in df.columns:
        mask &= df[COL_BRAND].astype(str).str.strip() == filters.brand
    if filters.gender != _ALL and COL_GENDER in df.columns:
        mask &= df[COL_GENDER].astype(str).str.strip() == filters.gender
    if (filters.min_price is not None or filters.max_price is not None) \
            and COL_OUR_PRICE in df.columns:
        price = pd.to_numeric(df[COL_OUR_PRICE], errors="coerce").fillna(0)
        if filters.min_price is not None:
            mask &= price >= filters.min_price
        if filters.max_price is not None:
            mask &= price <= filters.max_price
    return df[mask]


def options_for(df: pd.DataFrame, col: str) -> list[str]:
    """قيم فريدة لعمود (لقوائم الاختيار) مع «الكل» أولاً."""
    if df is None or df.empty or col not in df.columns:
        return [_ALL]
    values = sorted({str(v).strip() for v in df[col].dropna() if str(v).strip()})
    return [_ALL, *values]


def render_filter_bar(df: pd.DataFrame, key: str) -> Filters:
    """يعرض موسّع الفلاتر (مطويّ) ويعيد القيم المختارة."""
    import streamlit as st

    with st.expander("🔍 الفلاتر", expanded=False):
        col1, col2, col3 = st.columns(3)
        search = col1.text_input("بحث", key=f"{key}_q")
        brand = col2.selectbox("الماركة", options_for(df, COL_BRAND), key=f"{key}_b")
        gender = col3.selectbox("الجنس", options_for(df, COL_GENDER), key=f"{key}_g")
    return Filters(search=search.strip(), brand=brand, gender=gender)
