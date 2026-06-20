"""
utils/data_sanitizer.py — طبقة تنظيف ومعالجة البيانات (Data Sanitization Layer)
v1.0 - القضاء على أخطاء التسمية والترجمات العشوائية وتداخل اللغات والبيانات المفقودة.
"""
from __future__ import annotations
import re
import pandas as pd

# ══════════════════════════════════════════════════════════════════════════════
# 1. قاموس توحيد المصطلحات
# ══════════════════════════════════════════════════════════════════════════════
_CONCENTRATION_RULES = [
    # الإنجليزية الكاملة أولاً
    (r"\bEau\s+[Dd]e\s+Parfum\b",    "أو دو بارفيوم"),
    (r"\bEDP\b",                       "أو دو بارفيوم"),
    (r"\bEau\s+[Dd]e\s+Toilette\b",  "أو دو تواليت"),
    (r"\bEDT\b",                       "أو دو تواليت"),
    (r"\bEau\s+[Dd]e\s+Cologne\b",   "أو دو كولون"),
    (r"\bEDC\b",                       "أو دو كولون"),
    (r"\bExtrait\b",                   "إكسترا دو بارفيم"),
    (r"\bParfum\b",                    "بارفيم"),
    # توحيد الصياغات العربية الخاطئة
    (r"\bأو\s+دو\s+بارفان\b",        "أو دو بارفيوم"),
    (r"\bاو\s+دو\s+بارفان\b",        "أو دو بارفيوم"),
    (r"\bاو\s+دي\s+بارفان\b",        "أو دو بارفيوم"),
    (r"\bاو\s+دي\s+بارفيوم\b",       "أو دو بارفيوم"),
    (r"\bاو\s+دي\s+تواليت\b",        "أو دو تواليت"),
    # بارفان/بارفيوم المنفردة
    (r"(?<!أو دو )\bبارفيوم\b",      "أو دو بارفيوم"),
    (r"(?<!أو دو )\bبارفان\b",       "أو دو بارفيوم"),
    # حذف "Eau de" المعلقة
    (r"\bEau\s+de\s*$",              ""),
    (r"\bEau\s+de\s+(?=[^A-Za-z])", ""),
    (r"\bEau\s+de\b",               ""),
]

_GENDER_RULES = [
    (r"\bللنساء\b",         "للنساء"),
    (r"\bللرجال\b",         "للرجال"),
    (r"\bللجنسين\b",        "للجنسين"),
    (r"\bنسائي\b",          "للنساء"),
    (r"\bرجالي\b",          "للرجال"),
    (r"\bfor\s+Women\b",    "للنساء"),
    (r"\bWomen\b",          "للنساء"),
    (r"\bWoman\b",          "للنساء"),
    (r"\bPour\s+Femme\b",   "للنساء"),
    (r"\bFemme\b",          "للنساء"),
    (r"\bFeminine\b",       "للنساء"),
    (r"\bfor\s+Men\b",      "للرجال"),
    (r"\bMen\b",            "للرجال"),
    (r"\bMan\b",            "للرجال"),
    (r"\bPour\s+Homme\b",   "للرجال"),
    (r"\bHomme\b",          "للرجال"),
    (r"\bUnisex\b",         "للجنسين"),
    (r"\bFor\s+All\b",      "للجنسين"),
]

_ENGLISH_NOISE_RE = re.compile(
    r"\b(?:Eau|de|for|EDP|EDT|EDC|ml|mL|Parfum|Toilette|Cologne|Extrait|"
    r"Spray|Natural|Intense|Limited|Edition|Collector|Collection|"
    r"Original|Authentic|Gift|Set)\b",
    re.IGNORECASE,
)


def standardize_terms(text: str) -> str:
    """الدالة 1: توحيد مصطلحات التركيز والجنس."""
    if not text:
        return text
    result = str(text)
    for pattern, replacement in _CONCENTRATION_RULES:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    for pattern, replacement in _GENDER_RULES:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", result).strip()


def sanitize_description_terms(html_or_md: str) -> str:
    """الدالة 5: تنظيف مصطلحات الوصف مع الحفاظ على وسوم HTML."""
    if not html_or_md:
        return html_or_md
    parts = re.split(r"(<[^>]+>)", html_or_md)
    return "".join(
        part if (part.startswith("<") and part.endswith(">")) else standardize_terms(part)
        for part in parts
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. المطابقة المرنة للماركات
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_for_match(s: str) -> str:
    s = str(s or "").lower().strip()
    s = re.sub(r"[^\w\u0600-\u06FF\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def get_brand_arabic_name(
    brand_input: str,
    store_brands: "list[str] | pd.DataFrame",
    brand_col: str = "اسم الماركة",
) -> str:
    """
    الدالة 2: مطابقة مرنة للماركات.
    يبحث في ملف ماركات مهووس بخمس مراحل: تطابق مباشر، جزء إنجليزي،
    كلمات جزئية، contains، نص منظّف.
    يرجع الاسم الرسمي الكامل مثل 'جريس | Gres' أو '' إذا لم يُوجد.
    """
    bv = str(brand_input or "").strip()
    if not bv:
        return ""

    if isinstance(store_brands, pd.DataFrame):
        brands_list = [
            str(x).strip()
            for x in store_brands[brand_col].dropna().tolist()
            if str(x).strip()
        ]
    else:
        brands_list = [str(x).strip() for x in store_brands if str(x).strip()]

    if not brands_list:
        return ""

    bv_norm = _normalize_for_match(bv)
    bv_en   = (bv.split("|")[-1].strip() if "|" in bv else bv).lower().strip()

    # 1. تطابق مباشر
    if bv in brands_list:
        return bv
    for sb in brands_list:
        if sb.lower() == bv.lower():
            return sb

    # 2. تطابق الجزء الإنجليزي (بعد |)
    for sb in brands_list:
        parts = [p.strip() for p in re.split(r"[|]", sb)]
        for part in parts:
            if part.lower() == bv_en:
                return sb
            if bv_en and bv_en in part.lower().split():
                return sb

    # 3. بحث بـ contains (case-insensitive)
    bv_safe = re.escape(bv_en)
    for sb in brands_list:
        if re.search(rf"\b{bv_safe}\b", sb, re.IGNORECASE):
            return sb

    # 4. مطابقة جزئية بالنص المنظّف
    for sb in brands_list:
        sb_norm = _normalize_for_match(sb)
        if bv_norm and (bv_norm in sb_norm or sb_norm in bv_norm):
            return sb

    return ""


def get_brand_display_name(full_brand_label: str) -> str:
    """من 'جريس | Gres' يرجع 'جريس' (الجزء العربي للعرض في العنوان)."""
    s = str(full_brand_label or "").strip()
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
        ar_parts = [p for p in parts if re.search(r"[\u0600-\u06FF]", p)]
        return ar_parts[0] if ar_parts else parts[0]
    return s


# ══════════════════════════════════════════════════════════════════════════════
# 3. فاحص القيم المفقودة
# ══════════════════════════════════════════════════════════════════════════════

def extract_size_ml(text: str) -> str:
    """الدالة 6: استخراج الحجم بالمل. يرجع '100 مل' أو ''."""
    if not text:
        return ""
    m = re.search(r"(\d{2,4})\s*مل\b", str(text))
    if m:
        return f"{m.group(1)} مل"
    m = re.search(r"(\d{2,4})\s*(?:ml|mL|ML)\b", str(text))
    if m:
        return f"{m.group(1)} مل"
    return ""


def _extract_concentration(text: str) -> str:
    t = str(text or "").lower()
    if re.search(r"\bextrait\b", t):                          return "إكسترا دو بارفيم"
    if re.search(r"\b(eau\s+de\s+parfum|edp)\b", t):         return "أو دو بارفيوم"
    if re.search(r"\b(eau\s+de\s+toilette|edt)\b", t):       return "أو دو تواليت"
    if re.search(r"\b(eau\s+de\s+cologne|edc)\b", t):        return "أو دو كولون"
    if re.search(r"\bparfum\b", t):                           return "بارفيم"
    return ""


def _extract_gender(text: str) -> str:
    t = str(text or "").lower()
    if re.search(r"\b(women|pour\s+femme|للنساء|نسائي|femme|for\s+women)\b", t): return "للنساء"
    if re.search(r"\b(men|pour\s+homme|للرجال|رجالي|homme|for\s+men)\b", t):    return "للرجال"
    if re.search(r"\b(unisex|للجنسين|for\s+all)\b", t):                         return "للجنسين"
    return ""


def validate_product_data(product_dict: dict) -> dict:
    """
    الدالة 3: فحص القيم المطلوبة قبل توليد العنوان أو الوصف.

    يرجع: { status, missing, warnings, clean }
    status = "OK" | "Missing Data" | "Warning"
    إذا كان الحجم مفقوداً → status="Missing Data" → توقف وعلامة ⚠️
    """
    missing, warnings, clean = [], [], {}
    raw_name = str(product_dict.get("name") or "").strip()
    clean["name"] = raw_name

    # الماركة
    brand = str(product_dict.get("brand") or "").strip()
    if not brand or brand.lower() in ("nan", "none", ""):
        missing.append("الماركة")
    clean["brand"] = brand

    # الحجم
    size_raw = str(product_dict.get("size") or "").strip()
    if size_raw and re.search(r"\d", size_raw):
        clean["size"] = size_raw if "مل" in size_raw else f"{size_raw} مل"
    else:
        extracted = extract_size_ml(raw_name)
        if extracted:
            clean["size"] = extracted
            warnings.append(f"الحجم استُخرج تلقائياً من الاسم: {extracted}")
        else:
            missing.append("الحجم")
            clean["size"] = ""

    # التركيز
    conc = str(product_dict.get("concentration") or "").strip()
    if not conc or conc.lower() in ("nan", "none", ""):
        conc_ex = _extract_concentration(raw_name)
        if conc_ex:
            clean["concentration"] = conc_ex
            warnings.append(f"التركيز استُخرج من الاسم: {conc_ex}")
        else:
            missing.append("التركيز")
            clean["concentration"] = ""
    else:
        clean["concentration"] = standardize_terms(conc)

    # الجنس
    gender = str(product_dict.get("gender") or "").strip()
    gender_std = standardize_terms(gender)
    if gender_std not in ("للنساء", "للرجال", "للجنسين"):
        gen_ex = _extract_gender(raw_name)
        if gen_ex:
            clean["gender"] = gen_ex
            warnings.append(f"الجنس استُخرج من الاسم: {gen_ex}")
        else:
            missing.append("الجنس")
            clean["gender"] = ""
    else:
        clean["gender"] = gender_std

    for key in ("arabic_perfume_name", "year", "designer", "family"):
        if product_dict.get(key):
            clean[key] = str(product_dict[key]).strip()

    status = "Missing Data" if missing else ("Warning" if warnings else "OK")
    return {"status": status, "missing": missing, "warnings": warnings, "clean": clean}


# ══════════════════════════════════════════════════════════════════════════════
# 4. دالة بناء العنوان الصارم
# ══════════════════════════════════════════════════════════════════════════════

def build_arabic_product_title(
    product_type: str = "عطر",
    arabic_perfume_name: str = "",
    brand_arabic: str = "",
    concentration: str = "",
    size: str = "",
    gender: str = "",
    max_length: int = 220,
) -> str:
    """
    الدالة 4: بناء عنوان عربي صارم خالٍ من الإنجليزية.

    الهيكل: [نوع] + [اسم العطر] + [الماركة] + [التركيز] + [الحجم] + [الجنس]
    مثال: عطر إيفوريا سبرينغ تيمبتيشن كالفن كلاين أو دو بارفيوم 100 مل للنساء
    """
    conc_clean   = standardize_terms(str(concentration or "").strip())
    gender_clean = standardize_terms(str(gender or "").strip())
    size_clean   = str(size or "").strip()
    if size_clean and "مل" not in size_clean and re.match(r"^\d+$", size_clean):
        size_clean = f"{size_clean} مل"

    pieces = [
        str(product_type or "عطر").strip(),
        str(arabic_perfume_name or "").strip(),
        str(brand_arabic or "").strip(),
        conc_clean,
        size_clean,
        gender_clean,
    ]
    title = " ".join(p for p in pieces if p)
    title = _ENGLISH_NOISE_RE.sub(" ", title)
    return re.sub(r"\s{2,}", " ", title).strip()[:max_length]


def build_title_from_raw(
    raw_name: str,
    brand_input: str,
    store_brands: "list[str] | pd.DataFrame",
    product_type: str = "عطر",
    arabic_perfume_name: str = "",
    gender_hint: str = "",
    brand_col: str = "اسم الماركة",
) -> dict:
    """دالة 7 — شاملة: اسم خام → عنوان نظيف + حالة الصحة."""
    size   = extract_size_ml(raw_name)
    conc   = standardize_terms(_extract_concentration(raw_name))
    gender = standardize_terms(_extract_gender(gender_hint) or _extract_gender(raw_name))

    brand_label   = get_brand_arabic_name(brand_input, store_brands, brand_col)
    brand_display = get_brand_display_name(brand_label) if brand_label else brand_input.strip()

    validation = validate_product_data({
        "name": raw_name, "brand": brand_label or brand_input,
        "size": size, "concentration": conc, "gender": gender,
    })

    title = build_arabic_product_title(
        product_type=product_type,
        arabic_perfume_name=(arabic_perfume_name or "").strip(),
        brand_arabic=brand_display,
        concentration=validation["clean"].get("concentration", conc),
        size=validation["clean"].get("size", size),
        gender=validation["clean"].get("gender", gender),
    )

    return {
        "title":         title,
        "brand_label":   brand_label,
        "brand_display": brand_display,
        "concentration": validation["clean"].get("concentration", conc),
        "size":          validation["clean"].get("size", size),
        "gender":        validation["clean"].get("gender", gender),
        "status":        validation["status"],
        "missing":       validation["missing"],
        "warnings":      validation["warnings"],
    }


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 68)
    print("اختبار طبقة التنظيف — Data Sanitization Layer")
    print("=" * 68)

    print("\n[1] standardize_terms:")
    for c in ["أو دو بارفان", "Eau de Parfum", "EDP for Women",
              "Pour Homme EDT", "Eau de", "بارفان", "نسائي", "رجالي"]:
        print(f"  {c!r:40} → {standardize_terms(c)!r}")

    print("\n[2] extract_size_ml:")
    for c in ["Gres Cabotine Gold EDP 100ml",
              "عطر Calvin Klein CK One Shock Eau de Toilette أو دو تواليت",
              "100 مل للنساء", "50mL Spray"]:
        print(f"  {extract_size_ml(c)!r:10} ← {c!r}")

    brands_test = ["جريس | Gres", "كالفن كلاين | Calvin Klein", "جيفنشي | Givenchy"]
    print("\n[3] get_brand_arabic_name:")
    for bt in ["Gres", "GRES", "Calvin Klein", "Givenchy", "Unknown"]:
        r = get_brand_arabic_name(bt, brands_test)
        print(f"  {bt!r:20} → {r!r:32} display={get_brand_display_name(r)!r}")

    print("\n[4] validate — CK One Shock (حجم مفقود):")
    v = validate_product_data({
        "name": "Calvin Klein CK One Shock Eau de Toilette",
        "brand": "Calvin Klein", "size": "", "concentration": "", "gender": "للرجال"
    })
    print(f"  status={v['status']}  missing={v['missing']}")

    print("\n[5] build_arabic_product_title:")
    for tc in [
        dict(arabic_perfume_name="كابوتين جولد", brand_arabic="جريس",
             concentration="أو دو تواليت", size="100 مل", gender="للنساء"),
        dict(arabic_perfume_name="إيفوريا سبرينغ تيمبتيشن", brand_arabic="كالفن كلاين",
             concentration="أو دو بارفيوم", size="100 مل", gender="للنساء"),
        dict(arabic_perfume_name="سي كيه ون شوك", brand_arabic="كالفن كلاين",
             concentration="أو دو تواليت", size="100 مل", gender="للرجال"),
    ]:
        print(f"  → {build_arabic_product_title(**tc)}")
    print("\n✅ اكتملت الاختبارات")

# الوحدة الرابعة — طبقة التنظيف الشاملة (Mahwous Full Sanitization Layer)
# ══════════════════════════════════════════════════════════════════════════════

_URL_RE = re.compile(
    r"https?://[^\s\"'<>،]+|www\.[^\s\"'<>،]+",
    re.IGNORECASE,
)

_FILLER_PHRASES_AR = re.compile(
    r"(?:"
    r"تواصل\s+معنا|اتصل\s+بنا|للاستفسار|للطلب|"
    r"يرجى\s+التواصل|اضغط\s+هنا|انقر\s+هنا|"
    r"تابعونا\s+على|تابعنا\s+على|حسابنا\s+على|"
    r"لمزيد\s+من\s+المعلومات|خدمة\s+العملاء|"
    r"الشحن\s+مجاني|توصيل\s+سريع|ضمان\s+الأصالة"
    r")",
    re.IGNORECASE,
)

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^\)]+\)")
_BARE_HASHTAG_RE  = re.compile(r"#\w+")


def remove_links_and_noise(text: str) -> str:
    """
    يُزيل:
    • روابط URL كاملة
    • روابط Markdown [نص](url)
    • هاشتاقات عشوائية
    • عبارات الحشو التسويقي (تواصل معنا، اضغط هنا...)
    """
    if not text:
        return text
    t = _MARKDOWN_LINK_RE.sub(r"\1", text)   # [نص](url) → نص
    t = _URL_RE.sub("", t)
    t = _BARE_HASHTAG_RE.sub("", t)
    t = _FILLER_PHRASES_AR.sub("", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def enforce_markdown_structure(text: str) -> str:
    """
    يضمن أن الوصف يستخدم Markdown صحيحاً:
    • يُبقي العناوين (## / ###) سليمة
    • يُحوّل الأسطر المفصولة بـ - / * / • إلى قوائم Markdown
    • يُزيل الفقرات الفارغة المتكررة
    """
    if not text:
        return text
    lines = text.split("\n")
    out   = []
    for ln in lines:
        stripped = ln.strip()
        # تحويل نقاط غير قياسية → Markdown list
        if stripped and stripped[0] in ("•", "◦", "‣", "▪", "►"):
            stripped = "- " + stripped[1:].strip()
        out.append(stripped)
    # إزالة أسطر فارغة متتالية أكثر من اثنتين
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return result.strip()


def sanitize_full_description(
    text: str,
    apply_terms: bool = True,
    apply_links: bool = True,
    apply_markdown: bool = True,
) -> str:
    """
    الدالة الرئيسية لطبقة التنظيف الشاملة (Mahwous Sanitization).

    التسلسل:
    1. إزالة الروابط والحشو  (remove_links_and_noise)
    2. توحيد المصطلحات       (standardize_terms)
    3. تنظيف HTML/Markdown   (sanitize_description_terms)
    4. ضمان هيكل Markdown    (enforce_markdown_structure)

    Args:
        text:            النص الخام (HTML أو Markdown أو نص عادي)
        apply_terms:     تطبيق توحيد المصطلحات (التركيز / الجنس)
        apply_links:     حذف الروابط والحشو
        apply_markdown:  ضمان هيكل Markdown

    Returns:
        نص نظيف جاهز للعرض في واجهة مهووس
    """
    if not text:
        return text
    t = str(text)
    if apply_links:
        t = remove_links_and_noise(t)
    if apply_terms:
        t = sanitize_description_terms(t)   # يتعامل مع HTML tags أيضاً
    if apply_markdown:
        t = enforce_markdown_structure(t)
    return t


def sanitize_new_product(product: dict) -> dict:
    """
    يُطبّق طبقة التنظيف الإجبارية على منتج جديد قبل حفظه.
    مدخل: dict بالحقول المعتادة (name, description, brand, gender, concentration, size)
    مخرج: نفس الـ dict مع الحقول المُنظَّفة + _sanitized=True

    يُستدعى من magic_factory عند إنشاء منتج جديد.
    """
    clean = dict(product)

    # الاسم — توحيد المصطلحات فقط (لا نحذف أجزاء الاسم)
    if clean.get("name"):
        clean["name"] = standardize_terms(str(clean["name"]))

    # الوصف — تنظيف شامل
    if clean.get("description"):
        clean["description"] = sanitize_full_description(str(clean["description"]))

    # الماركة — تطبيع
    if clean.get("brand"):
        clean["brand"] = str(clean["brand"]).strip()

    # التركيز والجنس — توحيد
    for key in ("concentration", "gender"):
        if clean.get(key):
            clean[key] = standardize_terms(str(clean[key]))

    # الحجم — تطبيع
    if clean.get("size"):
        sz = str(clean["size"]).strip()
        if re.match(r"^\d+$", sz):
            sz += " مل"
        clean["size"] = sz

    clean["_sanitized"] = True
    return clean


# ══════════════════════════════════════════════════════════════════════════════
# إدارة الماركات الجديدة — Auto Brand Generation
# ══════════════════════════════════════════════════════════════════════════════

def _seo_url(text: str) -> str:
    """يُولّد SEO URL من نص عربي/إنجليزي"""
    t = str(text or "").strip().lower()
    t = re.sub(r"[^\w\u0600-\u06FF\s-]", "", t)
    t = re.sub(r"\s+", "-", t)
    return t[:60].strip("-")


def generate_brand_record(
    brand_name_en: str,
    brand_name_ar: str = "",
    description_ar: str = "",
    logo_url: str = "",
) -> dict:
    """
    يُولّد سجل ماركة جديد بالهيكلة المعتمدة في brands.csv / سلة.

    Args:
        brand_name_en: الاسم الإنجليزي (مطلوب)
        brand_name_ar: الاسم العربي (يُوّلد من AI إذا فارغ)
        description_ar: وصف عربي (يُوّلد من AI إذا فارغ)
        logo_url:      رابط الشعار

    Returns:
        dict يُحفظ مباشرةً في brands.csv
        {
          "اسم الماركة", "وصف مختصر عن الماركة",
          "صورة شعار الماركة", "Page Title", "SEO Page URL", "Page Description"
        }
    """
    en = brand_name_en.strip()
    ar = brand_name_ar.strip() if brand_name_ar else ""

    # اسم الماركة بصيغة: عربي | إنجليزي
    label = f"{ar} | {en}" if ar else en

    # SEO URL
    seo_part = _seo_url(ar or en)
    seo_url  = f"ماركة-{seo_part}" if ar else f"brand-{_seo_url(en)}"

    # page title
    page_title = (
        f"{ar or en} | عطور فاخرة - مهووس للعطور"
    )

    # page description
    if description_ar:
        page_desc = description_ar[:160]
    else:
        page_desc = f"اكتشف عطور {ar or en} الأصيلة في مهووس — تشكيلة متنوعة بأسعار تنافسية."

    return {
        "اسم الماركة":               label,
        "وصف مختصر عن الماركة":      description_ar or f"عطور {ar or en} الأصيلة.",
        "صورة شعار الماركة":         logo_url,
        "(إختياري) صورة البانر":     "",
        "(Page Title) عنوان صفحة العلامة التجارية":   page_title,
        "(SEO Page URL) رابط صفحة العلامة التجارية": seo_url,
        "(Page Description) وصف صفحة العلامة التجارية": page_desc,
    }


# ══════════════════════════════════════════════════════════════════════════════
# أسعار ملفات المنافسين (نصوص عربية / ر.س / فواصل آلاف)
# ══════════════════════════════════════════════════════════════════════════════

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٬٫", "0123456789,.")


def sanitize_competitor_price_to_float(val) -> float:
    """
    يستخرج أول سعر رقمي معقول من خلية قد تحتوي «1,234.50 ر.س» أو «٣٥٠ ريال» أو HTML.
    يُستخدم مع تصديرات سلة / الكشط (عمود مثل text-sm-2).
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "-", "—", "null"):
        return 0.0
    s = s.translate(_AR_DIGITS)
    s = re.sub(r"ر\.?\s*س|ريال|SAR|SR|﷼|\uFDFC|ر\s*س", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    candidates = re.findall(r"\d[\d,\.]*", s)
    if not candidates:
        return 0.0
    raw = candidates[-1].replace(",", "")
    try:
        x = float(raw)
    except ValueError:
        return 0.0
    if x < 0 or x > 10_000_000:
        return 0.0
    return float(x)


def append_brand_to_csv(brand_record: dict, csv_path: str) -> bool:
    """
    يُضيف سجل ماركة جديد إلى brands.csv إذا لم يكن موجوداً.

    Returns:
        True إذا أُضيف، False إذا كان موجوداً مسبقاً
    """
    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
    except Exception:
        df = pd.DataFrame(columns=list(brand_record.keys()))

    label = brand_record.get("اسم الماركة", "").strip()
    # تحقق من التكرار
    existing = df["اسم الماركة"].astype(str).str.strip().tolist() if "اسم الماركة" in df.columns else []
    for ex in existing:
        en_part = ex.split("|")[-1].strip().lower() if "|" in ex else ex.lower()
        en_new  = label.split("|")[-1].strip().lower() if "|" in label else label.lower()
        if en_new and en_new == en_part:
            return False  # موجود مسبقاً

    new_row = pd.DataFrame([brand_record])
    df_out  = pd.concat([df, new_row], ignore_index=True)
    df_out.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return True
