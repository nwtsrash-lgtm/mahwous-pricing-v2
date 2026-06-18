"""services/export_service.py — تصدير Make.com وSalla وCSV/Excel (نقل حرفي).

- Make.com: حمولة ``{NO, product_id, name, price, section, +سياق}`` بنفس مفاتيح
  ``make_helper.export_to_make_format`` (المفاتيح يعتمدها Make — ممنوع تغييرها).
- Salla: قائمة الأعمدة الأربعين بالترتيب الحرفي + التفاف على المُولّد القانوني
  (``salla_shamel_export`` يستورد Streamlit ⇒ يُستورَد كسولاً عند الاستدعاء فقط).
- CSV/Excel: توليد قياسي عبر pandas.

#PRESERVED_LOGIC: export_to_make_format (make_helper.py:169-249)،
SALLA_SHAMEL_COLUMNS (salla_shamel_export.py:28-69).
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Optional

import pandas as pd

from conf.constants import PROJECT_ROOT
from core.exceptions import ExportError

# 40 عمود سلة بالترتيب الحرفي (لاحظ المسافة اللاحقة في "النوع ").
SALLA_SHAMEL_COLUMNS: list[str] = [
    "النوع ", "أسم المنتج", "تصنيف المنتج", "صورة المنتج", "وصف صورة المنتج",
    "نوع المنتج", "سعر المنتج", "الوصف", "هل يتطلب شحن؟", "رمز المنتج sku",
    "سعر التكلفة", "السعر المخفض", "تاريخ بداية التخفيض", "تاريخ نهاية التخفيض",
    "اقصي كمية لكل عميل", "إخفاء خيار تحديد الكمية", "اضافة صورة عند الطلب",
    "الوزن", "وحدة الوزن", "الماركة", "العنوان الترويجي", "تثبيت المنتج",
    "الباركود", "السعرات الحرارية", "MPN", "GTIN", "خاضع للضريبة ؟",
    "سبب عدم الخضوع للضريبة",
    "[1] الاسم", "[1] النوع", "[1] القيمة", "[1] الصورة / اللون",
    "[2] الاسم", "[2] النوع", "[2] القيمة", "[2] الصورة / اللون",
    "[3] الاسم", "[3] النوع", "[3] القيمة", "[3] الصورة / اللون",
]


def _safe_float(val: Any, default: float = 0.0) -> float:
    """#PRESERVED_LOGIC make_helper.py:72-78."""
    try:
        if val is None or str(val).strip() in ("", "nan", "None", "NaN"):
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


def _clean_pid(raw: Any) -> str:
    """product_id كـ ``str(int(float(value)))``. #PRESERVED_LOGIC make_helper.py:82-90."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if text in ("", "nan", "None", "NaN", "0", "0.0"):
        return ""
    try:
        return str(int(float(text)))
    except (ValueError, TypeError):
        return text


def _extract_no(row: Any) -> str:
    """رقم المنتج (Primary Key سلة/زد). #PRESERVED_LOGIC make_helper.py:105-120."""
    if row is None:
        return ""
    getter = row.get if hasattr(row, "get") else (lambda k, d=None: d)
    raw = (
        getter("No.") or getter("NO") or getter("no") or getter("No")
        or getter("رقم_المنتج") or getter("رقم المنتج")
        or getter("catalog_no") or getter("product_no") or ""
    )
    return _clean_pid(raw)


def _section_price(section_type: str, our_price: float, comp_price: float) -> float:
    """السعر حسب القسم. #PRESERVED_LOGIC make_helper.py:206-215."""
    if section_type in ("raise", "lower"):
        return round(comp_price - 1, 2) if comp_price > 0 else our_price
    if section_type in ("approved", "update"):
        return our_price
    return comp_price if comp_price > 0 else our_price


def _add_context(product: dict[str, Any], row: Any) -> None:
    """يضيف الحقول السياقية المشروطة. #PRESERVED_LOGIC make_helper.py:234-245."""
    comp_name = str(row.get("منتج_المنافس", ""))
    comp_src = str(row.get("المنافس", ""))
    diff = _safe_float(row.get("الفرق", 0))
    match_pct = _safe_float(row.get("نسبة_التطابق", 0))
    decision = str(row.get("القرار", ""))
    brand = str(row.get("الماركة", ""))
    if comp_name and comp_name not in ("nan", "None", "—"):
        product["comp_name"] = comp_name
    if comp_src and comp_src not in ("nan", "None"):
        product["competitor"] = comp_src
    if diff:
        product["price_diff"] = diff
    if match_pct:
        product["match_score"] = match_pct
    if decision and decision not in ("nan", "None"):
        product["decision"] = decision
    if brand and brand not in ("nan", "None"):
        product["brand"] = brand


def _build_make_product(row: Any, section_type: str) -> Optional[dict[str, Any]]:
    """يبني منتج Make واحداً أو None إن غاب الاسم. #PRESERVED_LOGIC make_helper.py:179-247."""
    product_no = _extract_no(row)
    product_id = product_no or _clean_pid(
        row.get("معرف_المنتج") or row.get("product_id") or row.get("معرف المنتج")
        or row.get("sku") or row.get("SKU") or ""
    )
    name = (
        str(row.get("المنتج", "")) or str(row.get("منتج_المنافس", ""))
        or str(row.get("أسم المنتج", "")) or str(row.get("اسم المنتج", ""))
        or str(row.get("name", "")) or ""
    ).strip()
    if name in ("", "nan", "None"):
        return None
    comp_price = _safe_float(row.get("سعر_المنافس", 0))
    our_price = _safe_float(
        row.get("السعر", 0) or row.get("سعر المنتج", 0) or row.get("price", 0) or 0
    )
    product: dict[str, Any] = {
        "NO": product_no,
        "product_id": product_id,
        "name": name,
        "price": float(_section_price(section_type, our_price, comp_price)),
        "section": section_type,
    }
    _add_context(product, row)
    return product


def to_make_payload(df: pd.DataFrame | None, section_type: str = "update") -> list[dict[str, Any]]:
    """يحوّل DataFrame إلى قائمة منتجات Make. #PRESERVED_LOGIC make_helper.py:169-249."""
    if df is None or (hasattr(df, "empty") and df.empty):
        return []
    products: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        product = _build_make_product(row, section_type)
        if product is not None:
            products.append(product)
    return products


def to_csv(df: pd.DataFrame, path: Optional[str] = None) -> str:
    """يولّد CSV (UTF-8-SIG لدعم العربية في Excel)؛ يكتب للمسار إن مُرّر."""
    csv_text = df.to_csv(index=False)
    if path:
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write(csv_text)
    return csv_text


class ExportService:
    """خدمة التصدير: Make payload + Salla + CSV/Excel + إرسال webhook."""

    def __init__(
        self, poster: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._poster = poster

    def make_payload(
        self, df: pd.DataFrame | None, section_type: str = "update",
    ) -> list[dict[str, Any]]:
        return to_make_payload(df, section_type)

    def post_to_make(self, url: str, products: list[dict[str, Any]]) -> dict[str, Any]:
        """يرسل الحمولة لـ Make (poster محقون للاختبار، وإلا requests)."""
        if not url:
            raise ExportError("Webhook URL غير مهيّأ")
        poster = self._poster
        if poster is None:  # pragma: no cover - شبكة حقيقية
            import requests

            poster = requests.post
        try:
            resp = poster(
                url, json={"products": products},
                headers={"Content-Type": "application/json"}, timeout=30,
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        code = getattr(resp, "status_code", None)
        return {"success": code in (200, 204), "status_code": code}

    def salla_dataframe(self, missing_df: pd.DataFrame, *args: Any, **kwargs: Any):
        """يلتفّ على مُولّد سلة القانوني (استيراد كسول — يستورد Streamlit)."""
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from utils.salla_shamel_export import (  # type: ignore
                build_salla_shamel_dataframe,
            )
        except Exception as exc:
            raise ExportError("تعذّر تحميل مُصدّر سلة الشامل", error=str(exc)) from exc
        return build_salla_shamel_dataframe(missing_df, *args, **kwargs)

    @staticmethod
    def to_csv(df: pd.DataFrame, path: Optional[str] = None) -> str:
        return to_csv(df, path)

    @staticmethod
    def to_excel(df: pd.DataFrame, path: str) -> str:
        """يكتب Excel (يتطلب openpyxl)؛ يرفع ExportError إن غاب المحرّك."""
        try:
            df.to_excel(path, index=False)
        except Exception as exc:
            raise ExportError("تعذّر توليد Excel", error=str(exc)) from exc
        return path
