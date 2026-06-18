"""tests/test_pricing_service.py — اختبارات القرار السعري (P2)."""
import pytest

from core.enums import ActionType
from services.pricing_service import (
    DECISION_APPROVED,
    DECISION_LOWER,
    DECISION_NO_COMP_PRICE,
    DECISION_RAISE,
    PricingService,
    decide_price,
    smart_price_threshold,
    suggested_price_with_margin,
    undercut_price,
)


def test_smart_threshold_tiers() -> None:
    assert smart_price_threshold(500, 500) == 500 * 0.05   # متوسط ≥300 ⇒ 5%
    assert smart_price_threshold(150, 150) == 5            # متوسط [100,300) ⇒ الافتراضي
    assert smart_price_threshold(50, 50) == 5              # متوسط <100 ⇒ تسامح منخفض
    assert smart_price_threshold(0, 100) == 5             # سعر مفقود ⇒ الافتراضي


def test_decide_price_branches() -> None:
    assert decide_price(120, 100).decision == DECISION_RAISE     # نحن أعلى
    assert decide_price(100, 120).decision == DECISION_LOWER     # نحن أقل
    assert decide_price(102, 100).decision == DECISION_APPROVED  # ضمن التسامح
    assert decide_price(0, 100).decision == DECISION_NO_COMP_PRICE


def test_decide_price_diff_sign() -> None:
    d = decide_price(120, 100)
    assert d.diff == 20.0 and d.action == ActionType.RAISE


def test_suggested_price_helpers() -> None:
    assert undercut_price(100) == 99.0
    assert undercut_price(0) == 0.0
    assert suggested_price_with_margin(100, 20) == 120.0


def test_pricing_service_builds_price_diff() -> None:
    decision, diff = PricingService().evaluate("SKU9", our_price=120, comp_price=100)
    assert decision.action == ActionType.RAISE
    assert diff.product_id == "SKU9"
    assert diff.diff == 20.0
    assert diff.suggested_price == 99.0          # يُقترح خفض السعر تحت المنافس
    assert diff.diff_pct == 20.0
