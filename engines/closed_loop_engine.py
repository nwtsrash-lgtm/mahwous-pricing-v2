"""
engines/closed_loop_engine.py  v1.0 — محرك المطابقة الدائري المغلق
═══════════════════════════════════════════════════════════════════════

قانون حفظ البيانات (الدائرة المغلقة):
    المدخل = المتطابق + المراجعة_اليدوية + المفقود
    أي انتهاك لهذه المعادلة → RuntimeError فوري.

الشلال الرباعي (Waterfall):
    الطبقة 1 — SKU/Barcode (تطابق قطعي 100%)
    الطبقة 2 — نص تام بعد التطبيع (تطابق 100%)
    الطبقة 3 — RapidFuzz + تحقق هيكلي (حجم + ماركة + نوع)
    الطبقة 4 — شبكة الأمان (لا يسقط حرف واحد)

منطق التصنيف:
    score >= 90%  AND  هيكل مطابق  →  متطابق  (MATCHED)
    score >= 90%  BUT  هيكل مختلف  →  مراجعة (REVIEW)
    65% <= score < 90%              →  مراجعة (REVIEW)
    score < 65%                     →  مفقود  (MISSING)
    لا مرشح نهائياً                →  مراجعة (REVIEW) — طبقة 4

بصمة القرار (Audit Trail):
    كل صف يحمل: Match_Score + Match_Reason + طبقة_المطابقة

التطبيع غير المدمر (Non-Destructive):
    Raw_Name       — الاسم الخام لا يُمس أبداً
    Normalized_Name — نسخة مطبّعة للمطابقة فقط

⚠️  للتحليل والعرض فقط — لا يُعدِّل أي أسعار ولا يتصل بأي API ⚠️
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from rapidfuzz import fuzz
from rapidfuzz import process as rf_process

# ─── إعداد المُسجِّل ─────────────────────────────────────────────────────────
logger = logging.getLogger("closed_loop_engine")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] closed_loop: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ══════════════════════════════════════════════════════════════════════════════
#  الثوابت
# ══════════════════════════════════════════════════════════════════════════════

CONFIRMED_SCORE: float = 90.0
"""نسبة التطابق الدنيا للمطابقة المؤكدة (مع اجتياز التحقق الهيكلي)."""

REVIEW_LOWER: float = 65.0
"""حد المنطقة الرمادية — ما بين 65% و 89% → مراجعة يدوية."""

# حالات التصنيف — مستخدمة كمفاتيح ثابتة في كل مكان
STATUS_MATCHED = "متطابق"
STATUS_REVIEW  = "مراجعة_يدوية"
STATUS_MISSING = "مفقود"

# أعمدة كتالوج سلة الشامل
CAT_PK    = "No."
CAT_NAME  = "أسم المنتج"
CAT_PRICE = "سعر المنتج"


# ══════════════════════════════════════════════════════════════════════════════
#  خريطة التطبيع العربي (Non-Destructive — تُطبَّق على نسخة مؤقتة فقط)
# ══════════════════════════════════════════════════════════════════════════════

_AR_CHAR_MAP: Dict[str, str] = {
    # توحيد الألف
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    # توحيد التاء والياء
    "ة": "ه",
    "ى": "ي",
    # إزالة التشكيل (حركات + تنوين + شدة + سكون)
    "\u064B": "", "\u064C": "", "\u064D": "",
    "\u064E": "", "\u064F": "", "\u0650": "",
    "\u0651": "", "\u0652": "",
    # إزالة المدة والهمزة المفردة
    "\u0653": "", "\u0654": "", "\u0655": "",
}
_AR_TRANS_TABLE = str.maketrans(_AR_CHAR_MAP)

# تصحيحات الماركات (مع حدود الكلمات \b) — أحرف عربية → إنجليزية موحّدة
# مرتبة من الأطول للأقصر لتجنب التعارض
_BRAND_CORRECTIONS: List[Tuple[str, str]] = [
    ("ايف سان لوران", "ysl"),
    ("ايف سان لورن",  "ysl"),
    ("جان بول غوتييه", "jean paul gaultier"),
    ("كارولينا هيريرا", "carolina herrera"),
    ("نارسيسو رودريجيز", "narciso rodriguez"),
    ("دولتشي وغابانا", "dolce gabbana"),
    ("توم فورد",    "tom ford"),
    ("أرماني",      "armani"),
    ("ارماني",      "armani"),
    ("غيرلان",      "guerlain"),
    ("ديور",        "dior"),
    ("شانيل",       "chanel"),
    ("غوتشي",       "gucci"),
    ("برادا",       "prada"),
    ("برادة",       "prada"),
    ("بربري",       "burberry"),
    ("هيرمس",       "hermes"),
    ("كارتييه",     "cartier"),
    ("فالنتينو",    "valentino"),
    ("فيرساتشي",    "versace"),
    ("غيفنشي",      "givenchy"),
    ("لانكوم",      "lancome"),
    ("إيزي مياكي",  "issey miyake"),
    ("ايزي مياكي",  "issey miyake"),
    ("داوود هوف",   "davidoff"),
    ("كالفن كلين",  "calvin klein"),
    ("سوفاج",       "sauvage"),
    ("لطافة",       "lattafa"),
    ("رصاصي",       "rasasi"),
    ("أجمل",        "ajmal"),
    ("ناهد",        "nabeel"),
    ("كريد",        "creed"),
    ("أمواج",       "amouage"),
]

# تطبيع مصطلحات التركيز/النوع (تُطبَّق بعد تحويل الكل إلى lowercase)
_TYPE_WORD_MAP: List[Tuple[str, str]] = [
    ("او دو بارفان",  "edp"),
    ("أو دو بارفان",  "edp"),
    ("او دي بارفان",  "edp"),
    ("بارفيم",        "edp"),
    ("بارفام",        "edp"),
    ("بارفان",        "edp"),
    ("eau de parfum", "edp"),
    ("او دو تواليت",  "edt"),
    ("أو دو تواليت",  "edt"),
    ("او دي تواليت",  "edt"),
    ("تواليت",        "edt"),
    ("eau de toilette","edt"),
    ("او دي كولونيا", "edc"),
    ("كولونيا",       "edc"),
    ("eau de cologne","edc"),
    ("ملي",           "ml"),
    ("مل",            "ml"),
]

# استخراج الحجم: أرقام متبوعة بـ ml/مل/oz
_VOLUME_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:ml|مل|oz|fl\.?\s*oz)",
    re.IGNORECASE | re.UNICODE,
)

# استخراج نوع التركيز
_TYPE_RE = re.compile(
    r"\b(edp|edt|edc|parfum|extrait|extract"
    r"|eau\s+de\s+parfum|eau\s+de\s+toilette|eau\s+de\s+cologne"
    r"|بارفيم|بارفام|بارفان|تواليت|كولونيا)\b",
    re.IGNORECASE | re.UNICODE,
)


# ══════════════════════════════════════════════════════════════════════════════
#  1.  التطبيع غير المدمر — normalize_text
# ══════════════════════════════════════════════════════════════════════════════

def normalize_text(raw: str) -> str:
    """
    يُنتج نسخة مطبَّعة من الاسم للمطابقة دون تعديل الاسم الأصلي.

    الخطوات بالترتيب:
    1. توحيد الأحرف العربية (أ/إ/آ → ا ، ة → ه ، ى → ي).
    2. إزالة التشكيل والحركات.
    3. تصحيح أسماء الماركات بحدود كلمات Unicode (``(?<!\\w)...(?!\\w)``).
    4. تطبيع مصطلحات التركيز (EDP/EDT ...).
    5. تحويل لأحرف صغيرة + ضغط المسافات.

    Parameters
    ----------
    raw : str
        الاسم الخام. لا يُعدَّل أبداً.

    Returns
    -------
    str
        نسخة جديدة مطبَّعة. الـ ``raw`` لا يتغير.
    """
    text: str = str(raw)

    # الخطوة 1+2: توحيد الأحرف العربية + إزالة التشكيل
    text = text.translate(_AR_TRANS_TABLE)

    # الخطوة 3: تصحيح الماركات بـ word boundaries (Unicode — آمن مع العربية)
    for wrong, correct in _BRAND_CORRECTIONS:
        text = re.sub(
            r"(?<!\w)" + re.escape(wrong) + r"(?!\w)",
            correct,
            text,
            flags=re.IGNORECASE | re.UNICODE,
        )

    # تحويل لأحرف صغيرة قبل تطبيع المصطلحات
    text = text.lower()

    # الخطوة 4: توحيد مصطلحات النوع
    for wrong, correct in _TYPE_WORD_MAP:
        text = re.sub(
            r"(?<!\w)" + re.escape(wrong.lower()) + r"(?!\w)",
            correct,
            text,
            flags=re.UNICODE,
        )

    # الخطوة 5: إزالة الرموز الخاصة غير الأبجدية الرقمية + ضغط المسافات
    text = re.sub(r"[^\w\s\u0600-\u06FF]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


# ══════════════════════════════════════════════════════════════════════════════
#  2.  دوال الاستخراج الهيكلي
# ══════════════════════════════════════════════════════════════════════════════

def _extract_volume(normalized: str) -> Optional[str]:
    """يستخرج الحجم (مثل '100' من '100ml') من الاسم المطبَّع."""
    m = _VOLUME_RE.search(normalized)
    if m:
        return m.group(1).replace(",", ".")
    return None


def _extract_concentration_type(normalized: str) -> Optional[str]:
    """يستخرج نوع التركيز (edp/edt/edc/...) من الاسم المطبَّع."""
    m = _TYPE_RE.search(normalized)
    if m:
        raw_type = m.group(1).lower().strip()
        # توحيد المرادفات
        _aliases = {
            "eau de parfum": "edp", "بارفيم": "edp",
            "بارفام": "edp", "بارفان": "edp",
            "eau de toilette": "edt", "تواليت": "edt",
            "eau de cologne": "edc", "كولونيا": "edc",
        }
        return _aliases.get(raw_type, raw_type)
    return None


def _extract_brand(normalized: str, known_brands: List[str]) -> Optional[str]:
    """
    يستخرج اسم الماركة من الاسم المطبَّع بمطابقة القائمة المعروفة.
    يُفضّل الأطول لتجنب التعارض (مثل "Tom Ford" قبل "Ford").
    """
    name_lower = normalized.lower()
    for brand in sorted(known_brands, key=len, reverse=True):
        if re.search(
            r"(?<!\w)" + re.escape(brand.lower()) + r"(?!\w)",
            name_lower,
            re.UNICODE,
        ):
            return brand.lower()
    return None


def _check_structural_constraints(
    our_norm: str,
    comp_norm: str,
    known_brands: List[str],
) -> Tuple[bool, str]:
    """
    يتحقق من تطابق الهيكل الثلاثي: حجم + ماركة + نوع تركيز.

    القاعدة: يُعاقَب فقط عند وجود القيمة في الجانبين وعدم تطابقها.
    إذا غاب أحد الجانبين → محايد (لا يُعدّ خطأً).

    Parameters
    ----------
    our_norm : str
        الاسم المطبَّع لمنتجنا في الكتالوج.
    comp_norm : str
        الاسم المطبَّع لمنتج المنافس.
    known_brands : list[str]
        قائمة الماركات المعروفة.

    Returns
    -------
    tuple[bool, str]
        (all_constraints_pass, human_readable_reason)
    """
    violations: List[str] = []

    our_vol  = _extract_volume(our_norm)
    comp_vol = _extract_volume(comp_norm)
    if our_vol and comp_vol and our_vol != comp_vol:
        violations.append(f"حجم مختلف ({our_vol}ml != {comp_vol}ml)")

    our_type  = _extract_concentration_type(our_norm)
    comp_type = _extract_concentration_type(comp_norm)
    if our_type and comp_type and our_type != comp_type:
        violations.append(f"نوع مختلف ({our_type.upper()} != {comp_type.upper()})")

    our_brand  = _extract_brand(our_norm, known_brands)
    comp_brand = _extract_brand(comp_norm, known_brands)
    if our_brand and comp_brand and our_brand != comp_brand:
        violations.append(f"ماركة مختلفة ({our_brand} != {comp_brand})")

    if violations:
        return False, " | ".join(violations)
    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
#  3.  بنّاء صف النتيجة — _build_result_row
# ══════════════════════════════════════════════════════════════════════════════

def _build_result_row(
    raw_name: str,
    normalized_name: str,
    comp_price: Optional[float],
    comp_label: str,
    *,
    status: str,
    match_score: float,
    match_reason: str,
    match_layer: int,
    our_no: Optional[str] = None,
    our_name: Optional[str] = None,
    our_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    يبني صفاً كاملاً وموحداً لجدول النتائج.

    كل صف يحمل بصمة القرار الكاملة (Match_Score + Match_Reason).
    لا يُرجع None أبداً — كل صف مكتمل.

    Parameters
    ----------
    raw_name : str
        الاسم الخام للمنتج من ملف المنافس (لا يُعدَّل).
    normalized_name : str
        الاسم المطبَّع (للعرض + التدقيق).
    comp_price : float | None
        سعر المنافس بعد التنظيف.
    comp_label : str
        اسم المنافس للعرض.
    status : str
        STATUS_MATCHED / STATUS_REVIEW / STATUS_MISSING
    match_score : float
        نسبة التطابق (0-100).
    match_reason : str
        سبب التصنيف بنص قابل للقراءة.
    match_layer : int
        رقم الطبقة التي أنتجت هذه النتيجة (1-4).
    our_no : str | None
        رقم المنتج No. في سلة (إذا وُجد مطابق أو مرشح).
    our_name : str | None
        اسم منتجنا في الكتالوج.
    our_price : float | None
        سعر منتجنا (يُستخدم للمقارنة عند MATCHED فقط).

    Returns
    -------
    dict
        صف جاهز للإضافة إلى قائمة النتائج.
    """
    price_diff: Optional[float] = None
    if (
        status == STATUS_MATCHED
        and our_price is not None
        and comp_price is not None
    ):
        price_diff = round(comp_price - our_price, 2)

    return {
        "Raw_Name":          raw_name,
        "Normalized_Name":   normalized_name,
        "سعر_المنافس":       comp_price,
        "المنافس":           comp_label,
        "No.":               our_no,
        "أسم_المنتج_لدينا":  our_name,
        "سعر_المنتج_لدينا":  our_price if status == STATUS_MATCHED else None,
        "Match_Score":       round(float(match_score), 2),
        "Match_Reason":      match_reason,
        "الحالة":            status,
        "طبقة_المطابقة":     match_layer,
        "فرق_السعر":         price_diff,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  4.  مسار الشلال الرباعي — _route_single_product
# ══════════════════════════════════════════════════════════════════════════════

def _route_single_product(
    raw_name: str,
    comp_price: Optional[float],
    comp_label: str,
    comp_sku: Optional[str],
    catalog_df: pd.DataFrame,
    catalog_norms: List[str],
    sku_index: Dict[str, int],
    exact_index: Dict[str, int],
    known_brands: List[str],
) -> Dict[str, Any]:
    """
    يُمرّر منتجاً واحداً عبر الشلال الرباعي ويُعيد صف نتيجة كاملاً.

    لا يُرجع None أبداً — كل منتج يخرج من طبقة بنتيجة محددة.

    Parameters
    ----------
    raw_name : str
        الاسم الخام من ملف المنافس.
    comp_price : float | None
        السعر المنظَّف.
    comp_label : str
        اسم المنافس.
    comp_sku : str | None
        SKU أو باركود المنافس (إن وُجد).
    catalog_df : pd.DataFrame
        كتالوج متجرنا (مطبَّع مسبقاً).
    catalog_norms : list[str]
        قائمة الأسماء المطبَّعة لكل منتج في الكتالوج (Pre-built).
    sku_index : dict[str, int]
        فهرس SKU → فهرس الصف في catalog_df.
    exact_index : dict[str, int]
        فهرس الاسم المطبَّع → فهرس الصف في catalog_df.
    known_brands : list[str]
        قائمة الماركات لفحص الهيكل.

    Returns
    -------
    dict
        صف نتيجة مكتمل يُضاف للقائمة النهائية.
    """
    comp_norm: str = normalize_text(raw_name)

    # ──────────────────────────────────────────────────────────────────────
    #  الطبقة 1: مطابقة SKU / باركود (تطابق قطعي 100%)
    # ──────────────────────────────────────────────────────────────────────
    if comp_sku and sku_index:
        sku_clean = str(comp_sku).strip()
        if sku_clean and sku_clean.lower() not in ("nan", "none", ""):
            catalog_idx = sku_index.get(sku_clean)
            if catalog_idx is not None:
                cat = catalog_df.iloc[catalog_idx]
                return _build_result_row(
                    raw_name, comp_norm, comp_price, comp_label,
                    status=STATUS_MATCHED,
                    match_score=100.0,
                    match_reason="طبقة 1: مطابقة قطعية عبر SKU/باركود (100%)",
                    match_layer=1,
                    our_no=str(cat[CAT_PK]),
                    our_name=str(cat[CAT_NAME]),
                    our_price=cat[CAT_PRICE],
                )

    # ──────────────────────────────────────────────────────────────────────
    #  الطبقة 2: مطابقة نصية تامة بعد التطبيع (تطابق 100%)
    # ──────────────────────────────────────────────────────────────────────
    if comp_norm and comp_norm in exact_index:
        catalog_idx = exact_index[comp_norm]
        cat = catalog_df.iloc[catalog_idx]
        return _build_result_row(
            raw_name, comp_norm, comp_price, comp_label,
            status=STATUS_MATCHED,
            match_score=100.0,
            match_reason="طبقة 2: مطابقة نصية تامة بعد تطبيع الأحرف",
            match_layer=2,
            our_no=str(cat[CAT_PK]),
            our_name=str(cat[CAT_NAME]),
            our_price=cat[CAT_PRICE],
        )

    # ──────────────────────────────────────────────────────────────────────
    #  الطبقة 3: RapidFuzz + تحقق هيكلي (حجم + ماركة + نوع)
    # ──────────────────────────────────────────────────────────────────────
    if catalog_norms:
        # نحصل على أفضل مطابقة بدون score_cutoff لضمان الحصول على بصمة قرار دائماً
        best_match = rf_process.extractOne(
            comp_norm,
            catalog_norms,
            scorer=fuzz.token_set_ratio,
        )

        if best_match is not None:
            _best_str, score, catalog_idx = best_match
            score = float(score)
            cat = catalog_df.iloc[catalog_idx]
            our_norm_str = catalog_norms[catalog_idx]
            our_no   = str(cat[CAT_PK])
            our_name = str(cat[CAT_NAME])
            our_price = cat[CAT_PRICE]

            if score >= CONFIRMED_SCORE:
                # نسبة عالية — نتحقق من الهيكل
                struct_ok, struct_detail = _check_structural_constraints(
                    our_norm_str, comp_norm, known_brands
                )
                if struct_ok:
                    return _build_result_row(
                        raw_name, comp_norm, comp_price, comp_label,
                        status=STATUS_MATCHED,
                        match_score=score,
                        match_reason=(
                            f"طبقة 3: تطابق فازي مؤكد {score:.1f}% "
                            f"+ هيكل مطابق (حجم+ماركة+نوع)"
                        ),
                        match_layer=3,
                        our_no=our_no,
                        our_name=our_name,
                        our_price=our_price,
                    )
                else:
                    # نسبة عالية لكن الهيكل يختلف → مراجعة يدوية
                    return _build_result_row(
                        raw_name, comp_norm, comp_price, comp_label,
                        status=STATUS_REVIEW,
                        match_score=score,
                        match_reason=(
                            f"طبقة 3: نسبة {score:.1f}% عالية لكن اختلاف هيكلي — "
                            f"{struct_detail}"
                        ),
                        match_layer=3,
                        our_no=our_no,
                        our_name=our_name,
                        our_price=None,
                    )

            elif score >= REVIEW_LOWER:
                # المنطقة الرمادية 65-89% → مراجعة يدوية (حظر الاجتهاد)
                return _build_result_row(
                    raw_name, comp_norm, comp_price, comp_label,
                    status=STATUS_REVIEW,
                    match_score=score,
                    match_reason=(
                        f"طبقة 3: المنطقة الرمادية {score:.1f}% "
                        f"(65-89%) — مراجعة يدوية إلزامية"
                    ),
                    match_layer=3,
                    our_no=our_no,
                    our_name=our_name,
                    our_price=None,
                )

            else:
                # نسبة < 65% → مفقود مع أفضل مرشح كمرجع للمراجعة
                return _build_result_row(
                    raw_name, comp_norm, comp_price, comp_label,
                    status=STATUS_MISSING,
                    match_score=score,
                    match_reason=(
                        f"طبقة 3: أعلى نسبة تطابق {score:.1f}% أقل من 65% — مفقود"
                    ),
                    match_layer=3,
                    # نحتفظ بأفضل مرشح للمرجعية (لا يُستخدم في المقارنة السعرية)
                    our_no=our_no,
                    our_name=f"[مرشح ضعيف] {our_name}",
                    our_price=None,
                )

    # ──────────────────────────────────────────────────────────────────────
    #  الطبقة 4: شبكة الأمان — لا يسقط حرف واحد
    #  تصل هنا فقط إذا كان الكتالوج فارغاً تماماً أو extractOne أعاد None
    # ──────────────────────────────────────────────────────────────────────
    return _build_result_row(
        raw_name, comp_norm, comp_price, comp_label,
        status=STATUS_REVIEW,
        match_score=0.0,
        match_reason=(
            "طبقة 4: شبكة الأمان — لم يُعثر على أي مرشح — مراجعة يدوية إلزامية"
        ),
        match_layer=4,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  5.  تنظيف الأسعار (نفس clean_price من pricing_engine — مستقل)
# ══════════════════════════════════════════════════════════════════════════════

def _clean_price(raw: Any) -> Optional[float]:
    """
    يحوّل أي تمثيل سعر إلى float آمن.
    يعالج: "150 ر.س" / "1,250.50" / NaN / None / نص فارغ → None.

    Parameters
    ----------
    raw : Any
        القيمة الخام من الخلية.

    Returns
    -------
    float | None
    """
    if raw is None:
        return None
    try:
        val = float(raw)
        return None if pd.isna(val) else val
    except (ValueError, TypeError):
        pass
    try:
        text = str(raw).strip()
        if not text or text.lower() in ("nan", "none", "-", ""):
            return None
        digits_only = re.sub(r"[^\d.,]", "", text).replace(",", "")
        return float(digits_only) if digits_only else None
    except Exception:  # noqa: BLE001
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  6.  وحدة التحقق من الدائرة المغلقة — _sanity_check
# ══════════════════════════════════════════════════════════════════════════════

def _sanity_check(
    n_input: int,
    n_matched: int,
    n_review: int,
    n_missing: int,
    competitor_name: str = "غير محدد",
) -> None:
    """
    يتحقق من قانون حفظ البيانات:
        المدخل = المتطابق + المراجعة + المفقود

    إذا كان المجموع مختلفاً → RuntimeError فوري.
    لا تسامح مطلق — صفر تسرب مقبول.

    Parameters
    ----------
    n_input : int
        عدد منتجات المنافس المُدخلة.
    n_matched : int
        عدد المتطابقات المؤكدة.
    n_review : int
        عدد المنتجات في قسم المراجعة اليدوية.
    n_missing : int
        عدد المنتجات المفقودة.
    competitor_name : str
        اسم المنافس (للتشخيص في رسالة الخطأ).

    Raises
    ------
    RuntimeError
        إذا كان المجموع لا يساوي المدخل — انتهاك قانون حفظ البيانات.
    """
    n_output = n_matched + n_review + n_missing
    if n_output != n_input:
        delta = n_input - n_output
        raise RuntimeError(
            f"\n{'═' * 60}\n"
            f"❌  انتهاك قانون حفظ البيانات (الدائرة المغلقة)!\n"
            f"    المنافس:   {competitor_name}\n"
            f"    المدخل:    {n_input:,} منتج\n"
            f"    المخرج:    {n_output:,} منتج\n"
            f"             (متطابق:{n_matched} + مراجعة:{n_review} + مفقود:{n_missing})\n"
            f"    الفجوة:    {abs(delta):,} منتج {'مفقود في العملية' if delta > 0 else 'زائد بشكل غير مبرر'}\n"
            f"{'═' * 60}"
        )
    logger.info(
        "✅ تحقق الدائرة المغلقة [%s]: %d = %d+%d+%d",
        competitor_name, n_input, n_matched, n_review, n_missing,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  7.  الفهرسة المسبقة للكتالوج — _build_catalog_indexes
# ══════════════════════════════════════════════════════════════════════════════

def _build_catalog_indexes(
    catalog_df: pd.DataFrame,
    sku_col: Optional[str] = None,
) -> Tuple[List[str], Dict[str, int], Dict[str, int]]:
    """
    يبني الفهارس المسبقة للكتالوج مرة واحدة لكل المنافسين.

    Parameters
    ----------
    catalog_df : pd.DataFrame
        كتالوج متجرنا المنظَّف.
    sku_col : str | None
        اسم عمود الـ SKU في الكتالوج (إن وُجد).

    Returns
    -------
    tuple[list[str], dict[str,int], dict[str,int]]
        (catalog_norms, sku_index, exact_index)
        - catalog_norms: قائمة الأسماء المطبَّعة (للـ Fuzzy)
        - sku_index:     SKU string → row index
        - exact_index:   normalized name → row index
    """
    logger.info("🔨 بناء فهارس الكتالوج (%d منتج) ...", len(catalog_df))
    t0 = time.perf_counter()

    catalog_norms: List[str] = [
        normalize_text(n) for n in catalog_df[CAT_NAME].fillna("")
    ]

    exact_index: Dict[str, int] = {}
    for idx, norm in enumerate(catalog_norms):
        if norm and norm not in exact_index:
            exact_index[norm] = idx

    sku_index: Dict[str, int] = {}
    if sku_col and sku_col in catalog_df.columns:
        for idx, sku_val in enumerate(catalog_df[sku_col].astype(str)):
            sku_clean = sku_val.strip()
            if sku_clean and sku_clean.lower() not in ("nan", "none", ""):
                if sku_clean not in sku_index:
                    sku_index[sku_clean] = idx

    elapsed = time.perf_counter() - t0
    logger.info(
        "✅ الفهارس جاهزة: exact=%d | sku=%d (%.2f ث)",
        len(exact_index), len(sku_index), elapsed,
    )
    return catalog_norms, sku_index, exact_index


# ══════════════════════════════════════════════════════════════════════════════
#  8.  تنظيف وتحقق من كتالوج المتجر
# ══════════════════════════════════════════════════════════════════════════════

def _prepare_catalog(
    catalog_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    يتحقق من الأعمدة الإلزامية ويُنظّف الكتالوج بدون dropna().

    القواعد:
    - صفوف No. الفارغة → تُنقل إلى جدول تحذير وتُسجَّل ثم تُحذف.
    - الأسعار تُنظَّف بـ _clean_price().
    - لا يُسمح بـ dropna() العشوائي.

    Parameters
    ----------
    catalog_df : pd.DataFrame
        الكتالوج كما رُفع.

    Returns
    -------
    pd.DataFrame
        الكتالوج المنظَّف.

    Raises
    ------
    KeyError
        إذا غابت أعمدة إلزامية.
    """
    required = {CAT_PK, CAT_NAME, CAT_PRICE}
    missing_cols = required - set(catalog_df.columns)
    if missing_cols:
        raise KeyError(
            f"❌ الأعمدة الإلزامية التالية غير موجودة: {missing_cols}\n"
            f"الأعمدة المتاحة: {list(catalog_df.columns)}"
        )

    df = catalog_df[[CAT_PK, CAT_NAME, CAT_PRICE]].copy()
    df[CAT_PK] = df[CAT_PK].astype(str).str.strip()

    # تحديد الصفوف غير الصالحة بدقة (لا dropna عشوائي)
    invalid_pk = (
        df[CAT_PK].isna()
        | (df[CAT_PK] == "")
        | (df[CAT_PK].str.lower() == "nan")
        | (df[CAT_PK].str.lower() == "none")
    )
    n_dropped = int(invalid_pk.sum())
    if n_dropped > 0:
        logger.warning(
            "⚠️ تجاهل %d صف من الكتالوج بسبب No. فارغ أو غير صالح.",
            n_dropped,
        )

    df = df[~invalid_pk].copy()
    df[CAT_NAME]  = df[CAT_NAME].astype(str).str.strip()
    df[CAT_PRICE] = df[CAT_PRICE].apply(_clean_price)

    logger.info("✅ الكتالوج جاهز: %d منتج (%d صف مُسقط).", len(df), n_dropped)
    return df.reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════════════
#  9.  التقسيم الرباعي للنتائج — _split_results
# ══════════════════════════════════════════════════════════════════════════════

def _split_results(
    results_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    يقسّم جدول النتائج الكامل إلى 4 DataFrames مستقلة.

    القسم 1 — matched_cheaper:   متطابق + سعر المنافس < سعرنا
    القسم 2 — matched_pricier:   متطابق + سعر المنافس >= سعرنا
    القسم 3 — review:            مراجعة يدوية (المنطقة الرمادية)
    القسم 4 — missing:           مفقودات (score < 65%)

    Parameters
    ----------
    results_df : pd.DataFrame
        جدول نتائج كامل من run_closed_loop_matching().

    Returns
    -------
    tuple[DataFrame, DataFrame, DataFrame, DataFrame]
    """
    matched_mask = results_df["الحالة"] == STATUS_MATCHED
    review_mask  = results_df["الحالة"] == STATUS_REVIEW
    missing_mask = results_df["الحالة"] == STATUS_MISSING

    matched_df = results_df[matched_mask].copy()
    review_df  = results_df[review_mask].copy().reset_index(drop=True)
    missing_df = results_df[missing_mask].copy().reset_index(drop=True)

    # تقسيم المتطابقين حسب السعر (Vectorized)
    both_prices = (
        matched_df["سعر_المنافس"].notna()
        & matched_df["سعر_المنتج_لدينا"].notna()
    )
    valid_matched    = matched_df[both_prices]
    no_price_matched = matched_df[~both_prices]

    cheaper_mask = valid_matched["فرق_السعر"] < 0
    matched_cheaper = valid_matched[cheaper_mask].copy().reset_index(drop=True)
    matched_pricier = pd.concat(
        [valid_matched[~cheaper_mask], no_price_matched],
        ignore_index=True,
    )

    return matched_cheaper, matched_pricier, review_df, missing_df


# ══════════════════════════════════════════════════════════════════════════════
#  10. الدالة الرئيسية — run_closed_loop_matching
# ══════════════════════════════════════════════════════════════════════════════

def run_closed_loop_matching(
    catalog_df: pd.DataFrame,
    competitor_entries: List[Dict[str, Any]],
    known_brands: Optional[List[str]] = None,
    catalog_sku_col: Optional[str] = None,
) -> Dict[str, Any]:
    """
    المحرك الرئيسي للمطابقة الدائرية المغلقة.

    ⚠️ للتحليل والعرض فقط — لا يُعدِّل أي أسعار ولا يتصل بأي API ⚠️

    Parameters
    ----------
    catalog_df : pd.DataFrame
        كتالوج متجرنا (يجب أن يحتوي على: No. / أسم المنتج / سعر المنتج).
    competitor_entries : list[dict]
        قائمة المنافسين. كل عنصر::

            {
                "df":              pd.DataFrame,   # DataFrame ملف المنافس
                "name_col":        str,             # عمود أسماء المنتجات
                "price_col":       str,             # عمود الأسعار
                "competitor_name": str,             # الاسم التعريفي
                "sku_col":         str | None,      # عمود SKU/باركود (اختياري)
            }

    known_brands : list[str] | None
        قائمة الماركات المعروفة للتحقق الهيكلي.
        إذا تُرك None يُحاول الاستيراد من config.KNOWN_BRANDS.
    catalog_sku_col : str | None
        اسم عمود SKU/باركود في كتالوج المتجر (اختياري).

    Returns
    -------
    dict
        ``"matched_cheaper"``  → DataFrame المتطابقات حيث المنافس أرخص
        ``"matched_pricier"``  → DataFrame المتطابقات حيث المنافس أغلى
        ``"review"``           → DataFrame المنطقة الرمادية (مراجعة يدوية)
        ``"missing"``          → DataFrame المفقودات (< 65%)
        ``"all_results"``      → DataFrame الكامل (لـ Audit)
        ``"summary"``          → dict إحصائيات الدائرة المغلقة
        ``"sanity_passed"``    → bool نجاح تحقق الدائرة المغلقة

    Raises
    ------
    KeyError
        إذا غابت أعمدة إلزامية من الكتالوج أو أي ملف منافس.
    RuntimeError
        إذا انتُهك قانون حفظ البيانات (تسرب أي منتج).
    ValueError
        إذا كانت قائمة competitor_entries فارغة.
    """
    logger.info("═" * 65)
    logger.info("🚀 بدء المحرك الدائري المغلق")
    logger.info("═" * 65)

    if not competitor_entries:
        raise ValueError("❌ يجب توفير ملف منافس واحد على الأقل.")

    # استيراد قائمة الماركات من config إذا لم تُعطَ
    if known_brands is None:
        try:
            from config import KNOWN_BRANDS as _kb
            known_brands = list(_kb)
        except ImportError:
            known_brands = []
            logger.warning("⚠️ لم يُعثر على config.KNOWN_BRANDS — التحقق الهيكلي بدون قائمة ماركات.")

    # ── تنظيف وفهرسة الكتالوج (مرة واحدة لكل المنافسين) ──────────────────
    catalog_clean = _prepare_catalog(catalog_df)

    if catalog_clean.empty:
        raise ValueError("❌ الكتالوج فارغ بعد التنظيف — لا يمكن الاستمرار.")

    catalog_norms, sku_index, exact_index = _build_catalog_indexes(
        catalog_clean, sku_col=catalog_sku_col
    )

    # ── معالجة كل منافس ───────────────────────────────────────────────────
    all_rows: List[Dict[str, Any]] = []
    per_competitor_stats: List[Dict[str, Any]] = []

    for entry in competitor_entries:
        raw_comp_df: Optional[pd.DataFrame] = entry.get("df")
        name_col:    str                    = entry.get("name_col", "")
        price_col:   str                    = entry.get("price_col", "")
        comp_name:   str                    = entry.get("competitor_name", "منافس")
        sku_col_comp: Optional[str]         = entry.get("sku_col")

        if raw_comp_df is None or raw_comp_df.empty:
            logger.warning("⚠️ DataFrame '%s' فارغ — تم تخطيه.", comp_name)
            continue

        if not name_col or not price_col:
            logger.warning("⚠️ أعمدة '%s' غير محددة — تم تخطيه.", comp_name)
            continue

        # تحقق من الأعمدة المطلوبة
        missing_entry_cols = {name_col, price_col} - set(raw_comp_df.columns)
        if missing_entry_cols:
            raise KeyError(
                f"❌ الأعمدة {missing_entry_cols} غير موجودة في ملف '{comp_name}'.\n"
                f"الأعمدة المتاحة: {list(raw_comp_df.columns)}"
            )

        logger.info("🔍 معالجة '%s': %d منتج", comp_name, len(raw_comp_df))
        t_start = time.perf_counter()

        comp_rows: List[Dict[str, Any]] = []

        for row_idx, comp_row in raw_comp_df.iterrows():
            raw_name_val: str          = str(comp_row[name_col]).strip()
            comp_price_val             = _clean_price(comp_row[price_col])
            comp_sku_val: Optional[str] = (
                str(comp_row[sku_col_comp]).strip()
                if sku_col_comp and sku_col_comp in comp_row.index
                else None
            )

            # كل صف يمر عبر الشلال — لا استثناء مسكوت بصمت
            result_row = _route_single_product(
                raw_name=raw_name_val,
                comp_price=comp_price_val,
                comp_label=comp_name,
                comp_sku=comp_sku_val,
                catalog_df=catalog_clean,
                catalog_norms=catalog_norms,
                sku_index=sku_index,
                exact_index=exact_index,
                known_brands=known_brands,
            )
            comp_rows.append(result_row)

        # ── التحقق من الدائرة المغلقة لكل منافس ──────────────────────────
        n_in       = len(raw_comp_df)
        n_matched  = sum(1 for r in comp_rows if r["الحالة"] == STATUS_MATCHED)
        n_review   = sum(1 for r in comp_rows if r["الحالة"] == STATUS_REVIEW)
        n_missing  = sum(1 for r in comp_rows if r["الحالة"] == STATUS_MISSING)

        _sanity_check(n_in, n_matched, n_review, n_missing, comp_name)

        elapsed = time.perf_counter() - t_start
        logger.info(
            "✅ '%s' انتهى في %.2f ث — متطابق:%d | مراجعة:%d | مفقود:%d",
            comp_name, elapsed, n_matched, n_review, n_missing,
        )

        per_competitor_stats.append({
            "المنافس":        comp_name,
            "إجمالي_المدخل": n_in,
            "متطابق":        n_matched,
            "مراجعة":        n_review,
            "مفقود":         n_missing,
        })

        all_rows.extend(comp_rows)

    if not all_rows:
        logger.warning("⚠️ لم تُنتج أي نتيجة — جميع المنافسين تم تخطيهم.")
        _empty = pd.DataFrame()
        return {
            "matched_cheaper": _empty,
            "matched_pricier": _empty,
            "review":          _empty,
            "missing":         _empty,
            "all_results":     _empty,
            "summary":         {"خطأ": "لا نتائج"},
            "sanity_passed":   False,
            "per_competitor":  per_competitor_stats,
        }

    # ── التحقق الشامل للدائرة المغلقة (كل المنافسين مجتمعين) ─────────────
    all_df     = pd.DataFrame(all_rows)
    total_in   = len(all_df)
    total_mat  = int((all_df["الحالة"] == STATUS_MATCHED).sum())
    total_rev  = int((all_df["الحالة"] == STATUS_REVIEW).sum())
    total_mis  = int((all_df["الحالة"] == STATUS_MISSING).sum())

    _sanity_check(total_in, total_mat, total_rev, total_mis, "الإجمالي الكامل")

    # ── التقسيم الرباعي ──────────────────────────────────────────────────
    cheaper_df, pricier_df, review_df, missing_df = _split_results(all_df)

    # ── بناء الملخص ──────────────────────────────────────────────────────
    summary: Dict[str, Any] = {
        "إجمالي_مدخل":        total_in,
        "متطابق_مؤكد":        total_mat,
        "مراجعة_يدوية":       total_rev,
        "مفقود":              total_mis,
        "منافس_أقل_سعراً":    len(cheaper_df),
        "منافس_أعلى_أو_مساوٍ": len(pricier_df),
        "الدائرة_المغلقة":    f"{total_in} = {total_mat}+{total_rev}+{total_mis} ✅",
    }

    logger.info("─" * 65)
    logger.info("📊 الملخص الكامل:")
    for k, v in summary.items():
        logger.info("    %-36s: %s", k, v)
    logger.info("─" * 65)
    logger.info("✅ المحرك الدائري المغلق اكتمل بنجاح.")

    return {
        "matched_cheaper": cheaper_df,
        "matched_pricier": pricier_df,
        "review":          review_df,
        "missing":         missing_df,
        "all_results":     all_df,
        "summary":         summary,
        "sanity_passed":   True,
        "per_competitor":  per_competitor_stats,
    }
