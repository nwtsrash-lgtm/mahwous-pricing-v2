"""services/pricing_service.py — حساب الفروقات والقرار السعري (نقل حرفي).

نقل دقيق لمنطق القرار السعري من ``engines/engine.py`` (الدالة المنتجة لعمود
القرار): عتبة سعرية ذكية حسب متوسط السعر، ثم توزيع ثلاثي
(أعلى/أقل/موافق) مع حالة «بلا سعر منافس».

#PRESERVED_LOGIC: _smart_price_threshold (engine.py:2337-2347) وقرار السعر
(engine.py:2358-2366، diff = our_price − comp_price، engine.py:2317).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from conf.constants import PRICE_TOLERANCE
from core.enums import ActionType
from core.models import PriceDiff

# نصوص القرار الحرفية (#PRESERVED_LOGIC engine.py:2362-2366).
DECISION_RAISE = "🔴 سعر أعلى"
DECISION_LOWER = "🟢 سعر أقل"
DECISION_APPROVED = "✅ موافق"
DECISION_NO_COMP_PRICE = "⚠️ تحت المراجعة — بلا سعر منافس"

# معاملات العتبة الذكية (engine.py:2339-2341).
_HIGH_AVG = 300.0
_HIGH_PCT = 0.05
_MID_AVG = 100.0
_LOW_TOL = 5.0


def smart_price_threshold(
    our_price: float, comp_price: float, default_tol: float = PRICE_TOLERANCE,
) -> float:
    """عتبة تسامح «✅ موافق» ديناميكية حسب متوسط السعر."""
    tol = default_tol if default_tol else 10
    if our_price <= 0 or comp_price <= 0:
        return float(tol)
    avg = (our_price + comp_price) / 2
    if avg >= _HIGH_AVG:
        return avg * _HIGH_PCT
    if avg >= _MID_AVG:
        return float(tol)
    return _LOW_TOL


@dataclass(frozen=True)
class PriceDecision:
    """قرار سعري: النص + الفرق + العتبة المستخدمة + الإجراء."""

    decision: str
    diff: float
    threshold: float
    action: Optional[ActionType]


def decide_price(
    our_price: float, comp_price: float, default_tol: float = PRICE_TOLERANCE,
) -> PriceDecision:
    """يقرّر القسم السعري. diff = سعرنا − سعر المنافس (موجب = نحن أعلى)."""
    if our_price > 0 and comp_price > 0:
        pt = smart_price_threshold(our_price, comp_price, default_tol)
        diff = round(our_price - comp_price, 2)
        if diff > pt:
            return PriceDecision(DECISION_RAISE, diff, pt, ActionType.RAISE)
        if diff < -pt:
            return PriceDecision(DECISION_LOWER, diff, pt, ActionType.LOWER)
        return PriceDecision(DECISION_APPROVED, diff, pt, ActionType.APPROVE)
    return PriceDecision(DECISION_NO_COMP_PRICE, 0.0, 0.0, ActionType.REVIEW)


def undercut_price(comp_price: float) -> float:
    """سعر مقترح يقلّ عن المنافس بريال واحد. #PRESERVED_LOGIC app.py:6063."""
    return round(comp_price - 1, 2) if comp_price > 0 else 0.0


def suggested_price_with_margin(comp_price: float, margin_pct: float) -> float:
    """سعر مقترح بهامش فوق سعر المنافس. #PRESERVED_LOGIC engine.py:3522."""
    return round(comp_price * (1 + margin_pct / 100), 0) if comp_price > 0 else 0.0


class PricingService:
    """خدمة التسعير: تحوّل سعرَين إلى قرار + نموذج فرق سعري مُهيكَل."""

    def __init__(self, default_tol: float = PRICE_TOLERANCE) -> None:
        self._tol = default_tol

    def evaluate(
        self, product_id: str, our_price: float, comp_price: float,
    ) -> tuple[PriceDecision, PriceDiff]:
        """يُعيد (القرار، نموذج PriceDiff) لمنتج واحد."""
        decision = decide_price(our_price, comp_price, self._tol)
        diff_pct = (decision.diff / comp_price * 100.0) if comp_price > 0 else 0.0
        suggested = (
            undercut_price(comp_price)
            if decision.action == ActionType.RAISE
            else None
        )
        price_diff = PriceDiff(
            product_id=product_id,
            our_price=our_price,
            competitor_price=comp_price,
            diff=decision.diff,
            diff_pct=round(diff_pct, 2),
            suggested_price=suggested,
            action=decision.action,
        )
        return decision, price_diff
