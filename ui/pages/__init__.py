"""حزمة ui.pages — صفحات رفيعة تصل الخدمات بالمكوّنات (لا منطق عمل).

كل صفحة تُصدّر ``render(...)``. الأقسام السعرية/المراجعة/المستبعد أغلفة
رقيقة فوق ``_section_page.render_section_page``.
"""
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

__all__ = [
    "dashboard", "price_raise", "price_lower", "approved",
    "missing", "review", "excluded", "processed",
]
