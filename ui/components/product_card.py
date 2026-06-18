"""ui/components/product_card.py — بطاقة المقارنة (منتجنا ضدّ المنافس).

``build_card`` خالصة تستخرج حقول العرض من صفّ عبر ثوابت الأعمدة؛
``render_product_card`` رفيعة تستخدم عناصر Streamlit الأصلية (بلا HTML).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from conf.constants import (
    COL_BRAND,
    COL_COMP_IMAGE,
    COL_COMP_NAME,
    COL_COMP_PRICE,
    COL_COMP_STORE,
    COL_DIFF,
    COL_MATCH_RATIO,
    COL_OUR_ID,
    COL_OUR_NAME,
    COL_OUR_PRICE,
)


def _get(row: Any, key: str, default: str = "") -> Any:
    """قراءة آمنة من صف/قاموس."""
    getter = row.get if hasattr(row, "get") else (lambda k, d=None: d)
    value = getter(key, default)
    return default if value is None or str(value) == "nan" else value


@dataclass(frozen=True)
class CardView:
    """حقول البطاقة الجاهزة للعرض."""

    our_name: str
    our_price: float
    our_sku: str
    our_brand: str
    comp_name: str
    comp_price: float
    comp_store: str
    comp_image: str
    diff: float
    match_pct: float


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_card(row: Any) -> CardView:
    """يبني نموذج بطاقة من صف نتيجة."""
    return CardView(
        our_name=str(_get(row, COL_OUR_NAME)),
        our_price=_to_float(_get(row, COL_OUR_PRICE, 0)),
        our_sku=str(_get(row, COL_OUR_ID)),
        our_brand=str(_get(row, COL_BRAND)),
        comp_name=str(_get(row, COL_COMP_NAME)),
        comp_price=_to_float(_get(row, COL_COMP_PRICE, 0)),
        comp_store=str(_get(row, COL_COMP_STORE)),
        comp_image=str(_get(row, COL_COMP_IMAGE)),
        diff=_to_float(_get(row, COL_DIFF, 0)),
        match_pct=_to_float(_get(row, COL_MATCH_RATIO, 0)),
    )


def render_product_card(row: Any, *, detailed: bool = False) -> None:
    """يعرض بطاقة مقارنة (مدمجة افتراضياً، مفصّلة للمفقودات)."""
    import streamlit as st

    card = build_card(row)
    ours, comp = st.columns(2)
    with ours:
        st.caption("منتجنا")
        st.write(f"**{card.our_name}**")
        st.write(f"💰 {card.our_price:,.0f} ر.س · SKU {card.our_sku}")
    with comp:
        st.caption(f"المنافس · {card.comp_store}")
        if detailed and card.comp_image:
            st.image(card.comp_image, width=120)
        st.write(f"**{card.comp_name}**")
        st.write(f"💰 {card.comp_price:,.0f} ر.س")
    if card.match_pct:
        st.caption(f"🔍 تطابق {card.match_pct:.0f}% · فرق {card.diff:,.0f} ر.س")
