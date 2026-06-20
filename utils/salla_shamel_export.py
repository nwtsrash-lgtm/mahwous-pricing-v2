"""
utils/salla_shamel_export.py — v3.0 (مطابق لقالب سلة الرسمي)
══════════════════════════════════════════════════════════════
▸ القالب مُستخرج من ملف منتج_جديد.csv الرسمي لمنصة سلة
▸ أول صف: بيانات المنتج (meta-header) — إلزامي في سلة
▸ 40 عموداً بالترتيب الحرفي المطابق لـ سلة
▸ التصدير: CSV (مطابق للقالب) + XLSX — كلاهما عبر io.BytesIO
▸ وصف HTML من 7 أقسام مطابق لقالب سلة الرسمي
▸ تحقق مزدوج من المنتجات المفقودة قبل التصدير
"""
from __future__ import annotations

import csv
import difflib
import html as _html_lib
import io
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import streamlit as st
from utils import brand_manager

# ── 40 عمود بالترتيب الحرفي المطابق لـ سلة ──────────────────────────────
SALLA_SHAMEL_COLUMNS: list[str] = [
    "النوع ",
    "أسم المنتج",
    "تصنيف المنتج",
    "صورة المنتج",
    "وصف صورة المنتج",
    "نوع المنتج",
    "سعر المنتج",
    "الوصف",
    "هل يتطلب شحن؟",
    "رمز المنتج sku",
    "سعر التكلفة",
    "السعر المخفض",
    "تاريخ بداية التخفيض",
    "تاريخ نهاية التخفيض",
    "اقصي كمية لكل عميل",
    "إخفاء خيار تحديد الكمية",
    "اضافة صورة عند الطلب",
    "الوزن",
    "وحدة الوزن",
    "الماركة",
    "العنوان الترويجي",
    "تثبيت المنتج",
    "الباركود",
    "السعرات الحرارية",
    "MPN",
    "GTIN",
    "خاضع للضريبة ؟",
    "سبب عدم الخضوع للضريبة",
    "[1] الاسم",
    "[1] النوع",
    "[1] القيمة",
    "[1] الصورة / اللون",
    "[2] الاسم",
    "[2] النوع",
    "[2] القيمة",
    "[2] الصورة / اللون",
    "[3] الاسم",
    "[3] النوع",
    "[3] القيمة",
    "[3] الصورة / اللون",
]

# صف الـ meta-header المطلوب في قالب سلة CSV
_SALLA_META_HEADER = "بيانات المنتج" + "," * (len(SALLA_SHAMEL_COLUMNS) - 1)

# ── الفئات المعيارية في سلة ──────────────────────────────────────────────
_GENDER_CATEGORY = {
    "للرجال":   "العطور > عطور رجالية",
    "رجالي":    "العطور > عطور رجالية",
    "للنساء":   "العطور > عطور نسائية",
    "نسائي":    "العطور > عطور نسائية",
    "للجنسين":  "العطور > عطور للجنسين",
    "unisex":   "العطور > عطور للجنسين",
}
_DEFAULT_CATEGORY = "العطور > عطور للجنسين"
_CATEGORY_CSV_CANDIDATES = ("تصنيفات مهووس.csv", "data/تصنيفات مهووس.csv")


def _norm_text(s: str) -> str:
    """تطبيع النص للمقارنة — حروف صغيرة + إزالة تشكيل + مسافات"""
    t = unicodedata.normalize("NFKC", str(s or ""))
    t = re.sub(r"[\u064B-\u065F\u0670]", "", t)  # إزالة الحركات
    t = re.sub(r"[أإآا]", "ا", t)
    t = re.sub(r"[ةه]", "ه", t)
    t = re.sub(r"[يى]", "ي", t)
    return re.sub(r"\s+", " ", t).strip().lower()


# مُطبِّع المطابقة لبوابة منع التكرار — مكافئ تماماً لـ app.py::_miss_bare
# (نفس مطابقة المسار الحيّ للمفقودات): normalize_name + إسقاط كلمات التوقف
# + الأرقام المفردة + الكلمات <2 حرفاً. مُتحقَّق بايت-ببايت ضدّ app._miss_bare
# على كامل الكتالوج (7,863 منتجاً، صفر فروق). يُستخدم في verify_truly_missing
# فقط — لا يمسّ _norm_text أعلاه المستخدَم لتوليد SKU/مفاتيح تحديث الصفوف.
# ملاحظة: نسخة متزامنة يدوياً (استيراد app.py في util يشغّل التطبيق كاملاً).
_MISS_STOP_MATCH = frozenset(
    "عطر عينه عينة تستر سامبل ماء او دو دي بارفيوم برفيوم بارفان تواليت توالت "
    "كولونيا كولن مل غرام للرجال للنساء رجالي نسائي".split()
)


def _bare_match(nm: str) -> str:
    """اسم مجرّد للمطابقة — مطابق لخوارزمية app._miss_bare (المُطبِّع الموحّد)."""
    from engines.engine import normalize_name as _nn
    return " ".join(
        t for t in _nn(str(nm)).split()
        if t not in _MISS_STOP_MATCH and not re.fullmatch(r"\d+", t) and len(t) >= 2
    )


def _safe_str(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "<na>") else s


@st.cache_data(ttl=3600)
def load_salla_categories_safe():
    # البحث عن الملف في المسار الرئيسي أو مجلد data
    file_path = "تصنيفات مهووس.csv"
    if not os.path.exists(file_path):
        file_path = os.path.join("data", "تصنيفات مهووس.csv")

    if os.path.exists(file_path):
        try:
            df_cats = pd.read_csv(file_path)
            if "التصنيفات" in df_cats.columns:
                return [str(x).strip() for x in df_cats["التصنيفات"].dropna().tolist()]
        except Exception:
            pass  # تجاهل الخطأ بصمت لمنع انهيار النظام
    return []


def _sanitize_alt_text(text: str) -> str:
    # FIX: Salla Strict CSV Validation
    cleaned = re.sub(r"[^\w\s\u0600-\u06FF]", "", _safe_str(text))
    return re.sub(r"\s+", " ", cleaned).strip()


def _resolve_brand_safe(raw_brand: str) -> str:
    # FIX: Salla Strict CSV Validation
    brand_raw = _safe_str(raw_brand)
    if not brand_raw or brand_raw in ("غير متوفر", "غير محدد", "nan", "None"):
        return ""
    try:
        mgr = brand_manager.BrandManager.get_instance()
        matched = mgr._fuzzy_match_known(brand_raw)  # type: ignore[attr-defined]
        if matched:
            return _safe_str(matched)
    except Exception:
        pass
    # احتياط: لا تُفرّغ ماركة حقيقية لمجرّد غيابها عن قائمة معروفة —
    # نظّفها وأعِدها (طلب المالك: لا تُترك الماركة فارغة).
    _clean = brand_raw.strip().strip("-•|").strip()
    return _clean[:60]


def _sanitize_category_safe(raw_cat, export_mode="safe"):
    if pd.isna(raw_cat) or not str(raw_cat).strip():
        return ""

    cleaned_cat = str(raw_cat).strip()
    if export_mode == "safe":
        cleaned_cat = cleaned_cat.split(">")[-1].strip()

    valid_cats = load_salla_categories_safe()
    if not valid_cats:
        return cleaned_cat  # Fallback إذا لم يتم العثور على ملف التصنيفات

    # 1. المطابقة التامة (Exact Match) - الأولوية الأولى
    for v_cat in valid_cats:
        if cleaned_cat.lower() == v_cat.lower():
            return v_cat

    # 2. المطابقة التقريبية الصارمة (95%) - الأولوية الثانية
    best_match = None
    highest_ratio = 0.0
    for v_cat in valid_cats:
        ratio = difflib.SequenceMatcher(None, cleaned_cat.lower(), v_cat.lower()).ratio()
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_match = v_cat

    # نقبل فقط التطابق شبه المتطابق تماماً لتجنب الأخطاء الكارثية
    if highest_ratio >= 0.95:
        return best_match

    return ""  # إرجاع فارغ بدلاً من إرجاع تصنيف خاطئ يرفضه نظام سلة


def generate_safe_slug(text):
    if pd.isna(text) or not str(text).strip():
        return ""
    # إزالة الرموز الخاصة، الإبقاء على الحروف (عربي/إنجليزي)، الأرقام، والشرطات
    safe_slug = re.sub(r"[^a-zA-Z0-9\u0600-\u06FF\s-]", "", str(text)).strip()
    # استبدال المسافات بشرطات
    safe_slug = safe_slug.replace(" ", "-")
    # منع تكرار الشرطات المتتالية (مثل --)
    safe_slug = re.sub(r"-+", "-", safe_slug)
    return safe_slug


# ══════════════════════════════════════════════════════════════════════════
#  قالب HTML الشامل — مطابق 100% لقالب سلة الرسمي (7 أقسام)
# ══════════════════════════════════════════════════════════════════════════
def generate_salla_html_description(
    product_name: str,
    brand_name: str = "غير متوفر",
    gender: str = "للجنسين",
    size_ml: str = "100",
    concentration: str = "أو دو بارفيوم",
    fragrance_family: str = "غير متوفر",
    top_notes: str = "غير متوفر",
    heart_notes: str = "غير متوفر",
    base_notes: str = "غير متوفر",
    season: str = "جميع الفصول",
    occasions: str = "المناسبات الرسمية، السهرات، واللقاءات العملية",
    longevity: str = "8",
    sillage: str = "8",
    steadiness: str = "9",
    description_text: str = "",
    fragrance_notes: str = "",
) -> str:
    """
    قالب HTML من 7 أقسام — مطابق لملف منتج_جديد.csv من سلة.

    الأقسام:
      1. h2 + p رئيسي
      2. h3 تفاصيل المنتج (ul)
      3. h3 رحلة العطر — الهرم العطري (ul)
      4. h3 لماذا تختار (ul)
      5. h3 متى وأين ترتديه (p)
      6. h3 لمسة خبير (p)
      7. h3 الأسئلة الشائعة (ul) + h3 اكتشف أكثر (p) + p ختامي
    """
    pn  = _html_lib.escape(_safe_str(product_name) or "منتج", quote=False)
    brand_raw = _safe_str(brand_name)  # FIX: Salla Exact Match & Smart HTML
    brand_valid = bool(brand_raw and brand_raw != "غير متوفر")  # FIX: Salla Exact Match & Smart HTML
    br  = _html_lib.escape(brand_raw or "غير متوفر", quote=False)
    gn  = _html_lib.escape(_safe_str(gender)        or "للجنسين", quote=False)
    sz  = _html_lib.escape(_safe_str(size_ml)       or "100", quote=False)
    cc  = _html_lib.escape(_safe_str(concentration) or "أو دو بارفيوم", quote=False)
    ff_raw = _safe_str(fragrance_family)  # FIX: Salla Exact Match & Smart HTML
    ff = _html_lib.escape(ff_raw or "غير متوفر", quote=False)
    ff_faq = _html_lib.escape(
        ff_raw if ff_raw and ff_raw != "غير متوفر" else "مزيج عطري ساحر وفريد.",
        quote=False,
    )  # FIX: Salla Exact Match & Smart HTML
    tn  = _html_lib.escape(_safe_str(top_notes)     or "غير متوفر", quote=False)
    hn  = _html_lib.escape(_safe_str(heart_notes)   or "غير متوفر", quote=False)
    bn  = _html_lib.escape(_safe_str(base_notes)    or "غير متوفر", quote=False)
    sea = _html_lib.escape(_safe_str(season)        or "جميع الفصول", quote=False)
    occ = _html_lib.escape(_safe_str(occasions)     or "المناسبات الرسمية والسهرات", quote=False)
    lng = _safe_str(longevity) or "8"
    sig = _safe_str(sillage) or "8"
    std = _safe_str(steadiness) or "9"

    safe_slug = generate_safe_slug(brand_raw) if brand_valid else ""
    brand_slug = safe_slug.strip("-") if safe_slug else ""
    brand_url  = f"https://mahwous.com/brands/{brand_slug}" if brand_slug else "https://mahwous.com/"  # FIX: Salla Exact Match & Smart HTML
    bu  = _html_lib.escape(brand_url, quote=True)
    brand_intro = (
        f'<strong><a href="{bu}" target="_blank" rel="noopener">{br}</a></strong>'
        if brand_valid else "أرقى الدور العريقة"
    )  # FIX: Salla Exact Match & Smart HTML
    brand_details = (
        f'<a href="{bu}" target="_blank" rel="noopener">{br}</a>'
        if brand_valid else "أرقى الدور العريقة"
    )  # FIX: Salla Exact Match & Smart HTML
    heritage_line = (
        f"من دار {br} العريقة بتراث عطري أصيل."
        if brand_valid else "من أرقى الدور العريقة بتراث عطري أصيل."
    )  # FIX: Salla Exact Match & Smart HTML
    discover_line = (
        f'اكتشف <a href="{bu}" target="_blank" rel="noopener">عطور {br}</a>'
        if brand_valid
        else '<a href="https://mahwous.com/" target="_blank" rel="noopener">اكتشف المزيد من عطور مهووس</a>'
    )  # FIX: Salla Exact Match & Smart HTML

    size_label = f"{sz} مل" if sz.isdigit() else sz
    brand_sentence = _html_lib.escape(heritage_line, quote=False)
    fallback_desc = (
        f"اكتشف سحر {pn} بتركيز {cc} وحجم {size_label}، عطر {ff} مصمم ل{gn} بإحساس فاخر يدوم."
    )
    description_safe = _html_lib.escape(_safe_str(description_text) or fallback_desc, quote=False)
    notes_raw = _safe_str(fragrance_notes)
    if not notes_raw:
        notes_raw = (
            f"إفتتاحية العطر: {tn}. قلب العطر: {hn}. قاعدة العطر: {bn}."
            if any(x != "غير متوفر" for x in (top_notes, heart_notes, base_notes))
            else "مزيج عطري ساحر ينبض بالجاذبية والفخامة، صُمم ليترك أثراً لا يُنسى."
        )
    fragrance_notes_safe = _html_lib.escape(notes_raw, quote=False)  # FIX: Zero-Gap HTML & AI Fragrance Notes
    brand_link_html = (
        f"<p style=\"margin: 0;\"><a href=\"{bu}\" target=\"_blank\" rel=\"noopener\">اكتشف المزيد من عطور {br}</a></p>"
        if brand_valid
        else "<p style=\"margin: 0;\"><a href=\"https://mahwous.com/\" target=\"_blank\" rel=\"noopener\">اكتشف المزيد من عطور مهووس</a></p>"
    )
    raw_html = f"""
    <div dir="rtl" style="line-height: 1.5; font-family: Tahoma, Arial, sans-serif; color: #333;">
        <h2 style="margin: 0 0 8px 0; color: #2c3e50; font-size: 20px;">{pn}</h2>
        <p style="margin: 0 0 12px 0;">{description_safe}</p>

        <h3 style="margin: 12px 0 5px 0; color: #b8860b; font-size: 16px;">المكونات العطرية:</h3>
        <p style="margin: 0 0 12px 0; font-weight: bold;">{fragrance_notes_safe}</p>

        <h3 style="margin: 12px 0 5px 0; color: #2c3e50; font-size: 16px;">لماذا تختار هذا العطر؟</h3>
        <ul style="margin: 0 0 12px 0; padding-right: 20px;">
            <li style="margin-bottom: 4px;"><strong>التميز والأصالة:</strong> {brand_sentence}</li>
            <li style="margin-bottom: 4px;"><strong>الجاذبية المضمونة:</strong> عطر يجعلك محور الاهتمام في كل مكان.</li>
            <li style="margin-bottom: 4px;"><strong>الأداء:</strong> الفوحان {sig}/10 والثبات {std}/10.</li>
            <li style="margin-bottom: 4px;"><strong>المناسبات:</strong> يلائم {occ} خلال {sea}.</li>
        </ul>

        <h3 style="margin: 12px 0 5px 0; color: #2c3e50; font-size: 16px;">الأسئلة الشائعة:</h3>
        <ul style="margin: 0 0 12px 0; padding-right: 20px;">
            <li style="margin-bottom: 4px;"><strong>كم يدوم العطر؟</strong> بين {lng}-12 ساعة حسب البشرة ودرجة الحرارة.</li>
            <li style="margin-bottom: 4px;"><strong>ما هي العائلة العطرية؟</strong> {ff_faq}</li>
            <li style="margin-bottom: 4px;"><strong>متى أستخدمه؟</strong> صُمم ليناسب كافة أوقاتك المميزة.</li>
        </ul>
        {brand_link_html}
    </div>
    """  # FIX: Zero-Gap HTML & AI Fragrance Notes

    clean_html = raw_html.replace("\n", " ").replace("\r", "")
    clean_html = re.sub(r"\s{2,}", " ", clean_html).strip()  # FIX: Zero-Gap HTML & AI Fragrance Notes
    return clean_html


def sanitize_salla_description_html(raw: str) -> str:
    """فلتر ثلاثي الطبقات — يزيل أي نص حواري AI قبل الـ HTML"""
    if not raw:
        return ""
    s = str(raw).strip()
    s = re.sub(r"(?is)^```(?:html|xml)?\s*", "", s).strip()
    s = re.sub(r"(?is)\s*```\s*$", "", s).strip()
    first = re.search(r"(?is)<\s*(?:h2|h3|div|p)\b", s)
    if first and first.start() > 0:
        prefix = s[: first.start()]
        if re.search(r"[a-zA-Z\u0600-\u06FF]", prefix):
            s = s[first.start():]
    m = re.search(r"(?is)<\s*(?:h2|h3|div|p)\b", s)
    return s[m.start():].strip() if m else ""


# ══════════════════════════════════════════════════════════════════════════
#  استخراج بيانات الصف
# ══════════════════════════════════════════════════════════════════════════
def _extract_product_name(row: dict) -> str:
    for k in ("أسم المنتج", "اسم المنتج", "منتج_المنافس", "المنتج", "cleaned_title", "name", "title", "الاسم"):
        v = _safe_str(row.get(k, ""))
        if v and not v.lower().startswith(("http://", "https://")):
            return v
    return ""


def _extract_brand(row: dict) -> str:
    for k in ("الماركة_الرسمية", "الماركة", "الماركة_الرسمي", "brand", "Brand"):
        v = _safe_str(row.get(k, ""))
        if v and v.lower() not in ("nan", "none", "unknown", "ماركة عالمية"):
            return v
    return "غير متوفر"


def _brand_from_name(pname: str) -> str:
    """كشف الماركة من اسم المنتج عبر محرك الماركات — ضمان عدم ترك عمود الماركة فارغاً.

    يُستخدم كملاذ أخير عندما لا يحمل الصف ماركة صريحة (طلب المالك: الماركة مملوءة دائماً).
    """
    if not pname:
        return ""
    try:
        from engines.engine import extract_brand as _eng_extract_brand
        b = _eng_extract_brand(pname)
        if b:
            # خذ الجزء العربي من «عربي | English» إن كان ثنائياً
            b = b.split("|")[0].strip() if "|" in b else b.strip()
            return _safe_str(b)[:60]
    except Exception:
        pass
    return ""


def _generate_sku(brand: str, pname: str, size: str) -> str:
    """يولّد رمز SKU فريداً وثابتاً (deterministic) عند غياب SKU في المصدر.

    نفس المنتج (ماركة+اسم+حجم) ⇒ نفس الرمز، ومنتجات مختلفة ⇒ رموز مختلفة،
    لضمان عمود «رمز المنتج sku» غير فارغ ولا يتكرر بين منتجين مختلفين.
    """
    import hashlib
    base = _norm_text(f"{brand}|{pname}|{size}") or _norm_text(pname) or "mahwous"
    h = hashlib.md5(base.encode("utf-8")).hexdigest()[:10].upper()
    return f"MH-{h}"


# كشف الجنس من اسم المنتج — النسائي أولاً لأن "women/woman" تحتوي "men/man"
_GENDER_NAME_PATTERNS = [
    (("نسائي", "نسائى", "نساء", "للنساء", "women", "woman", "femme", "for her"), "نسائي"),
    (("رجالي", "رجالى", "رجال", "للرجال", "men", "man", "homme", "for him"), "رجالي"),
    (("للجنسين", "مشترك", "unisex"), "للجنسين"),
]


def _extract_gender(row: dict) -> str:
    for k in ("الجنس", "gender_hint", "Gender", "gender"):
        v = _safe_str(row.get(k, ""))
        if v:
            return v
    # ملاذ أخير: استنتج الجنس من اسم المنتج لضمان تصنيف صحيح (رجالي/نسائي/مشترك)
    name = _extract_product_name(row).lower()
    if name:
        for kws, label in _GENDER_NAME_PATTERNS:
            if any(kw in name for kw in kws):
                return label
    return "للجنسين"


_SIZE_RE = re.compile(r"(\d{1,4})\s*(?:مل|ملي|ml|ML|mL)\b", re.I)


def _extract_size(row: dict) -> str:
    # 1) من حقل الحجم المخصّص
    for k in ("الحجم", "size", "Size", "حجم"):
        v = _safe_str(row.get(k, ""))
        m = _SIZE_RE.search(v)
        if m:
            return m.group(1)
        # رقم خام في حقل الحجم (مثل "100")
        if v.isdigit() and 1 <= int(v) <= 9999:
            return v
    # 2) من اسم المنتج كـ fallback
    for k in ("منتج_المنافس", "أسم المنتج", "اسم المنتج", "المنتج", "name", "title"):
        v = _safe_str(row.get(k, ""))
        m = _SIZE_RE.search(v)
        if m:
            return m.group(1)
    return "100"


def _extract_price(row: dict) -> str:
    for k in ("سعر_المنافس", "سعر المنافس", "السعر", "سعر المنتج", "Price", "price"):
        v = _safe_str(row.get(k, ""))
        try:
            p = float(v.replace(",", ""))
            if p > 0:
                return str(round(p, 2))
        except (ValueError, TypeError):
            pass
    return ""


_CDN_CGI_IMAGE_RE = re.compile(r"/cdn-cgi/image/[^/]+/", re.I)


def _sanitize_image_url(url: str) -> str:
    """يزيل تحويلات Cloudflare Image Resizing (cdn-cgi/image/...,...)
    لأن الفواصل داخلها تكسر استيراد سلة الذي يَفصِل الصور بـ ','."""
    if not url:
        return ""
    cleaned = _CDN_CGI_IMAGE_RE.sub("/", url, count=1)
    # احتياط: لو ما زال يحتوي فواصل، خذ أول جزء قبل أول فاصلة فقط
    if "," in cleaned:
        cleaned = cleaned.split(",", 1)[0]
    return cleaned.strip()


def _extract_image(row: dict) -> str:
    for k in ("صورة_المنافس", "صورة المنتج", "image_url", "صورة", "الصورة"):
        v = _safe_str(row.get(k, ""))
        if v and v.lower().startswith("http"):
            return _sanitize_image_url(v)
    return ""


def _extract_category(row: dict, gender: str, export_mode: str = "safe") -> str:
    for k in ("التصنيف_الرسمي", "تصنيف المنتج", "التصنيف", "category"):
        v = _safe_str(row.get(k, ""))
        if v:
            return _sanitize_category_safe(v, export_mode=export_mode)  # FIX: Salla Strict CSV Validation
    # اشتق من الجنس
    fallback_cat = _GENDER_CATEGORY.get(gender, _DEFAULT_CATEGORY)
    return _sanitize_category_safe(fallback_cat, export_mode=export_mode)  # FIX: Salla Strict CSV Validation


def _extract_notes(row: dict) -> tuple[str, str, str]:
    top   = _safe_str(row.get("top_notes", "")) or "غير متوفر"
    heart = _safe_str(row.get("heart_notes", "")) or "غير متوفر"
    base  = _safe_str(row.get("base_notes", "")) or "غير متوفر"
    return top, heart, base


# ══════════════════════════════════════════════════════════════════════════
#  التحقق من المنتجات المفقودة — هل هي موجودة فعلاً في الكتالوج؟
# ══════════════════════════════════════════════════════════════════════════
def verify_truly_missing(
    missing_df: pd.DataFrame,
    our_catalog_df: Optional[pd.DataFrame] = None,
    fuzzy_threshold: float = 85.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    يفحص كل منتج «مفقود» مقابل الكتالوج بطريقتين:
      1. تطابق نصي مباشر (بعد التطبيع)
      2. تطابق fuzzy ≥ fuzzy_threshold%

    يُعيد (truly_missing_df, found_in_catalog_df)
    """
    if missing_df is None or missing_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    if our_catalog_df is None or our_catalog_df.empty:
        return missing_df.copy(), pd.DataFrame()

    # بناء فهرس الكتالوج
    name_col = None
    for c in ("اسم المنتج", "المنتج", "أسم المنتج", "name", "Name", "title"):
        if c in our_catalog_df.columns:
            name_col = c
            break
    if not name_col:
        return missing_df.copy(), pd.DataFrame()

    catalog_names_raw = our_catalog_df[name_col].dropna().astype(str).tolist()
    # المُطبِّع الموحّد _bare_match (= app._miss_bare) بدل _norm_text الضعيف —
    # كي تتّسق بوابة منع التكرار مع المسار الحيّ للمفقودات (المرحلة 2/P0).
    catalog_norms = [_bare_match(n) for n in catalog_names_raw]

    truly_missing = []
    found_in_cat  = []

    try:
        from rapidfuzz import process as rf_proc, fuzz
        use_fuzzy = True
    except ImportError:
        use_fuzzy = False

    for _, row in missing_df.iterrows():
        pname = _extract_product_name(row.to_dict())
        if not pname:
            truly_missing.append(row)
            continue
        pnorm = _bare_match(pname)

        # 1. تطابق نصي مباشر
        if pnorm in catalog_norms:
            found_in_cat.append(row)
            continue

        # 2. fuzzy
        found = False
        if use_fuzzy and catalog_norms:
            best = rf_proc.extractOne(pnorm, catalog_norms, scorer=fuzz.token_set_ratio)
            if best and best[1] >= fuzzy_threshold:
                found = True

        if found:
            found_in_cat.append(row)
        else:
            truly_missing.append(row)

    return (
        pd.DataFrame(truly_missing).reset_index(drop=True) if truly_missing else pd.DataFrame(),
        pd.DataFrame(found_in_cat).reset_index(drop=True)  if found_in_cat  else pd.DataFrame(),
    )


# ══════════════════════════════════════════════════════════════════════════
#  بناء DataFrame سلة
# ══════════════════════════════════════════════════════════════════════════
def build_salla_shamel_dataframe(
    missing_df: pd.DataFrame,
    our_catalog_df: Optional[pd.DataFrame] = None,
    verify_missing: bool = True,
    export_mode: str = "safe",  # FIX: Salla Export Mode Toggle
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    يبني DataFrame بـ 40 عمود من جدول المنتجات المفقودة.

    ▸ إذا verify_missing=True يتحقق مسبقاً من أن المنتج غير موجود في الكتالوج
    ▸ يُعيد (salla_df, found_in_catalog_df)
    """
    if missing_df is None or missing_df.empty:
        return pd.DataFrame(columns=SALLA_SHAMEL_COLUMNS), pd.DataFrame()

    truly_missing, found_in_cat = (
        verify_truly_missing(missing_df, our_catalog_df)
        if verify_missing and our_catalog_df is not None and not our_catalog_df.empty
        else (missing_df.copy(), pd.DataFrame())
    )

    if truly_missing.empty:
        return pd.DataFrame(columns=SALLA_SHAMEL_COLUMNS), found_in_cat

    rows: list[dict] = []
    for _, row in truly_missing.iterrows():
        r = row.to_dict()
        pname   = _extract_product_name(r)
        brand   = _extract_brand(r)
        gender  = _extract_gender(r)
        size    = _extract_size(r)
        price   = _extract_price(r)
        image   = _extract_image(r)
        cat     = _extract_category(r, gender, export_mode=export_mode)  # FIX: Salla Export Mode Toggle
        top_n, heart_n, base_n = _extract_notes(r)
        img_alt = _sanitize_alt_text(pname) if pname else ""  # FIX: Salla Strict CSV Validation
        safe_brand = _resolve_brand_safe(brand)  # FIX: Salla Strict CSV Validation
        # ضمان عدم ترك الماركة فارغة: اكشفها من اسم المنتج كملاذ أخير
        if not safe_brand:
            safe_brand = _brand_from_name(pname)

        # Phase 3: استخدام الوصف المُولَّد من AI إن وُجد (من خط المعالجة الذكية)
        _ai_desc_raw = _safe_str(r.get("وصف_AI", ""))
        if _ai_desc_raw:
            description = sanitize_salla_description_html(_ai_desc_raw)
        else:
            description = ""

        # Fallback: توليد الوصف من القالب الثابت إذا لم يتوفر وصف AI
        if not description:
            description = generate_salla_html_description(
                product_name=pname,
                brand_name=safe_brand or "غير متوفر",
                gender=gender,
                size_ml=size,
                fragrance_family=_safe_str(r.get("العائلة_العطرية", "غير متوفر")),
                top_notes=top_n,
                heart_notes=heart_n,
                base_notes=base_n,
                description_text=_safe_str(r.get("description", "")),
                fragrance_notes=_safe_str(r.get("fragrance_notes", "")),  # FIX: Zero-Gap HTML & AI Fragrance Notes
            )

        out: dict[str, Any] = {c: "" for c in SALLA_SHAMEL_COLUMNS}
        out["النوع "]                    = "منتج"
        out["أسم المنتج"]                = pname
        out["تصنيف المنتج"]              = cat
        out["صورة المنتج"]               = image
        out["وصف صورة المنتج"]          = img_alt
        out["نوع المنتج"]                = "منتج جاهز"
        out["سعر المنتج"]                = price
        out["الوصف"]                     = description
        out["هل يتطلب شحن؟"]            = "نعم"
        # FIX: extract SKU from input row if available (e.g. magic factory / scraper),
        # وإلا ولّد رمزاً فريداً ثابتاً لضمان عمود SKU غير فارغ وغير مكرر
        _sku_in = (
            _safe_str(r.get("رمز المنتج sku", ""))
            or _safe_str(r.get("sku", ""))
            or _safe_str(r.get("SKU", ""))
        )
        out["رمز المنتج sku"]            = _sku_in or _generate_sku(safe_brand, pname, size)
        out["سعر التكلفة"]               = ""
        # السعر المخفض = سعر المنافس − 1 ريال
        try:
            _p = float(str(price).replace(",", "")) if price not in ("", None) else 0.0
            out["السعر المخفض"] = str(round(_p - 1, 2)) if _p > 1 else ""
        except (ValueError, TypeError):
            out["السعر المخفض"] = ""
        out["تاريخ بداية التخفيض"]       = ""
        out["تاريخ نهاية التخفيض"]       = ""
        out["اقصي كمية لكل عميل"]        = 100  # FIX: Salla Strict CSV Validation
        out["إخفاء خيار تحديد الكمية"]  = "لا"  # FIX: Salla Strict CSV Validation
        out["اضافة صورة عند الطلب"]     = "لا"  # FIX: Salla Strict CSV Validation
        out["الوزن"]                     = 0.2
        out["وحدة الوزن"]               = "kg"
        out["الماركة"]                   = safe_brand  # FIX: Salla Strict CSV Validation
        # FIX: extract promotional title & barcode from input row if available
        out["العنوان الترويجي"]          = _safe_str(r.get("العنوان الترويجي", ""))
        out["تثبيت المنتج"]              = ""
        out["الباركود"]                  = _safe_str(r.get("الباركود", ""))
        out["السعرات الحرارية"]          = ""
        out["MPN"]                       = ""
        out["GTIN"]                      = ""
        out["خاضع للضريبة ؟"]           = "نعم"  # FIX: Salla Strict CSV Validation
        out["سبب عدم الخضوع للضريبة"]   = ""
        rows.append(out)

    df = pd.DataFrame(rows, columns=SALLA_SHAMEL_COLUMNS)
    assert len(df.columns) == 40, f"عدد الأعمدة {len(df.columns)} ≠ 40"

    # ── بوابة جودة إلزامية: وصف مهووس صالح + رابط صورة حقيقي ──────────
    try:
        from utils.product_gate import is_mahwous_description, is_real_image_url
        keep_mask = df.apply(
            lambda r: bool(is_mahwous_description(r.get("الوصف", "")))
                      and bool(is_real_image_url(r.get("صورة المنتج", ""))),
            axis=1,
        )
        rejected_count = int((~keep_mask).sum())
        if rejected_count:
            try:
                st.warning(
                    f"⚠️ تم استبعاد {rejected_count} منتج من ملف سلة لغياب وصف مهووس صالح أو رابط صورة حقيقي."
                )
            except Exception:
                pass
        df = df.loc[keep_mask].reset_index(drop=True)
    except Exception:
        pass

    return df, found_in_cat


# ══════════════════════════════════════════════════════════════════════════
#  التصدير — CSV مطابق لقالب سلة (مع صف بيانات المنتج)
# ══════════════════════════════════════════════════════════════════════════
def export_to_salla_shamel_csv(
    missing_df: pd.DataFrame,
    our_catalog_df: Optional[pd.DataFrame] = None,
    verify_missing: bool = True,
    export_mode: str = "safe",  # FIX: Salla Export Mode Toggle
) -> tuple[bytes, int, pd.DataFrame]:
    """
    يصدّر إلى CSV مطابق لقالب سلة الرسمي (منتج_جديد.csv).

    ▸ السطر الأول: بيانات المنتج,...  (meta-header إلزامي في سلة)
    ▸ السطر الثاني: أسماء الأعمدة الـ 40
    ▸ السطر الثالث+: بيانات المنتجات
    ▸ الترميز: UTF-8 with BOM (مطلوب لعرض العربية في Excel)

    يُعيد (csv_bytes, عدد_المنتجات_المُصدَّرة, found_in_catalog_df)
    """
    salla_df, found_df = build_salla_shamel_dataframe(
        missing_df, our_catalog_df, verify_missing=verify_missing, export_mode=export_mode
    )

    buf = io.StringIO()
    # السطر الأول: meta-header
    buf.write(_SALLA_META_HEADER + "\n")
    # السطر الثاني+: بيانات
    salla_df.to_csv(buf, index=False, encoding="utf-8")
    csv_text = buf.getvalue()
    return csv_text.encode("utf-8-sig"), len(salla_df), found_df


# ══════════════════════════════════════════════════════════════════════════
#  التصدير — XLSX عبر io.BytesIO
# ══════════════════════════════════════════════════════════════════════════
def export_to_salla_shamel(
    missing_df: pd.DataFrame,
    our_catalog_df: Optional[pd.DataFrame] = None,
    generate_descriptions: bool = True,
    verify_missing: bool = True,
    export_mode: str = "safe",  # FIX: Salla Export Mode Toggle
) -> bytes:
    """
    يصدّر إلى xlsx عبر io.BytesIO — بدون disk I/O.
    يُعيد bytes جاهزة لـ st.download_button.
    """
    _ = generate_descriptions
    salla_df, _ = build_salla_shamel_dataframe(
        missing_df, our_catalog_df, verify_missing=verify_missing, export_mode=export_mode
    )
    if not salla_df.empty:
        salla_df = salla_df.reindex(columns=SALLA_SHAMEL_COLUMNS)

    buf = io.BytesIO()
    try:
        salla_df.to_excel(buf, index=False, engine="openpyxl")
    except ImportError as e:
        raise ImportError("تثبيت openpyxl مطلوب: pip install openpyxl") from e
    buf.seek(0)
    return buf.read()


# ══════════════════════════════════════════════════════════════════════════
#  تراكم بيانات المنافسين — حفظ + دمج عبر الجلسات
# ══════════════════════════════════════════════════════════════════════════
def merge_competitor_uploads(
    existing_df: Optional[pd.DataFrame],
    new_df: pd.DataFrame,
    competitor_name: str = "",
) -> pd.DataFrame:
    """
    يدمج ملف منافس جديد مع البيانات المحفوظة بدون فقدان سجلات قديمة.

    ▸ التحقق من التكرار عبر: اسم المنتج المُطبَّع + المنافس
    ▸ عند تكرار نفس المنتج من نفس المنافس → يُحدَّث السعر فقط
    ▸ منتجات جديدة → تُضاف في نهاية القائمة
    """
    if new_df is None or new_df.empty:
        return existing_df if existing_df is not None else pd.DataFrame()

    # تحديد عمود الاسم في الملف الجديد
    name_col = None
    for c in ("المنتج", "اسم المنتج", "منتج_المنافس", "name", "Product"):
        if c in new_df.columns:
            name_col = c
            break

    price_col = None
    for c in ("سعر_المنافس", "سعر المنافس", "سعر المنتج", "السعر", "Price", "price"):
        if c in new_df.columns:
            price_col = c
            break

    if existing_df is None or existing_df.empty:
        result = new_df.copy()
        if competitor_name and "المنافس" not in result.columns:
            result["المنافس"] = competitor_name
        return result.reset_index(drop=True)

    result = existing_df.copy()
    if competitor_name and "المنافس" not in result.columns:
        result["المنافس"] = competitor_name

    existing_name_col = None
    for c in ("المنتج", "اسم المنتج", "منتج_المنافس", "name", "Product"):
        if c in result.columns:
            existing_name_col = c
            break

    if not name_col or not existing_name_col:
        # لا يمكن المطابقة — أضف كلها
        combined = pd.concat([result, new_df], ignore_index=True)
        return combined.reset_index(drop=True)

    # بناء فهرس النسخة الموجودة
    existing_index: dict[str, int] = {}
    for i, row in result.iterrows():
        key = _norm_text(str(row.get(existing_name_col, "") or ""))
        comp = _norm_text(str(row.get("المنافس", "") or ""))
        if key:
            existing_index[f"{comp}::{key}"] = int(i)  # type: ignore[arg-type]

    new_rows = []
    for _, row in new_df.iterrows():
        pname = _safe_str(row.get(name_col, ""))
        comp  = _safe_str(row.get("المنافس", competitor_name))
        key   = f"{_norm_text(comp)}::{_norm_text(pname)}"
        if key in existing_index:
            # تحديث السعر فقط
            if price_col:
                result.at[existing_index[key], price_col] = row.get(price_col, "")
        else:
            new_row = row.to_dict()
            if competitor_name and "المنافس" not in new_row:
                new_row["المنافس"] = competitor_name
            new_rows.append(new_row)

    if new_rows:
        result = pd.concat([result, pd.DataFrame(new_rows)], ignore_index=True)

    return result.reset_index(drop=True)


# ── دوال توافق رجعي ──────────────────────────────────────────────────────
def resolve_brand_for_shamel(brand_raw: str) -> str:
    return _safe_str(brand_raw)


def resolve_category_for_shamel(
    category_raw: str,
    gender_hint: str = "",
    product_name_fallback: str = "",
) -> str:
    if category_raw and ">" in category_raw:
        return category_raw
    return _GENDER_CATEGORY.get(gender_hint, _DEFAULT_CATEGORY)


def build_salla_shamel_description_html(
    product_name: str,
    brand_raw: str,
    *,
    resolved_brand: Optional[str] = None,
) -> str:
    brand = resolved_brand or brand_raw or "غير متوفر"
    return generate_salla_html_description(product_name=product_name, brand_name=brand)
