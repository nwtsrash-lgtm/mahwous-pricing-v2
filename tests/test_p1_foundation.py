"""tests/test_p1_foundation.py — اختبارات طبقة الأساس (P1).

تتحقّق من: بناء النماذج من الأعمدة العربية، عدم قابلية التغيير (frozen)،
تطبيع الأسعار، عتبات الثقة 82/65، قواعد الإسقاط، وحارس حفظ البيانات.
"""
import pytest

from conf.constants import (
    COL_OUR_NAME,
    KNOWN_BRANDS,
    MATCH_CONFIRMED_THRESHOLD,
    MISS_STOPWORDS,
)
from conf.settings import Settings
from core.enums import ConfidenceLevel, Gender, ItemType, SectionType
from core.exceptions import DataLossError
from core.models import (
    CompetitorProduct,
    Product,
    ReconciliationReport,
)


def test_product_builds_from_arabic_aliases() -> None:
    row = {"معرف_المنتج": "SKU1", "المنتج": "عطر شانيل", "السعر": "250.5"}
    product = Product.model_validate(row)
    assert product.product_id == "SKU1"
    assert product.name == "عطر شانيل"
    assert product.price == 250.5 and isinstance(product.price, float)


def test_product_is_frozen() -> None:
    product = Product.model_validate({"معرف_المنتج": "A", "المنتج": "x"})
    with pytest.raises(Exception):
        product.price = 9.9  # type: ignore[misc]


def test_price_coercion_matches_pandas_coerce() -> None:
    comp = CompetitorProduct.model_validate(
        {"المنافس": "s", "منتج_المنافس": "y", "سعر_المنافس": "غير رقم"}
    )
    assert comp.price == 0.0


@pytest.mark.parametrize(
    "score,expected",
    [(90, ConfidenceLevel.CONFIRMED), (70, ConfidenceLevel.REVIEW),
     (40, ConfidenceLevel.NONE), (82, ConfidenceLevel.CONFIRMED),
     (65, ConfidenceLevel.REVIEW)],
)
def test_confidence_thresholds(score: int, expected: ConfidenceLevel) -> None:
    assert ConfidenceLevel.from_score(score) == expected


def test_item_type_drop_and_section_prefix() -> None:
    assert ItemType.GIFT_SET.droppable_from_missing
    assert not ItemType.PERFUME.droppable_from_missing
    assert SectionType.PRICE_RAISE.prefix == "🔴"
    assert SectionType.EXCLUDED.prefix == "⚪"
    assert Gender.MALE.is_explicit and not Gender.UNISEX.is_explicit


def test_reconciliation_balanced_passes() -> None:
    report = ReconciliationReport.from_sections(
        100,
        {"price_raise": 40, "price_lower": 30, "approved": 20,
         "review": 5, "excluded": 5},
    )
    assert report.is_balanced and report.gap == 0
    report.assert_balanced()  # must not raise


def test_reconciliation_gap_raises_data_loss() -> None:
    report = ReconciliationReport.from_sections(
        100,
        {"price_raise": 40, "price_lower": 30, "approved": 20,
         "review": 5, "excluded": 3},
    )
    assert report.gap == 2
    with pytest.raises(DataLossError) as exc:
        report.assert_balanced()
    assert exc.value.gap == 2


def test_settings_load_does_not_crash() -> None:
    settings = Settings.load()
    assert isinstance(settings.any_ai_configured, bool)
    assert settings.db_path.endswith("perfume_pricing.db")


def test_constants_ground_truth() -> None:
    assert MATCH_CONFIRMED_THRESHOLD == 82
    assert COL_OUR_NAME == "المنتج"
    assert "عطر" in MISS_STOPWORDS
    assert len(KNOWN_BRANDS) > 140
