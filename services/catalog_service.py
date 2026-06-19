"""services/catalog_service.py — تحميل كتالوجنا وتوحيد أعمدته.

يقرأ كتالوجنا بصيغة سلة (صف meta «بيانات المنتج» + أعمدة سلة) أو CSV/Excel
عادي، ويوحّد أسماء الأعمدة إلى الأسماء الداخلية المستخدمة في التحليل.

يحلّ مشكلة حقيقية ظهرت باختبار بيانات فعلية: القراءة الساذجة تلتقط صفّ
الـmeta كترويسة فيفسد عمود الاسم. هنا نكتشف صفّ الـmeta ونتخطّاه.
"""
from __future__ import annotations

import io
from typing import Any

import pandas as pd

from conf.constants import (
    COL_BRAND,
    COL_OUR_ID,
    COL_OUR_NAME,
    COL_OUR_PRICE,
    COL_SIZE,
    COL_TYPE,
)
from core.exceptions import RepositoryError

_META_HEADER = "بيانات المنتج"

# أعمدة سلة → الأعمدة الداخلية للتحليل.
_SALLA_MAP: dict[str, str] = {
    "أسم المنتج": COL_OUR_NAME,
    "اسم المنتج": COL_OUR_NAME,
    "سعر المنتج": COL_OUR_PRICE,
    "No.": COL_OUR_ID,
    "الماركة": COL_BRAND,
    "النوع ": COL_TYPE,
    "نوع المنتج": COL_TYPE,
}


def _seek0(source: Any) -> None:
    if hasattr(source, "seek"):
        try:
            source.seek(0)
        except Exception:
            pass


def _is_excel(source: Any) -> bool:
    name = str(getattr(source, "name", source)).lower()
    return name.endswith((".xlsx", ".xls"))


def _read_raw(source: Any) -> pd.DataFrame:
    """يقرأ الملف متخطّياً صفّ الـmeta «بيانات المنتج» إن وُجد."""
    reader = pd.read_excel if _is_excel(source) else pd.read_csv
    _seek0(source)
    try:
        peek = reader(source, header=None, nrows=1)
        first_cell = str(peek.iloc[0, 0]).strip()
    except Exception:
        first_cell = ""
    header_row = 1 if first_cell == _META_HEADER else 0
    _seek0(source)
    try:
        return reader(source, header=header_row)
    except Exception as exc:
        raise RepositoryError("تعذّرت قراءة ملف الكتالوج", error=str(exc)) from exc


def map_columns(df: pd.DataFrame) -> pd.DataFrame:
    """يوحّد أعمدة سلة إلى الأسماء الداخلية (لا يغيّر ما لا يعرفه)."""
    rename = {src: dst for src, dst in _SALLA_MAP.items()
              if src in df.columns and dst not in df.columns}
    return df.rename(columns=rename)


def name_column(df: pd.DataFrame) -> str:
    """يحدّد عمود اسم المنتج (الداخلي أولاً، ثم استدلال)."""
    if COL_OUR_NAME in df.columns:
        return COL_OUR_NAME
    for col in df.columns:
        if any(key in str(col) for key in ("اسم", "أسم", "المنتج", "name", "product")):
            return col
    return df.columns[0]


def load_catalog(source: Any) -> pd.DataFrame:
    """يحمّل كتالوجنا جاهزاً للتحليل (قراءة + توحيد أعمدة)."""
    df = _read_raw(source)
    if df is None or df.empty:
        raise RepositoryError("ملف الكتالوج فارغ أو غير صالح")
    df = map_columns(df)
    # تنظيف صفوف بلا اسم
    ncol = name_column(df)
    df = df[df[ncol].notna() & (df[ncol].astype(str).str.strip() != "")]
    return df.reset_index(drop=True)


def load_catalog_bytes(data: bytes, filename: str) -> pd.DataFrame:
    """يحمّل من bytes (للاستخدام مع الرفع/الاختبار)."""
    buffer = io.BytesIO(data)
    buffer.name = filename  # type: ignore[attr-defined]
    return load_catalog(buffer)
