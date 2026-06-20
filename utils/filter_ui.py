"""
utils/filter_ui.py — محرك الفلترة المركزي v2.0
═══════════════════════════════════════════════
فلاتر سريعة في الشريط الجانبي تُطبَّق عبر جميع أقسام البيانات.
تُكمّل (لا تستبدل) الفلاتر التفصيلية الموجودة داخل كل قسم.

v2.0 — Task 3.1 additions:
  • Price range (نطاق السعر)
  • Date added filter (تاريخ الإضافة)
  • Inventory / availability status (حالة المخزون) — hook ready
  • Gender classification (التصنيف الجنسي) — NEVER treated as stopword
  • Perfume size (حجم العطر) — ml-aware matching
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

# ── Session-state keys (prefixed _gf_ to avoid collision with other keys) ────
_GF_BRAND     = "_gf_brand"
_GF_RISK      = "_gf_risk"
_GF_SEARCH    = "_gf_search"
# v2.0 new keys
_GF_GENDER    = "_gf_gender"     # Task 3.1 — gender classification
_GF_SIZE      = "_gf_size"       # Task 3.1 — perfume size (ml)
_GF_PRICE_MIN = "_gf_price_min"  # Task 3.1 — price range lower bound
_GF_PRICE_MAX = "_gf_price_max"  # Task 3.1 — price range upper bound
_GF_DATE      = "_gf_date"       # Task 3.1 — date added period
_GF_STOCK     = "_gf_stock"      # Task 3.1 — inventory status (hook, future)
_GF_CHANGE    = "_gf_change"     # حالة التغيير: جديد / تغيّر السعر (مقارنةً بالتحليل السابق)

# خيارات فلتر «حالة التغيير» → القيمة المخزّنة في عمود «حالة_التغيير»
_CHANGE_OPTIONS = {
    "الكل": None,
    "🆕 المنتجات الجديدة": "🆕 جديد",
    "🔄 المتغير أسعارها": "🔄 تغيّر السعر",
}

# Date-period labels → timedelta offset from today
_DATE_OPTIONS = {
    "كل الفترات": None,
    "اليوم": timedelta(days=0),
    "آخر 3 أيام": timedelta(days=3),
    "آخر 7 أيام": timedelta(days=7),
    "آخر 30 يوماً": timedelta(days=30),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _unique_vals(df: pd.DataFrame, col: str) -> list[str]:
    """Return sorted unique non-empty string values for a column."""
    if col not in df.columns:
        return []
    return sorted(
        str(v) for v in df[col].dropna().unique()
        if str(v).strip() and str(v) not in ("nan", "None")
    )


# ── Public API ────────────────────────────────────────────────────────────────

def render_sidebar_filters(df: pd.DataFrame) -> None:
    """
    Draw all quick-filter widgets in the sidebar.
    Call exactly once per render inside `with st.sidebar:` (or without it,
    since Streamlit auto-routes sidebar widgets when called from there).

    Filters shown (all conditional on column existence):
      v1.0: brand, risk level, text search
      v2.0: gender, size, price range, date period, inventory status hook
    """
    if df is None or df.empty:
        return

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🔍 فلاتر سريعة")

    # ── Original v1.0 filters ─────────────────────────────────────────────────

    # Brand filter
    _brands = _unique_vals(df, "الماركة")
    if _brands:
        st.sidebar.selectbox("🏷️ الماركة", ["الكل"] + _brands, key=_GF_BRAND)
    else:
        st.session_state.setdefault(_GF_BRAND, "الكل")

    # Risk level filter
    _risks = _unique_vals(df, "الخطورة")
    if _risks:
        st.sidebar.selectbox("⚡ الخطورة", ["الكل"] + _risks, key=_GF_RISK)
    else:
        st.session_state.setdefault(_GF_RISK, "الكل")

    # Quick text search
    st.sidebar.text_input(
        "🔎 بحث سريع",
        key=_GF_SEARCH,
        placeholder="اسم أو SKU أو ماركة…",
    )

    # ── v2.0 new filters ──────────────────────────────────────────────────────

    st.sidebar.markdown("---")
    st.sidebar.markdown("#### 🧴 فلاتر متقدمة")

    # Gender filter — preserves رجالي / نسائي / للجنسين distinctions
    _genders = _unique_vals(df, "الجنس")
    if _genders:
        st.sidebar.selectbox(
            "🚻 الجنس",
            ["الكل"] + _genders,
            key=_GF_GENDER,
            help="تصفية حسب الجنس (رجالي / نسائي / للجنسين)",
        )
    else:
        st.session_state.setdefault(_GF_GENDER, "الكل")

    # Perfume size filter (ml)
    _sizes = _unique_vals(df, "الحجم")
    if _sizes:
        st.sidebar.selectbox(
            "📦 الحجم",
            ["الكل"] + _sizes,
            key=_GF_SIZE,
            help="تصفية حسب حجم العطر (مل)",
        )
    else:
        st.session_state.setdefault(_GF_SIZE, "الكل")

    # Price range — two compact number inputs side by side
    if "السعر" in df.columns:
        _p_col1, _p_col2 = st.sidebar.columns(2)
        with _p_col1:
            st.number_input(
                "💰 سعر من",
                min_value=0.0,
                step=10.0,
                value=float(st.session_state.get(_GF_PRICE_MIN, 0.0) or 0.0),
                key=_GF_PRICE_MIN,
            )
        with _p_col2:
            st.number_input(
                "💰 سعر إلى",
                min_value=0.0,
                step=10.0,
                value=float(st.session_state.get(_GF_PRICE_MAX, 0.0) or 0.0),
                key=_GF_PRICE_MAX,
                help="0 = بدون حد أقصى",
            )

    # Date added period — filters on تاريخ_المطابقة column
    if "تاريخ_المطابقة" in df.columns:
        st.sidebar.selectbox(
            "📅 الفترة الزمنية",
            list(_DATE_OPTIONS.keys()),
            key=_GF_DATE,
            help="فلترة حسب تاريخ إضافة المطابقة",
        )
    else:
        st.session_state.setdefault(_GF_DATE, "كل الفترات")

    # حالة التغيير — يظهر فقط بعد تحليل تراكمي (عمود «حالة_التغيير» موجود وبه قيم)
    if ("حالة_التغيير" in df.columns
            and df["حالة_التغيير"].astype(str).str.strip().ne("").any()):
        st.sidebar.selectbox(
            "🔔 حالة التغيير",
            list(_CHANGE_OPTIONS.keys()),
            key=_GF_CHANGE,
            help="المنتجات الجديدة أو التي تغيّر سعرها مقارنةً بالتحليل السابق",
        )
    else:
        st.session_state.setdefault(_GF_CHANGE, "الكل")

    # Inventory status hook — shown only if column exists (scraped data)
    _stock_vals = _unique_vals(df, "availability")
    if _stock_vals:
        st.sidebar.selectbox(
            "🏬 المخزون",
            ["الكل"] + _stock_vals,
            key=_GF_STOCK,
            help="تصفية حسب حالة توفر المنتج",
        )
    else:
        st.session_state.setdefault(_GF_STOCK, "الكل")

    # Reset button — clears all v2 filters in one click
    if st.sidebar.button("🔄 مسح الفلاتر المتقدمة", key="_gf_reset_adv"):
        for _k in (_GF_GENDER, _GF_SIZE, _GF_PRICE_MIN, _GF_PRICE_MAX,
                   _GF_DATE, _GF_STOCK, _GF_CHANGE):
            st.session_state.pop(_k, None)
        st.rerun()


def apply_global_filters(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply all active global (sidebar) filters to df.

    Safe contract:
      • Returns df unchanged if no filter is active or column is absent.
      • Never modifies the original df — operates on a copy.
      • v2.0: applies gender, size, price range, date and inventory filters.
    """
    if df is None or df.empty:
        return df

    brand_v     = str(st.session_state.get(_GF_BRAND,     "الكل") or "الكل")
    risk_v      = str(st.session_state.get(_GF_RISK,      "الكل") or "الكل")
    search_v    = str(st.session_state.get(_GF_SEARCH,    "")     or "").strip()
    gender_v    = str(st.session_state.get(_GF_GENDER,    "الكل") or "الكل")
    size_v      = str(st.session_state.get(_GF_SIZE,      "الكل") or "الكل")
    price_min_v = float(st.session_state.get(_GF_PRICE_MIN, 0.0) or 0.0)
    price_max_v = float(st.session_state.get(_GF_PRICE_MAX, 0.0) or 0.0)
    date_v      = str(st.session_state.get(_GF_DATE,      "كل الفترات") or "كل الفترات")
    stock_v     = str(st.session_state.get(_GF_STOCK,     "الكل") or "الكل")
    change_v    = str(st.session_state.get(_GF_CHANGE,    "الكل") or "الكل")

    # Fast path: nothing active
    _no_v1 = brand_v == "الكل" and risk_v == "الكل" and not search_v
    _no_v2 = (gender_v == "الكل" and size_v == "الكل"
              and price_min_v == 0.0 and price_max_v == 0.0
              and date_v == "كل الفترات" and stock_v == "الكل"
              and change_v == "الكل")
    if _no_v1 and _no_v2:
        return df

    result = df.copy()

    # ── v1.0 filters ──────────────────────────────────────────────────────────
    if brand_v != "الكل" and "الماركة" in result.columns:
        result = result[result["الماركة"].astype(str) == brand_v]

    if risk_v != "الكل" and "الخطورة" in result.columns:
        result = result[result["الخطورة"].astype(str) == risk_v]

    if search_v:
        _mask = pd.Series([False] * len(result), index=result.index)
        for _col in ("المنتج", "معرف_المنتج", "الماركة", "منتج_المنافس"):
            if _col in result.columns:
                _mask = _mask | result[_col].astype(str).str.contains(
                    search_v, case=False, na=False
                )
        result = result[_mask]

    # ── v2.0 filters ──────────────────────────────────────────────────────────

    # Gender — strict equality (رجالي / نسائي / للجنسين must never be stripped)
    if gender_v != "الكل" and "الجنس" in result.columns:
        result = result[result["الجنس"].astype(str) == gender_v]

    # Perfume size
    if size_v != "الكل" and "الحجم" in result.columns:
        result = result[result["الحجم"].astype(str) == size_v]

    # Price range
    if price_min_v > 0.0 and "السعر" in result.columns:
        result = result[result["السعر"].apply(
            lambda x: _safe_float(x) >= price_min_v
        )]
    if price_max_v > 0.0 and "السعر" in result.columns:
        result = result[result["السعر"].apply(
            lambda x: _safe_float(x) <= price_max_v
        )]

    # Date period filter on تاريخ_المطابقة (format: YYYY-MM-DD)
    _delta = _DATE_OPTIONS.get(date_v)
    if _delta is not None and "تاريخ_المطابقة" in result.columns:
        _cutoff = (date.today() - _delta).isoformat()
        result = result[
            result["تاريخ_المطابقة"].astype(str).str[:10] >= _cutoff
        ]

    # Inventory / availability status
    if stock_v != "الكل" and "availability" in result.columns:
        result = result[result["availability"].astype(str) == stock_v]

    # حالة التغيير (جديد / تغيّر السعر)
    _change_target = _CHANGE_OPTIONS.get(change_v)
    if _change_target is not None and "حالة_التغيير" in result.columns:
        result = result[result["حالة_التغيير"].astype(str) == _change_target]

    return result.reset_index(drop=True)


def get_active_filter_summary() -> str:
    """
    Human-readable summary of all active global filters.
    Used as a table caption (e.g. "ماركة: Dior | جنس: رجالي | سعر: 100-500").
    v2.0: includes new filter keys.
    """
    _parts: list[str] = []

    brand_v     = str(st.session_state.get(_GF_BRAND,     "الكل") or "الكل")
    risk_v      = str(st.session_state.get(_GF_RISK,      "الكل") or "الكل")
    search_v    = str(st.session_state.get(_GF_SEARCH,    "")     or "").strip()
    gender_v    = str(st.session_state.get(_GF_GENDER,    "الكل") or "الكل")
    size_v      = str(st.session_state.get(_GF_SIZE,      "الكل") or "الكل")
    price_min_v = float(st.session_state.get(_GF_PRICE_MIN, 0.0) or 0.0)
    price_max_v = float(st.session_state.get(_GF_PRICE_MAX, 0.0) or 0.0)
    date_v      = str(st.session_state.get(_GF_DATE,      "كل الفترات") or "كل الفترات")
    stock_v     = str(st.session_state.get(_GF_STOCK,     "الكل") or "الكل")
    change_v    = str(st.session_state.get(_GF_CHANGE,    "الكل") or "الكل")

    if brand_v  != "الكل":         _parts.append(f"ماركة: {brand_v}")
    if risk_v   != "الكل":         _parts.append(f"خطورة: {risk_v}")
    if search_v:                   _parts.append(f"بحث: {search_v}")
    if gender_v != "الكل":         _parts.append(f"جنس: {gender_v}")
    if size_v   != "الكل":         _parts.append(f"حجم: {size_v}")
    if price_min_v > 0 or price_max_v > 0:
        _p_lo = f"{price_min_v:.0f}" if price_min_v > 0 else "0"
        _p_hi = f"{price_max_v:.0f}" if price_max_v > 0 else "∞"
        _parts.append(f"سعر: {_p_lo}–{_p_hi}")
    if date_v   != "كل الفترات":   _parts.append(f"تاريخ: {date_v}")
    if stock_v  != "الكل":         _parts.append(f"مخزون: {stock_v}")
    if change_v != "الكل":         _parts.append(f"تغيير: {change_v}")

    return " | ".join(_parts) if _parts else ""


# ── Internal helper ───────────────────────────────────────────────────────────

def _safe_float(v) -> float:
    """Convert a cell value to float safely, return 0.0 on failure."""
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0
