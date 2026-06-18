"""ui/pages/price_raise.py — قسم 🔴 سعر أعلى (غلاف رفيع)."""
from __future__ import annotations

import pandas as pd

from core.enums import SectionType
from ui.pages._section_page import render_section_page
from ui.state_manager import AppState


def render(state: AppState, sections: dict[str, pd.DataFrame]) -> None:
    """يعرض قسم «سعر أعلى» (منتجاتنا أغلى من المنافس)."""
    render_section_page(state, sections, SectionType.PRICE_RAISE)
