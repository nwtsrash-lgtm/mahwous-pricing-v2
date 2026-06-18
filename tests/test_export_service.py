"""tests/test_export_service.py — اختبارات التصدير (P3)."""
import pandas as pd

from services.export_service import (
    SALLA_SHAMEL_COLUMNS,
    ExportService,
    _clean_pid,
    _extract_no,
    _section_price,
    to_make_payload,
)


def test_salla_columns_exact_count_and_first() -> None:
    assert len(SALLA_SHAMEL_COLUMNS) == 40
    assert SALLA_SHAMEL_COLUMNS[0] == "النوع "       # مسافة لاحقة محفوظة
    assert SALLA_SHAMEL_COLUMNS[-1] == "[3] الصورة / اللون"


def test_clean_pid() -> None:
    assert _clean_pid("100.0") == "100"
    assert _clean_pid("0") == "" and _clean_pid("nan") == "" and _clean_pid(None) == ""
    assert _clean_pid("ABC") == "ABC"


def test_extract_no_reads_aliases() -> None:
    assert _extract_no({"No.": "55.0"}) == "55"
    assert _extract_no({"NO": 77}) == "77"
    assert _extract_no({"رقم المنتج": "9"}) == "9"


def test_section_price_rules() -> None:
    assert _section_price("raise", 50, 100) == 99.0      # comp-1
    assert _section_price("lower", 200, 100) == 99.0     # comp-1
    assert _section_price("approved", 50, 100) == 50     # سعرنا
    assert _section_price("missing", 50, 100) == 100     # سعر المنافس


def test_make_payload_structure_and_context() -> None:
    df = pd.DataFrame([{
        "معرف_المنتج": "100.0", "المنتج": "عطر تجريبي", "السعر": 80,
        "سعر_المنافس": 100, "المنافس": "متجر س", "الفرق": -20,
        "نسبة_التطابق": 95, "القرار": "🟢 سعر أقل", "الماركة": "Chanel",
    }])
    payload = to_make_payload(df, section_type="lower")
    assert len(payload) == 1
    p = payload[0]
    assert p["product_id"] == "100" and p["name"] == "عطر تجريبي"
    assert p["price"] == 99.0 and p["section"] == "lower"
    assert "comp_name" not in p  # لا عمود منتج_المنافس ⇒ يُحذف الحقل
    assert p["competitor"] == "متجر س" and p["brand"] == "Chanel"
    assert p["match_score"] == 95 and p["decision"] == "🟢 سعر أقل"


def test_make_payload_skips_nameless_rows() -> None:
    df = pd.DataFrame([{"معرف_المنتج": "1", "المنتج": ""}])
    assert to_make_payload(df) == []


def test_post_to_make_with_injected_poster() -> None:
    class _Resp:
        status_code = 200

    captured = {}

    def poster(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _Resp()

    svc = ExportService(poster=poster)
    result = svc.post_to_make("http://hook", [{"NO": "1"}])
    assert result["success"] is True and result["status_code"] == 200
    assert captured["payload"] == {"products": [{"NO": "1"}]}


def test_to_csv_returns_text() -> None:
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    csv = ExportService.to_csv(df)
    assert "a,b" in csv and "x" in csv
