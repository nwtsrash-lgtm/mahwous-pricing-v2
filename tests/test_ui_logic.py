"""tests/test_ui_logic.py — اختبارات منطق الواجهة الخالص (P4) دون Streamlit."""
import pandas as pd

from core.enums import ConfidenceLevel, SectionType
from ui.components.action_bar import page_keys
from ui.components.filter_bar import Filters, apply_filters, options_for
from ui.components.pagination import paginate
from ui.components.product_card import build_card
from ui.components.status_badge import confidence_badge, section_badge
from ui.state_manager import AppState, DictStore, stable_key


# ── state_manager ──
def test_stable_key_and_hide_unhide() -> None:
    assert stable_key(" عطر ") == "softdel_عطر"
    state = AppState()
    state.hide("عطر شانيل")
    assert state.is_hidden("عطر شانيل")
    state.unhide("عطر شانيل")
    assert not state.is_hidden("عطر شانيل")


def test_state_save_load_roundtrip() -> None:
    store = DictStore()
    state = AppState.load(store)
    state.hide("منتج")
    state.mark_price_processed("SKU1")
    state.save(store)
    reloaded = AppState.load(store)
    assert reloaded.is_hidden("منتج") and reloaded.is_price_processed("SKU1")


def test_state_normalize_corrupt_sets() -> None:
    store = DictStore({"_app_state_v2": AppState(hidden_products=None)})  # type: ignore
    state = AppState.load(store)
    assert isinstance(state.hidden_products, set)


# ── pagination ──
def test_paginate_list_and_dataframe() -> None:
    view = paginate(list(range(30)), page=2, per_page=12)
    assert view.items == list(range(12, 24))
    assert view.total_pages == 3 and view.start == 12 and view.end == 24
    df = pd.DataFrame({"x": range(30)})
    dview = paginate(df, page=3, per_page=12)
    assert len(dview.items) == 6 and dview.page == 3


def test_paginate_clamps_overflow_and_empty() -> None:
    assert paginate(list(range(5)), page=99, per_page=10).page == 1
    empty = paginate([], page=1, per_page=10)
    assert empty.total == 0 and empty.total_pages == 1 and empty.caption == "لا عناصر"


# ── filter_bar ──
def test_apply_filters_search_brand_price() -> None:
    df = pd.DataFrame({
        "المنتج": ["عطر شانيل", "عطر ديور", "ماء"],
        "الماركة": ["Chanel", "Dior", "Chanel"],
        "السعر": [100, 200, 50],
    })
    assert len(apply_filters(df, Filters(search="ديور"))) == 1
    assert len(apply_filters(df, Filters(brand="Chanel"))) == 2
    assert len(apply_filters(df, Filters(min_price=80, max_price=150))) == 1
    assert options_for(df, "الماركة") == ["الكل", "Chanel", "Dior"]


def test_filters_active_chips() -> None:
    chips = Filters(search="x", brand="Chanel").active_chips
    assert any("Chanel" in c for c in chips) and any("x" in c for c in chips)


# ── status_badge ──
def test_badges() -> None:
    assert confidence_badge(90)[1] == "green"
    assert confidence_badge(70)[1] == "orange"
    assert confidence_badge(40)[1] == "gray"
    label, color, icon = section_badge(SectionType.PRICE_RAISE)
    assert color == "red" and icon == "🔴"


# ── product_card ──
def test_build_card_extracts_fields() -> None:
    row = {"المنتج": "عطر", "السعر": "100", "معرف_المنتج": "S1",
           "منتج_المنافس": "عطر منافس", "سعر_المنافس": 120, "المنافس": "متجر",
           "نسبة_التطابق": 95, "الفرق": -20}
    card = build_card(row)
    assert card.our_name == "عطر" and card.our_price == 100.0
    assert card.comp_store == "متجر" and card.match_pct == 95.0


# ── action_bar ──
def test_page_keys() -> None:
    df = pd.DataFrame({"معرف_المنتج": ["a", " b ", ""]})
    assert page_keys(df, "معرف_المنتج") == ["a", "b"]
    assert page_keys(pd.DataFrame(), "x") == []
