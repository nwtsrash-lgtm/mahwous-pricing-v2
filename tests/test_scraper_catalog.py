"""tests/test_scraper_catalog.py — اختبارات تحميل الكتالوج وإدارة المنافسين."""
import pandas as pd
import pytest

from conf.constants import COL_OUR_ID, COL_OUR_NAME, COL_OUR_PRICE
from services.catalog_service import load_catalog_bytes, map_columns, name_column
from services.scraper_service import Competitor, ScraperService, extract_store_id

# CSV بصيغة سلة: صف meta «بيانات المنتج» ثم الترويسة الحقيقية ثم المنتجات.
_SALLA_CSV = (
    "بيانات المنتج,,,\n"
    "No.,أسم المنتج,سعر المنتج,الماركة\n"
    "1,عطر شانيل شانس 100 مل,250,Chanel\n"
    "2,عطر ديور سوفاج 100 مل,300,Dior\n"
).encode("utf-8")

_PLAIN_CSV = "معرف_المنتج,المنتج,السعر\nA,عطر,100\n".encode("utf-8")


def test_load_salla_skips_meta_and_maps_columns() -> None:
    df = load_catalog_bytes(_SALLA_CSV, "store.csv")
    assert len(df) == 2
    assert COL_OUR_NAME in df.columns and COL_OUR_PRICE in df.columns
    assert COL_OUR_ID in df.columns
    assert df.iloc[0][COL_OUR_NAME] == "عطر شانيل شانس 100 مل"


def test_load_plain_csv_without_meta() -> None:
    df = load_catalog_bytes(_PLAIN_CSV, "plain.csv")
    assert len(df) == 1 and df.iloc[0][COL_OUR_NAME] == "عطر"


def test_map_columns_and_name_detection() -> None:
    raw = pd.DataFrame({"أسم المنتج": ["x"], "سعر المنتج": [9]})
    mapped = map_columns(raw)
    assert COL_OUR_NAME in mapped.columns
    assert name_column(mapped) == COL_OUR_NAME
    # استدلال حين غياب العمود الداخلي
    assert name_column(pd.DataFrame({"product name": ["x"]})) == "product name"


def test_extract_store_id() -> None:
    assert extract_store_id("https://mahally.com/stores/216339537/") == 216339537
    assert extract_store_id("https://mahally.com/stores/42") == 42
    assert extract_store_id("https://example.com/no-id") is None
    assert extract_store_id("") is None


def test_scraper_add_list_remove(tmp_path) -> None:
    svc = ScraperService(links_file=tmp_path / "comps.json")
    assert svc.list_competitors() == []
    comp = svc.add_competitor("متجر العطور", "https://mahally.com/stores/100/")
    assert isinstance(comp, Competitor) and comp.mahally_store_id == 100
    listed = svc.list_competitors()
    assert len(listed) == 1 and listed[0].name == "متجر العطور"
    assert svc.remove_competitor(100) is True
    assert svc.list_competitors() == []
    assert svc.remove_competitor(999) is False  # غير موجود


def test_scraper_add_validations(tmp_path) -> None:
    svc = ScraperService(links_file=tmp_path / "c.json")
    with pytest.raises(Exception):
        svc.add_competitor("x", "https://bad-url.com")        # لا معرّف
    svc.add_competitor("A", "https://mahally.com/stores/5/")
    with pytest.raises(Exception):
        svc.add_competitor("A2", "https://mahally.com/stores/5/")  # تكرار
