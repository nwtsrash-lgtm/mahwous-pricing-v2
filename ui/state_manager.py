"""ui/state_manager.py — مدير حالة موحّد ومُنمَّط (بديل st.session_state الفوضوي).

يغلّف ``st.session_state`` خلف ``AppState`` مُنمّط + ``StateStore`` قابل للحقن،
فتُختبر منطق الحالة بقاموس عادي دون تشغيل Streamlit.

#PRESERVED_LOGIC: المفاتيح الثابتة للحذف الناعم بصيغة ``softdel_{اسم}``
(app.py soft-delete) — تبقى مستقرّة لإتاحة التراجع.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import pandas as pd

_STATE_KEY = "_app_state_v2"


def stable_key(product_name: str) -> str:
    """مفتاح حذف ناعم مستقرّ لمنتج. #PRESERVED_LOGIC softdel_{product_name}."""
    return f"softdel_{str(product_name).strip()}"


class StateStore(Protocol):
    """واجهة مخزن حالة (get/set/contains)."""

    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...


class DictStore:
    """مخزن قاموسي للاختبار (بلا Streamlit)."""

    def __init__(self, data: Optional[dict[str, Any]] = None) -> None:
        self._data: dict[str, Any] = data if data is not None else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class StreamlitStore:
    """مخزن يغلّف ``st.session_state`` (استيراد كسول)."""

    def __init__(self) -> None:
        import streamlit as st

        self._ss = st.session_state

    def get(self, key: str, default: Any = None) -> Any:
        return self._ss.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._ss[key] = value


@dataclass
class AppState:
    """حالة التطبيق المُنمّطة. تُحمَّل/تُحفَظ عبر ``StateStore``."""

    our_catalog: Optional[pd.DataFrame] = None
    competitor_catalogs: dict[str, pd.DataFrame] = field(default_factory=dict)
    analysis_results: Optional[Any] = None
    job_id: Optional[str] = None
    is_analysis_running: bool = False
    hidden_products: set[str] = field(default_factory=set)
    processed_price_skus: set[str] = field(default_factory=set)
    processed_missing_urls: set[str] = field(default_factory=set)
    current_page: str = "dashboard"
    # نتائج التحليل الجاهزة للعرض (يملؤها الموجّه بعد run_analysis).
    sections: dict[str, Any] = field(default_factory=dict)
    missing_df: Optional[pd.DataFrame] = None

    # ── الحذف الناعم (مفاتيح مستقرّة) ──
    def hide(self, product_name: str) -> None:
        self.hidden_products.add(stable_key(product_name))

    def unhide(self, product_name: str) -> None:
        self.hidden_products.discard(stable_key(product_name))

    def is_hidden(self, product_name: str) -> bool:
        return stable_key(product_name) in self.hidden_products

    # ── تتبّع المعالجة ──
    def mark_price_processed(self, sku: str) -> None:
        self.processed_price_skus.add(str(sku).strip())

    def mark_missing_processed(self, url: str) -> None:
        self.processed_missing_urls.add(str(url).strip())

    def is_price_processed(self, sku: str) -> bool:
        return str(sku).strip() in self.processed_price_skus

    def save(self, store: StateStore) -> None:
        """يحفظ الحالة في المخزن (مفتاح واحد يحمل الكائن)."""
        store.set(_STATE_KEY, self)

    @classmethod
    def load(cls, store: StateStore) -> "AppState":
        """يحمّل الحالة أو ينشئ جديدة مع تطبيع الأنواع."""
        existing = store.get(_STATE_KEY)
        if isinstance(existing, cls):
            existing._normalize()
            return existing
        state = cls()
        state.save(store)
        return state

    def _normalize(self) -> None:
        """يضمن أنواع المجموعات (حماية من حالة قديمة تالفة)."""
        self.hidden_products = set(self.hidden_products or set())
        self.processed_price_skus = set(self.processed_price_skus or set())
        self.processed_missing_urls = set(self.processed_missing_urls or set())
