"""
engines/pricing_engine.py  v1.0 — محرك التسعير والمطابقة (الشامل)
═══════════════════════════════════════════════════════════════════════
الهدف:
  قراءة كتالوج سلة (تنسيق الشامل) ومقارنته بملفات المنافسين
  مع تصنيف النتائج يدوياً إلى 3 أقسام واضحة.

⚠️  للتحليل والعرض فقط — لا يُعدِّل أي أسعار ولا يتصل بأي API ⚠️

الهيكلة:
  1. clean_price()              — تنظيف نصوص الأسعار → float آمن
  2. load_base_catalog()        — قراءة الشامل ، No. كـ Primary Key
  3. load_competitor_file()     — قراءة ملف منافس (اسم + سعر)
  4. match_competitor_products()— RapidFuzz vectorized matching
  5. generate_pricing_report()  — الدالة الرئيسية → 3 DataFrames + ملخص

المكتبات المستخدمة:
  - rapidfuzz  (المطابقة التقريبية — الأسرع والأحدث)
  - pandas     (معالجة البيانات بعمليات vectorized)
  - re         (تنظيف الأسعار النصية)
  - logging    (تسجيل التحذيرات بدلاً من رفع الاستثناءات)
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from rapidfuzz import fuzz
from rapidfuzz import process as rf_process

# ─── إعداد المُسجِّل ────────────────────────────────────────────────────────
logger = logging.getLogger("pricing_engine")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] pricing_engine: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# ─── الثوابت الإلزامية ───────────────────────────────────────────────────────
DEFAULT_MATCH_THRESHOLD: float = 75.0
"""أدنى نسبة تطابق مقبولة (0-100). ما دونها يُصنَّف مفقوداً."""

CATALOG_PK_COL: str = "No."
"""عمود المعرّف الفريد (Primary Key) في تنسيق سلة الشامل."""

CATALOG_NAME_COL: str = "أسم المنتج"
"""عمود اسم المنتج في كتالوج سلة."""

CATALOG_PRICE_COL: str = "سعر المنتج"
"""عمود سعر المنتج في كتالوج سلة."""


# ══════════════════════════════════════════════════════════════════════════════
# 1.  تنظيف الأسعار — clean_price
# ══════════════════════════════════════════════════════════════════════════════

def clean_price(raw: Any) -> Optional[float]:
    """
    يحوّل أي تمثيل لسعر المنتج إلى float آمن.

    يعالج الحالات التالية دون أن ينهار البرنامج:
        "150 ر.س"   →  150.0
        "1,250.50"  →  1250.5
        "  200  "   →  200.0
        ""  / None  →  None
        "غير محدد" →  None
        NaN         →  None

    Parameters
    ----------
    raw : Any
        القيمة الخام من خلية جدول البيانات (أي نوع).

    Returns
    -------
    float | None
        الرقم العشري بعد التنظيف، أو None إذا تعذّر الاستخراج.
    """
    if raw is None:
        return None

    # الحالة الأسرع: رقم حقيقي بالفعل
    try:
        val = float(raw)
        return None if pd.isna(val) else val
    except (ValueError, TypeError):
        pass

    # معالجة النصوص عبر regex
    try:
        text = str(raw).strip()
        if not text or text.lower() in ("nan", "none", "-", ""):
            return None

        # إزالة كل ما هو ليس رقماً أو فاصلة عشرية أو فاصلة آلاف
        digits_only = re.sub(r"[^\d.,]", "", text)

        # فواصل الآلاف: "1,250" → "1250"
        digits_only = digits_only.replace(",", "")

        if not digits_only:
            return None

        return float(digits_only)

    except Exception:  # noqa: BLE001
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 2.  تحميل كتالوج المتجر — load_base_catalog
# ══════════════════════════════════════════════════════════════════════════════

def load_base_catalog(
    file_path: str | Path,
    encoding: str = "utf-8-sig",
) -> pd.DataFrame:
    """
    يقرأ ملف CSV بتنسيق سلة الشامل ويُعيد DataFrame نظيفاً.

    القواعد الإلزامية:
    ──────────────────
    • يستخدم عمود ``No.`` (رقم منتج سلة) كـ Primary Key صارم.
    • أي صف يكون فيه ``No.`` فارغاً أو غير صالح → يُحذف مع تحذير في الـ Log.
      البرنامج لا يتوقف أبداً بسبب هذا.
    • يستخرج فقط 3 أعمدة: [No., أسم المنتج, سعر المنتج].
    • يُنظّف عمود الأسعار تلقائياً عبر ``clean_price()``.

    Parameters
    ----------
    file_path : str | Path
        مسار ملف CSV بتنسيق الشامل.
    encoding : str
        ترميز الملف. الافتراضي ``utf-8-sig`` لدعم BOM العربية.
        إذا فشل يُجرِّب ``cp1256`` تلقائياً.

    Returns
    -------
    pd.DataFrame
        أعمدة: [No. (str), أسم المنتج (str), سعر المنتج (float|NaN)]
        مُفهرَس من الصفر بعد إسقاط الصفوف الفارغة.

    Raises
    ------
    FileNotFoundError
        إذا لم يوجد الملف في المسار المحدد.
    KeyError
        إذا غابت أحد الأعمدة الثلاثة الإلزامية من الملف.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(
            f"❌ ملف الكتالوج غير موجود: {path}\n"
            "تأكد من صحة المسار وإعادة المحاولة."
        )

    logger.info("📂 قراءة كتالوج المتجر: %s", path.name)

    # محاولة القراءة مع فول-باك تلقائي للترميز
    try:
        raw_df: pd.DataFrame = pd.read_csv(
            path, encoding=encoding, dtype=str, low_memory=False
        )
    except UnicodeDecodeError:
        logger.warning(
            "⚠️ فشل ترميز '%s'، تجربة cp1256 ...", encoding
        )
        raw_df = pd.read_csv(
            path, encoding="cp1256", dtype=str, low_memory=False
        )

    # التحقق من الأعمدة الإلزامية
    required_cols = {CATALOG_PK_COL, CATALOG_NAME_COL, CATALOG_PRICE_COL}
    missing_cols = required_cols - set(raw_df.columns)
    if missing_cols:
        raise KeyError(
            f"❌ الأعمدة التالية غير موجودة في '{path.name}': {missing_cols}\n"
            f"الأعمدة المتاحة: {list(raw_df.columns)}"
        )

    # استخراج الأعمدة الثلاثة فقط (لا شيء زائد)
    df = raw_df[[CATALOG_PK_COL, CATALOG_NAME_COL, CATALOG_PRICE_COL]].copy()

    # ── إسقاط صفوف No. الفارغة أو غير الصالحة ─────────────────────────────
    total_before: int = len(df)

    pk_series = df[CATALOG_PK_COL].astype(str).str.strip()
    invalid_pk_mask: pd.Series = (
        df[CATALOG_PK_COL].isna()
        | (pk_series == "")
        | (pk_series.str.lower() == "nan")
        | (pk_series.str.lower() == "none")
    )
    dropped_count: int = int(invalid_pk_mask.sum())

    if dropped_count > 0:
        logger.warning(
            "⚠️ تم تجاهل %d صف (من أصل %d) بسبب عمود No. فارغ أو غير صالح.",
            dropped_count,
            total_before,
        )

    df = df[~invalid_pk_mask].copy()
    df[CATALOG_PK_COL] = df[CATALOG_PK_COL].astype(str).str.strip()

    # ── تنظيف أسماء المنتجات ────────────────────────────────────────────────
    df[CATALOG_NAME_COL] = (
        df[CATALOG_NAME_COL]
        .astype(str)
        .str.strip()
        .replace({"nan": pd.NA, "none": pd.NA, "None": pd.NA})
    )

    # ── تنظيف الأسعار عبر clean_price (Vectorized apply) ───────────────────
    df[CATALOG_PRICE_COL] = df[CATALOG_PRICE_COL].apply(clean_price)

    logger.info(
        "✅ كتالوج المتجر جاهز: %d منتج (أُسقط %d صف بـ No. فارغ).",
        len(df),
        dropped_count,
    )
    return df.reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════════════
# 3.  تحميل ملف المنافس — load_competitor_file
# ══════════════════════════════════════════════════════════════════════════════

def load_competitor_file(
    file_path: str | Path,
    name_col: str,
    price_col: str,
    competitor_name: Optional[str] = None,
    encoding: str = "utf-8-sig",
) -> pd.DataFrame:
    """
    يقرأ ملف CSV للمنافس ويُعيد DataFrame نظيفاً موحّد الأعمدة.

    Parameters
    ----------
    file_path : str | Path
        مسار ملف CSV للمنافس.
    name_col : str
        اسم عمود أسماء المنتجات في ملف المنافس.
    price_col : str
        اسم عمود الأسعار في ملف المنافس.
    competitor_name : str | None
        الاسم التعريفي للمنافس (للعرض). إذا تُرك فارغاً يُستخدم اسم الملف.
    encoding : str
        ترميز الملف. الافتراضي ``utf-8-sig``.

    Returns
    -------
    pd.DataFrame
        أعمدة ثابتة: [اسم_منتج_المنافس (str),
                       سعر_المنافس (float|NaN),
                       المنافس (str)]

    Raises
    ------
    FileNotFoundError
        إذا لم يوجد الملف.
    KeyError
        إذا غاب أحد الأعمدتين المطلوبتين من الملف.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"❌ ملف المنافس غير موجود: {path}")

    comp_label: str = competitor_name or path.stem
    logger.info("📂 قراءة ملف المنافس: %s (%s)", comp_label, path.name)

    try:
        raw_df: pd.DataFrame = pd.read_csv(
            path, encoding=encoding, dtype=str, low_memory=False
        )
    except UnicodeDecodeError:
        raw_df = pd.read_csv(
            path, encoding="cp1256", dtype=str, low_memory=False
        )

    # التحقق من الأعمدة قبل الاستخراج
    for col in (name_col, price_col):
        if col not in raw_df.columns:
            raise KeyError(
                f"❌ عمود '{col}' غير موجود في '{path.name}'.\n"
                f"الأعمدة المتاحة: {list(raw_df.columns)}"
            )

    df = raw_df[[name_col, price_col]].copy()
    df = df.rename(
        columns={name_col: "اسم_منتج_المنافس", price_col: "سعر_المنافس"}
    )

    # توحيد البيانات
    df["اسم_منتج_المنافس"] = df["اسم_منتج_المنافس"].astype(str).str.strip()
    df["سعر_المنافس"] = df["سعر_المنافس"].apply(clean_price)
    df["المنافس"] = comp_label

    # إسقاط الصفوف التي بلا اسم
    df = df[
        df["اسم_منتج_المنافس"].notna()
        & (df["اسم_منتج_المنافس"].str.strip() != "")
        & (df["اسم_منتج_المنافس"].str.lower() != "nan")
    ].copy()

    logger.info(
        "✅ ملف المنافس '%s' جاهز: %d منتج.", comp_label, len(df)
    )
    return df.reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════════════
# 4.  تطبيع الأسماء (مساعدة داخلية)
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_name(name: str) -> str:
    """
    تطبيع اسم المنتج لتحسين دقة المطابقة التقريبية.

    الخطوات:
    1. تحويل لأحرف صغيرة (ASCII).
    2. إزالة الأحرف الخاصة غير الأبجدية الرقمية (مع الإبقاء على العربية).
    3. ضغط المسافات المتكررة.

    Parameters
    ----------
    name : str
        الاسم الخام.

    Returns
    -------
    str
        النص المطبَّع والجاهز للمطابقة.
    """
    text = str(name).lower().strip()
    # الإبقاء على: أحرف ASCII + أرقام + عربية + مسافات
    text = re.sub(r"[^\w\s\u0600-\u06FF]", " ", text)
    # ضغط المسافات المتكررة إلى مسافة واحدة
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ══════════════════════════════════════════════════════════════════════════════
# 5.  محرك المطابقة — match_competitor_products
# ══════════════════════════════════════════════════════════════════════════════

def match_competitor_products(
    catalog_df: pd.DataFrame,
    competitor_df: pd.DataFrame,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> pd.DataFrame:
    """
    يُطابق منتجات المنافس مع كتالوج المتجر باستخدام RapidFuzz.

    الخوارزمية (Vectorized):
    ─────────────────────────
    1. Pre-normalize: تطبيع جميع أسماء الكتالوج مرة واحدة في قائمة.
    2. لكل منتج منافس → ``process.extractOne`` مقابل القائمة المطبَّعة.
    3. إذا score ≥ threshold → مطابقة ناجحة وربط بيانات الصف.
    4. إذا score < threshold أو لم يُوجد → يُصنَّف "مفقود".

    Parameters
    ----------
    catalog_df : pd.DataFrame
        كتالوج المتجر الناتج من ``load_base_catalog()``.
    competitor_df : pd.DataFrame
        ملف المنافس الناتج من ``load_competitor_file()``.
    threshold : float
        أدنى نسبة تطابق مقبولة (0-100). الافتراضي 75.

    Returns
    -------
    pd.DataFrame
        أعمدة:
        اسم_منتج_المنافس | سعر_المنافس | المنافس |
        No. | أسم_المنتج_لدينا | سعر_المنتج_لدينا |
        نسبة_التطابق | حالة_المطابقة
    """
    comp_label: str = (
        competitor_df["المنافس"].iloc[0]
        if not competitor_df.empty
        else "مجهول"
    )
    logger.info(
        "🔍 بدء مطابقة %d منتج من '%s' — عتبة=%.0f%%",
        len(competitor_df),
        comp_label,
        threshold,
    )
    t_start = time.perf_counter()

    # ── Pre-normalize catalog (مرة واحدة لكل الكتالوج) ─────────────────────
    catalog_names_norm: List[str] = [
        _normalize_name(n) for n in catalog_df[CATALOG_NAME_COL].fillna("")
    ]

    # ── المطابقة صف بصف مع استخدام RapidFuzz Vectorized ──────────────────
    result_rows: List[Dict[str, Any]] = []

    for _, comp_row in competitor_df.iterrows():
        comp_name_raw: str = comp_row["اسم_منتج_المنافس"]
        comp_price: Optional[float] = comp_row["سعر_المنافس"]

        query_norm = _normalize_name(comp_name_raw)

        # اسم فارغ بعد التطبيع → مفقود تلقائياً
        if not query_norm:
            result_rows.append(
                _build_row(comp_name_raw, comp_price, comp_label, matched=False)
            )
            continue

        # RapidFuzz extractOne — يُجري المقارنة الكاملة داخلياً بـ C
        match_result = rf_process.extractOne(
            query_norm,
            catalog_names_norm,
            scorer=fuzz.token_set_ratio,
            score_cutoff=threshold,
        )

        if match_result is None:
            # لم يتجاوز العتبة → مفقود
            result_rows.append(
                _build_row(comp_name_raw, comp_price, comp_label, matched=False)
            )
        else:
            _matched_str, score, catalog_idx = match_result
            cat_row = catalog_df.iloc[catalog_idx]
            result_rows.append(
                _build_row(
                    comp_name_raw,
                    comp_price,
                    comp_label,
                    matched=True,
                    score=float(score),
                    pk=cat_row[CATALOG_PK_COL],
                    our_name=cat_row[CATALOG_NAME_COL],
                    our_price=cat_row[CATALOG_PRICE_COL],
                )
            )

    elapsed = time.perf_counter() - t_start
    matched_count = sum(1 for r in result_rows if r["حالة_المطابقة"] == "متطابق")
    logger.info(
        "✅ انتهت المطابقة: %d/%d نجح في %.2f ثانية.",
        matched_count,
        len(result_rows),
        elapsed,
    )
    return pd.DataFrame(result_rows)


def _build_row(
    comp_name: str,
    comp_price: Optional[float],
    comp_label: str,
    *,
    matched: bool,
    score: float = 0.0,
    pk: Optional[str] = None,
    our_name: Optional[str] = None,
    our_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    يبني صفاً موحداً لجدول النتائج (مطابق أو مفقود).

    Parameters
    ----------
    comp_name : str
        اسم المنتج من ملف المنافس.
    comp_price : float | None
        سعر المنافس بعد التنظيف.
    comp_label : str
        الاسم التعريفي للمنافس.
    matched : bool
        True → مطابقة ناجحة, False → مفقود.
    score : float
        نسبة التطابق (0-100). صفر للمفقودات.
    pk : str | None
        رقم No. من كتالوج متجرنا (فقط للمطابقات).
    our_name : str | None
        اسم المنتج في كتالوج متجرنا (فقط للمطابقات).
    our_price : float | None
        سعر المنتج في متجرنا (فقط للمطابقات).

    Returns
    -------
    dict
        صف جاهز للإضافة إلى قائمة النتائج.
    """
    return {
        "اسم_منتج_المنافس": comp_name,
        "سعر_المنافس":       comp_price,
        "المنافس":           comp_label,
        "No.":               pk,
        "أسم_المنتج_لدينا": our_name,
        "سعر_المنتج_لدينا": our_price,
        "نسبة_التطابق":      round(score, 2),
        "حالة_المطابقة":    "متطابق" if matched else "مفقود",
    }


# ══════════════════════════════════════════════════════════════════════════════
# 6.  التقسيم الثلاثي — _split_into_three_sections
# ══════════════════════════════════════════════════════════════════════════════

def _split_into_three_sections(
    matched_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    يقسّم نتائج المطابقة إلى 3 DataFrames مستقلة.

    القسم الأول  (cheaper)   — منافس أقل:  سعر المنافس < سعرنا
    القسم الثاني (pricier)   — منافس أعلى: سعر المنافس >= سعرنا
                               (يشمل الحالات التي ينعدم فيها أحد الأسعار)
    القسم الثالث (not_found) — مفقودات:   فشلت المطابقة

    Parameters
    ----------
    matched_df : pd.DataFrame
        ناتج ``match_competitor_products()``.

    Returns
    -------
    tuple[DataFrame, DataFrame, DataFrame]
        (cheaper_df, pricier_df, not_found_df)
        كل واحد مُفهرَس من الصفر.
    """
    # فصل المفقودات أولاً
    is_matched = matched_df["حالة_المطابقة"] == "متطابق"
    not_found_df = matched_df[~is_matched].copy().reset_index(drop=True)
    matched_only  = matched_df[is_matched].copy()

    # فصل الصفوف التي يكتمل فيها كلا السعرين لإجراء المقارنة
    both_prices = (
        matched_only["سعر_المنافس"].notna()
        & matched_only["سعر_المنتج_لدينا"].notna()
    )
    valid_for_compare = matched_only[both_prices].copy()
    no_price_rows     = matched_only[~both_prices].copy()

    # المقارنة الفعلية (Vectorized — لا loop)
    cheaper_mask = (
        valid_for_compare["سعر_المنافس"] < valid_for_compare["سعر_المنتج_لدينا"]
    )
    cheaper_df = valid_for_compare[cheaper_mask].copy()
    pricier_raw = valid_for_compare[~cheaper_mask].copy()

    # دمج الصفوف عديمة السعر مع "أعلى أو مساوٍ" (للعرض اليدوي)
    pricier_df = pd.concat(
        [pricier_raw, no_price_rows], ignore_index=True
    )

    # إضافة عمود الفرق للتسهيل القرائي (فرق سالب = منافس أرخص)
    for df_part in (cheaper_df, pricier_df):
        if not df_part.empty:
            df_part["فرق_السعر"] = (
                df_part["سعر_المنافس"].fillna(0)
                - df_part["سعر_المنتج_لدينا"].fillna(0)
            ).round(2)

    return (
        cheaper_df.reset_index(drop=True),
        pricier_df.reset_index(drop=True),
        not_found_df,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 7.  الدالة الرئيسية — generate_pricing_report
# ══════════════════════════════════════════════════════════════════════════════

def generate_pricing_report(
    catalog_path: str | Path,
    competitor_files: List[Dict[str, str]],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> Dict[str, Any]:
    """
    الدالة الرئيسية لمحرك التسعير والمطابقة.

    ⚠️ للتحليل والعرض فقط — لا يُعدِّل أي أسعار ولا يتصل بأي API ⚠️

    Parameters
    ----------
    catalog_path : str | Path
        مسار ملف CSV بتنسيق سلة الشامل (منتجات مهووس بتنسيق الشامل.csv).
    competitor_files : list[dict]
        قائمة بملفات المنافسين. كل عنصر قاموس بهذا الشكل::

            {
                "path":            "مسار/الملف.csv",       # إلزامي
                "name_col":        "اسم عمود المنتج",       # إلزامي
                "price_col":       "اسم عمود السعر",        # إلزامي
                "competitor_name": "اسم المنافس",           # اختياري
            }

    threshold : float
        أدنى نسبة تطابق مقبولة (0-100). الافتراضي 75.

    Returns
    -------
    dict
        مفاتيح:
        ``"cheaper"``   → DataFrame القسم الأول  (منافس أقل سعراً)
        ``"pricier"``   → DataFrame القسم الثاني (منافس أعلى أو مساوٍ)
        ``"not_found"`` → DataFrame القسم الثالث (مفقودات — فشلت المطابقة)
        ``"summary"``   → dict إحصائيات موجزة
        ``"catalog"``   → DataFrame كتالوج المتجر كاملاً (للمرجع)

    Raises
    ------
    FileNotFoundError
        إذا لم يوجد ملف الكتالوج.
    KeyError
        إذا غابت أعمدة إلزامية من ملف الكتالوج.
    ValueError
        إذا كانت قائمة competitor_files فارغة.

    Examples
    --------
    >>> report = generate_pricing_report(
    ...     catalog_path="منتجات مهووس بتنسيق الشامل.csv",
    ...     competitor_files=[
    ...         {
    ...             "path": "competitor_A.csv",
    ...             "name_col": "Product Name",
    ...             "price_col": "Price",
    ...             "competitor_name": "متجر A",
    ...         }
    ...     ],
    ...     threshold=75.0,
    ... )
    >>> cheaper  = report["cheaper"]     # منافس أقل
    >>> pricier  = report["pricier"]     # منافس أعلى
    >>> missing  = report["not_found"]   # مفقودات
    >>> summary  = report["summary"]     # إحصائيات
    """
    logger.info("═" * 65)
    logger.info("🚀 بدء تشغيل محرك التسعير والمطابقة  (عتبة=%.0f%%)", threshold)
    logger.info("═" * 65)

    if not competitor_files:
        raise ValueError("يجب توفير ملف منافس واحد على الأقل في competitor_files.")

    # ── تحميل كتالوج المتجر (يرفع استثناء إذا فشل — مقصود) ─────────────────
    catalog_df = load_base_catalog(catalog_path)

    if catalog_df.empty:
        logger.error("❌ كتالوج المتجر فارغ بعد التنظيف!")
        _empty = pd.DataFrame()
        return {
            "cheaper":   _empty,
            "pricier":   _empty,
            "not_found": _empty,
            "summary":   {"خطأ": "كتالوج المتجر فارغ"},
            "catalog":   catalog_df,
        }

    # ── معالجة كل ملف منافس بشكل آمن (أخطاء أي ملف لا توقف الباقي) ─────────
    all_match_results: List[pd.DataFrame] = []

    for entry in competitor_files:
        comp_path  = entry.get("path")
        name_col   = entry.get("name_col")
        price_col  = entry.get("price_col")
        comp_name  = entry.get("competitor_name")

        if not all([comp_path, name_col, price_col]):
            logger.warning(
                "⚠️ إدخال منافس ناقص (تم تخطيه): %s", entry
            )
            continue

        try:
            comp_df = load_competitor_file(
                file_path=comp_path,
                name_col=name_col,
                price_col=price_col,
                competitor_name=comp_name,
            )
            result_df = match_competitor_products(
                catalog_df=catalog_df,
                competitor_df=comp_df,
                threshold=threshold,
            )
            all_match_results.append(result_df)

        except FileNotFoundError as exc:
            logger.error("❌ ملف غير موجود: %s", exc)
        except KeyError as exc:
            logger.error("❌ عمود مفقود: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "❌ خطأ غير متوقع أثناء معالجة '%s': %s", comp_path, exc
            )

    if not all_match_results:
        logger.warning("⚠️ لم تنجح معالجة أي ملف منافس.")
        _empty = pd.DataFrame()
        return {
            "cheaper":   _empty,
            "pricier":   _empty,
            "not_found": _empty,
            "summary":   {"خطأ": "لم تنجح معالجة أي ملف منافس"},
            "catalog":   catalog_df,
        }

    # ── دمج نتائج جميع المنافسين في جدول موحّد ────────────────────────────
    combined_df = pd.concat(all_match_results, ignore_index=True)

    # ── التقسيم الثلاثي ───────────────────────────────────────────────────
    cheaper_df, pricier_df, not_found_df = _split_into_three_sections(combined_df)

    # ── بناء ملخص إحصائي ─────────────────────────────────────────────────
    total_matched = int((combined_df["حالة_المطابقة"] == "متطابق").sum())
    summary: Dict[str, Any] = {
        "إجمالي_منتجات_المنافسين":   len(combined_df),
        "مطابقات_ناجحة":             total_matched,
        "مفقودات":                   len(not_found_df),
        "منافس_أقل_سعراً":           len(cheaper_df),
        "منافس_أعلى_أو_مساوٍ":       len(pricier_df),
        "عتبة_المطابقة_المستخدمة":   threshold,
    }

    logger.info("─" * 65)
    logger.info("📊 ملخص النتائج:")
    for k, v in summary.items():
        logger.info("    %-38s: %s", k, v)
    logger.info("─" * 65)
    logger.info("✅ اكتمل محرك التسعير بنجاح.")

    return {
        "cheaper":   cheaper_df,
        "pricier":   pricier_df,
        "not_found": not_found_df,
        "summary":   summary,
        "catalog":   catalog_df,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 8.  دالة مساعدة للـ Streamlit — من DataFrame مرفوع مباشرة
# ══════════════════════════════════════════════════════════════════════════════

def generate_pricing_report_from_dataframes(
    catalog_df: pd.DataFrame,
    competitor_entries: List[Dict[str, Any]],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> Dict[str, Any]:
    """
    نسخة بديلة من ``generate_pricing_report`` تقبل DataFrames مباشرةً
    بدلاً من مسارات الملفات.

    مُصمَّمة للاستخدام مع Streamlit حيث يكون الملف مرفوعاً كـ
    ``UploadedFile`` ومحوَّلاً مسبقاً إلى DataFrame.

    ⚠️ للتحليل والعرض فقط — لا يُعدِّل أي أسعار ⚠️

    Parameters
    ----------
    catalog_df : pd.DataFrame
        كتالوج المتجر محمَّلاً مسبقاً. يجب أن يحتوي على الأعمدة الثلاثة.
        سيمر عبر نفس خطوات التحقق والتنظيف الداخلية.
    competitor_entries : list[dict]
        كل عنصر::

            {
                "df":              pd.DataFrame,   # DataFrame المنافس
                "name_col":        str,             # اسم عمود المنتج
                "price_col":       str,             # اسم عمود السعر
                "competitor_name": str,             # اسم المنافس
            }

    threshold : float
        أدنى نسبة تطابق (0-100).

    Returns
    -------
    dict
        نفس بنية ``generate_pricing_report()``.
    """
    logger.info("🚀 بدء محرك التسعير (من DataFrames مباشرة، عتبة=%.0f%%)", threshold)

    # ── تطبيق قواعد الكتالوج على الـ DataFrame المُمرَّر ──────────────────
    required_cols = {CATALOG_PK_COL, CATALOG_NAME_COL, CATALOG_PRICE_COL}
    missing_cols = required_cols - set(catalog_df.columns)
    if missing_cols:
        raise KeyError(
            f"❌ الأعمدة التالية غير موجودة في كتالوج المتجر: {missing_cols}"
        )

    df = catalog_df[[CATALOG_PK_COL, CATALOG_NAME_COL, CATALOG_PRICE_COL]].copy()
    df[CATALOG_PK_COL] = df[CATALOG_PK_COL].astype(str).str.strip()

    pk_series = df[CATALOG_PK_COL]
    invalid_mask = (
        pk_series.isna()
        | (pk_series == "")
        | (pk_series.str.lower() == "nan")
        | (pk_series.str.lower() == "none")
    )
    dropped = int(invalid_mask.sum())
    if dropped:
        logger.warning("⚠️ تجاهل %d صف بـ No. فارغ.", dropped)
    df = df[~invalid_mask].copy()

    df[CATALOG_NAME_COL] = (
        df[CATALOG_NAME_COL].astype(str).str.strip()
        .replace({"nan": pd.NA, "none": pd.NA})
    )
    df[CATALOG_PRICE_COL] = df[CATALOG_PRICE_COL].apply(clean_price)

    if df.empty:
        logger.error("❌ كتالوج المتجر فارغ بعد التنظيف!")
        _empty = pd.DataFrame()
        return {
            "cheaper": _empty, "pricier": _empty, "not_found": _empty,
            "summary": {"خطأ": "كتالوج فارغ"}, "catalog": df,
        }

    # ── معالجة كل منافس ────────────────────────────────────────────────────
    all_results: List[pd.DataFrame] = []

    for entry in competitor_entries:
        raw_comp_df:  pd.DataFrame = entry.get("df")
        name_col:     str          = entry.get("name_col", "")
        price_col:    str          = entry.get("price_col", "")
        comp_name:    str          = entry.get("competitor_name", "منافس")

        if raw_comp_df is None or raw_comp_df.empty:
            logger.warning("⚠️ DataFrame المنافس '%s' فارغ — تم تخطيه.", comp_name)
            continue
        if not name_col or not price_col:
            logger.warning("⚠️ أعمدة المنافس '%s' غير محددة — تم تخطيه.", comp_name)
            continue

        try:
            for col in (name_col, price_col):
                if col not in raw_comp_df.columns:
                    raise KeyError(
                        f"عمود '{col}' غير موجود في ملف {comp_name}."
                    )

            comp_df = raw_comp_df[[name_col, price_col]].copy()
            comp_df = comp_df.rename(
                columns={name_col: "اسم_منتج_المنافس", price_col: "سعر_المنافس"}
            )
            comp_df["اسم_منتج_المنافس"] = (
                comp_df["اسم_منتج_المنافس"].astype(str).str.strip()
            )
            comp_df["سعر_المنافس"] = comp_df["سعر_المنافس"].apply(clean_price)
            comp_df["المنافس"] = comp_name

            comp_df = comp_df[
                comp_df["اسم_منتج_المنافس"].notna()
                & (comp_df["اسم_منتج_المنافس"].str.strip() != "")
                & (comp_df["اسم_منتج_المنافس"].str.lower() != "nan")
            ].copy()

            result = match_competitor_products(
                catalog_df=df,
                competitor_df=comp_df,
                threshold=threshold,
            )
            all_results.append(result)

        except KeyError as exc:
            logger.error("❌ عمود مفقود للمنافس '%s': %s", comp_name, exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "❌ خطأ غير متوقع للمنافس '%s': %s", comp_name, exc
            )

    if not all_results:
        logger.warning("⚠️ لم تنجح معالجة أي منافس.")
        _empty = pd.DataFrame()
        return {
            "cheaper": _empty, "pricier": _empty, "not_found": _empty,
            "summary": {"خطأ": "لا نتائج"}, "catalog": df,
        }

    combined = pd.concat(all_results, ignore_index=True)
    cheaper_df, pricier_df, not_found_df = _split_into_three_sections(combined)

    total_matched = int((combined["حالة_المطابقة"] == "متطابق").sum())
    summary: Dict[str, Any] = {
        "إجمالي_منتجات_المنافسين":   len(combined),
        "مطابقات_ناجحة":             total_matched,
        "مفقودات":                   len(not_found_df),
        "منافس_أقل_سعراً":           len(cheaper_df),
        "منافس_أعلى_أو_مساوٍ":       len(pricier_df),
        "عتبة_المطابقة_المستخدمة":   threshold,
    }

    logger.info("✅ اكتملت المعالجة. ملخص: %s", summary)
    return {
        "cheaper":   cheaper_df,
        "pricier":   pricier_df,
        "not_found": not_found_df,
        "summary":   summary,
        "catalog":   df,
    }
