"""
engines/file_reader.py  v1.0 — وحدة قراءة الملفات المستقلة
════════════════════════════════════════════════════════════════

مسؤولية واحدة فقط: قراءة ملفات CSV/Excel وإرجاعها كـ DataFrame.

مبدأ التصميم (Separation of Concerns):
    - هذه الوحدة مسؤولة عن الـ I/O فقط.
    - لا تحتوي على أي منطق مطابقة أو تسعير.
    - محرك المطابقة (closed_loop_engine.py) يستقبل DataFrame جاهزاً فقط.
    - إذا أردت استبدال طريقة القراءة لاحقاً، لا تلمس سوى هذا الملف.

سلسلة اكتشاف الترميز (Encoding Detection Chain):
    1. utf-8-sig  — UTF-8 مع BOM (تصدير Excel/Windows)
    2. cp1256     — Windows-1256 (عربي Windows قديم) ← الأكثر شيوعاً بعد UTF-8
    3. utf-8      — UTF-8 بدون BOM
    4. latin-1    — لا يُعطي UnicodeDecodeError أبداً (fallback صامت مع تحذير)

⚠️  للتحليل والعرض فقط — لا يُعدِّل أي أسعار ولا يتصل بأي API ⚠️
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Callable, IO, List, Optional, Tuple, Union

import pandas as pd

from utils.data_sanitizer import sanitize_competitor_price_to_float

logger = logging.getLogger(__name__)

# ترتيب محاولات الترميز
_ENCODING_CHAIN: list[str] = ["utf-8-sig", "utf-8", "cp1256", "windows-1256", "latin-1"]


# ══════════════════════════════════════════════════════════════════════════════
#  1.  الدالة الأساسية — read_csv_safe
# ══════════════════════════════════════════════════════════════════════════════

def read_csv_safe(
    source: Union[str, Path, IO[bytes]],
    *,
    fallback_encoding: str = "latin-1",
    low_memory: bool = False,
) -> Tuple[pd.DataFrame, str]:
    """
    يقرأ ملف CSV مع اكتشاف الترميز تلقائياً.

    يجرب السلسلة: utf-8-sig → cp1256 → utf-8 → latin-1 (لا يفشل أبداً).

    Parameters
    ----------
    source : str | Path | file-like
        مسار الملف أو كائن ملف (مثل Streamlit UploadedFile).
    fallback_encoding : str
        الترميز الأخير الاحتياطي. latin-1 لا يُعطي خطأ مع أي بيانات.
    low_memory : bool
        تمرير إلى pandas. False أآمن للأعمدة المختلطة.

    Returns
    -------
    tuple[pd.DataFrame, str]
        (DataFrame المحمّل, اسم الترميز الذي نجح).

    Raises
    ------
    ValueError
        إذا كان الملف فارغاً تماماً بعد القراءة الناجحة.
    """
    # قراءة bytes مرة واحدة حتى نتجنب seek errors في كائنات Streamlit
    if hasattr(source, "read"):
        raw_bytes: bytes = source.read()
        if hasattr(source, "seek"):
            source.seek(0)
    else:
        raw_bytes = Path(source).read_bytes()

    encodings = list(_ENCODING_CHAIN)
    if fallback_encoding not in encodings:
        encodings.append(fallback_encoding)

    last_error: Exception = RuntimeError("لم يتم تحديد خطأ")

    for enc in encodings:
        try:
            df = pd.read_csv(
                io.BytesIO(raw_bytes),
                dtype=str,
                encoding=enc,
                low_memory=low_memory,
            )
            if df.empty:
                raise ValueError(f"الملف فارغ بعد القراءة بترميز {enc!r}.")
            logger.info("✅ قُرئ الملف بترميز: %s (%d صف)", enc, len(df))
            return df, enc

        except (UnicodeDecodeError, UnicodeError, LookupError) as exc:
            logger.debug("⏩ فشل الترميز %r: %s — جرب التالي…", enc, exc)
            last_error = exc
            continue

        except pd.errors.EmptyDataError as exc:
            raise ValueError("الملف فارغ أو لا يحتوي على أعمدة صالحة.") from exc

    # آخر محاولة: latin-1 مع errors='replace' (لا ينهار أبداً)
    logger.warning(
        "⚠️ فشلت جميع الترميزات القياسية — القراءة بـ latin-1/replace "
        "(احتمال ظهور رموز مشوهة في الأسماء العربية)."
    )
    df = pd.read_csv(
        io.BytesIO(raw_bytes),
        dtype=str,
        encoding="latin-1",
        encoding_errors="replace",
        low_memory=low_memory,
    )
    return df, "latin-1 (replace)"


# ══════════════════════════════════════════════════════════════════════════════
#  2.  واجهة مبسطة للاستخدام السريع في app.py
# ══════════════════════════════════════════════════════════════════════════════

def load_csv(
    source: Union[str, Path, IO[bytes]],
) -> pd.DataFrame:
    """
    يُحمِّل CSV ويُرجع DataFrame فقط (بدون بيانات الترميز).

    للاستخدام السريع في Streamlit:
        df = load_csv(uploaded_file)
    """
    df, enc = read_csv_safe(source)
    return df


# ══════════════════════════════════════════════════════════════════════════════
#  3.  تصدير آمن مع طابع زمني لمنع الكتابة فوق الملفات القديمة
# ══════════════════════════════════════════════════════════════════════════════

def _header_norm_key(col) -> str:
    """مفتاح مقارنة مرن لأسماء أعمدة تصدير سلة/الكشط."""
    return (
        str(col)
        .strip()
        .lower()
        .replace("\ufeff", "")
        .replace("\u200f", "")
        .replace("\u200e", "")
        .replace(" ", "")
    )


def _first_column_matching(df: pd.DataFrame, predicates: List[Callable[[str], bool]]) -> Optional[str]:
    for col in df.columns:
        key = _header_norm_key(col)
        for pred in predicates:
            try:
                if pred(key):
                    return str(col)
            except Exception:
                continue
    return None


def normalize_salla_export_competitor_df(
    df: pd.DataFrame,
    *,
    competitor_label: str = "",
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """
    يحوّل تصدير سلة/كشط (أسماء أعمدة CSS) إلى أعمدة يفهمها محرك المطابقة.

    تعيين إلزامي (مرن — يقبل اختلاف لاحقة الـ hash في class names):
      - abs-size href  → رابط المنتج
      - w-full src     → صورة المنتج
      - styles_productCard__name__* → المنتج (عمود «المنتج»)
      - text-sm-2      → سعر المنتج

    يعيد (dataframe, None) عند النجاح، أو (None, رسالة_خطأ).
    """
    if df is None or df.empty:
        return None, "الملف لا يحتوي على صفوف بيانات."

    work = df.copy()
    work.columns = [str(c).strip() for c in work.columns]

    url_predicates: List[Callable[[str], bool]] = [
        lambda k: "abs-size" in k and "href" in k,
        lambda k: "abssize" in k and "href" in k,
        lambda k: k == "product_url" or k.endswith("producturl"),
    ]
    img_predicates: List[Callable[[str], bool]] = [
        lambda k: "w-full" in k and "src" in k,
        lambda k: "wfull" in k and "src" in k,
        lambda k: k in ("image_url", "product_image", "imageurl"),
    ]
    name_predicates: List[Callable[[str], bool]] = [
        lambda k: "styles_productcard__name__" in k,
        lambda k: "productcard__name__" in k,
        lambda k: k in ("product_name", "productname", "title", "name"),
        lambda k: "product" in k and "name" in k and "card" in k,
    ]
    price_predicates: List[Callable[[str], bool]] = [
        lambda k: re.search(r"text-sm-2(?:[^0-9]|$)", k) is not None,
        lambda k: k == "text-sm-2" or k.endswith("text-sm-2"),
        lambda k: k in ("price", "product_price", "السعر", "سعرالمنتج"),
    ]

    c_url = _first_column_matching(work, url_predicates)
    c_img = _first_column_matching(work, img_predicates)
    c_name = _first_column_matching(work, name_predicates)
    c_price = _first_column_matching(work, price_predicates)

    if not c_name:
        sample = "، ".join(f"«{c}»" for c in list(work.columns)[:12])
        return None, (
            "لم يُعثر على عمود اسم المنتج المتوقع (مثل styles_productCard__name__…). "
            f"أول الأعمدة: {sample}"
        )
    if not c_price:
        sample = "، ".join(f"«{c}»" for c in list(work.columns)[:12])
        return None, (
            "لم يُعثر على عمود السعر المتوقع (مثل text-sm-2). "
            f"أول الأعمدة: {sample}"
        )

    names = work[c_name].fillna("").astype(str).str.strip()
    prices = work[c_price].map(sanitize_competitor_price_to_float)

    out = pd.DataFrame(
        {
            "المنتج": names,
            "سعر المنتج": prices,
        }
    )
    if c_img and c_img in work.columns:
        out["صورة المنتج"] = work[c_img].fillna("").astype(str).str.strip()
    else:
        out["صورة المنتج"] = ""

    if c_url and c_url in work.columns:
        out["رابط المنتج"] = work[c_url].fillna("").astype(str).str.strip()
    else:
        out["رابط المنتج"] = ""

    label = (competitor_label or "").strip() or "منافس"
    out["المنافس"] = label

    out = out[(out["المنتج"] != "") & (out["المنتج"].str.lower() != "nan")].reset_index(drop=True)
    if out.empty:
        return None, "بعد التنظيف لم يبقَ أي صف يحتوي على اسم منتج صالح."

    return out, None


def load_competitor_csv_for_matching(
    file_obj: Union[str, Path, IO[bytes]],
    *,
    competitor_label: str = "",
) -> Tuple[Optional[pd.DataFrame], Optional[str], str]:
    """
    يقرأ CSV منافس بترميز آمن ثم يطبّق normalize_salla_export_competitor_df.

    Returns
    -------
    (df, error, encoding_used)
    """
    try:
        raw_df, enc = read_csv_safe(file_obj)
    except Exception as exc:
        return None, f"فشل قراءة CSV: {exc}", ""

    norm, err = normalize_salla_export_competitor_df(raw_df, competitor_label=competitor_label)
    return norm, err, enc


def make_export_filename(base_name: str, extension: str = "xlsx") -> str:
    """
    يُنشئ اسم ملف يحمل طابعاً زمنياً بدقة الثانية لمنع الكتابة فوق تقارير سابقة.

    مثال:
        make_export_filename("تقرير_المطابقة")
        → "تقرير_المطابقة_2024-11-15_143022.xlsx"

    Parameters
    ----------
    base_name : str
        الاسم الأساسي بدون امتداد.
    extension : str
        امتداد الملف بدون نقطة (افتراضي: xlsx).

    Returns
    -------
    str
        اسم الملف الكامل مع الطابع الزمني.
    """
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    ext = extension.lstrip(".")
    return f"{base_name}_{timestamp}.{ext}"
