"""
engines/challenge_engine.py — محرك تحليل وتسعير تلقائي للعطور (نسخة التحدي)
═══════════════════════════════════════════════════════════════════════════
المهمة: لكل منتج من ملفات المنافسين → مطابقة مع كتالوج المتجر الأساسي
التصنيف الثلاثي المحافظ:
  ✅ مطابق مؤكد     — score ≥ 88 + لا تعارض حاسم في الحجم/التركيز/التستر/الإصدار
  ⚠️ تحت المراجعة  — score 65-87 OR تعارض في صفة واحدة غير حاسمة
  🔍 مفقود مؤكد    — score < 65 بعد جميع طبقات التحقق
ملاحظة: كل منتج يجب أن ينتهي في قسم — لا يُحذف أي سجل بصمت.
"""
from __future__ import annotations

import re
import io
import gc
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:
    from rapidfuzz import fuzz, process as rf_process
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False

# ─── ثوابت التصنيف ──────────────────────────────────────────────────────────
CONFIRMED_SCORE   = 88   # score ≥ هذا → مطابق مؤكد (مع فحص العوامل الحاسمة)
REVIEW_SCORE      = 65   # score ≥ هذا → تحت المراجعة
MISSING_SCORE     = 64   # score < هذا → مفقود مؤكد

PRICE_ALERT_PCT   = 20   # فرق سعر > 20% → تنبيه سعري
MIN_SAMPLE_ML     = 10   # حجم ≤ هذا بالمل → عينة صغيرة
MIN_RETAIL_ML     = 10   # حجم ≤ هذا → لا يُعتبر فرصة استحواذ حقيقية

# ─── قاموس المرادفات للتطبيع ────────────────────────────────────────────────
_SYN: Dict[str, str] = {
    # تركيزات
    "بيرفيوم": "edp", "بارفيوم": "edp", "برفيوم": "edp", "بارفيومز": "edp",
    "برفان": "edp", "parfum": "edp", "perfume": "edp", "بيرفيومز": "edp",
    "تواليت": "edt", "تواليتة": "edt", "toilette": "edt", "او دو تواليت": "edt",
    "او دي تواليت": "edt", "او دو بارفيوم": "edp", "او دي بارفيوم": "edp",
    "eau de parfum": "edp", "eau de toilette": "edt", "eau de cologne": "edc",
    "كولون": "edc", "cologne": "edc",
    "اكستريت": "extrait", "اكسترايت": "extrait", "extract": "extrait",
    # تهجئات شائعة
    "سوفاج": "sauvage", "سوفايج": "sauvage", "سافاج": "sauvage",
    "بلو": "bleu", "نوار": "noir", "نوير": "noir",
    "روز": "rose", "روس": "rose", "عود": "oud", "عودي": "oud",
    "انتنس": "intense", "انتينس": "intense", "إنتنس": "intense",
    "ابسولو": "absolue", "ابسوليو": "absolue", "absolu": "absolue",
    "بريفيه": "prive", "privee": "prive", "privé": "prive",
    "جولد": "gold", "قولد": "gold", "سيلفر": "silver", "بلاك": "black",
    "وايت": "white", "نايت": "night",
    # أحجام عربية
    "٥٠": "50", "٧٥": "75", "١٠٠": "100", "١٢٥": "125",
    "١٥٠": "150", "٢٠٠": "200", "٣٠": "30",
    # ماركات
    "لطافة": "lattafa", "رصاصي": "rasasi", "اجمل": "ajmal", "أجمل": "ajmal",
    "امواج": "amouage", "أمواج": "amouage", "ارماف": "armaf", "أرماف": "armaf",
    "مونتال": "montale", "مانسيرا": "mancera", "كيليان": "kilian",
    "مارلي": "parfums de marly", "بارفيومز دي مارلي": "parfums de marly",
    "ديور": "dior", "شانيل": "chanel", "غوتشي": "gucci", "قوتشي": "gucci",
    "برادا": "prada", "توم فورد": "tom ford", "أرماني": "armani", "ارماني": "armani",
    "فيرساتشي": "versace", "فرساتشي": "versace", "فيرزاتشي": "versace",
    "هيرميس": "hermes", "كريد": "creed",
    "بلغاري": "bvlgari", "بولغاري": "bvlgari",
    "مونت بلانك": "montblanc", "لانكوم": "lancome",
    "جيفنشي": "givenchy", "باكو رابان": "paco rabanne",
    "ايف سان لوران": "ysl", "إيف سان لوران": "ysl", "yves saint laurent": "ysl",
    "دانهيل": "dunhill", "دنهيل": "dunhill", "دن هيل": "dunhill",
    "جو مالون": "jo malone", "جيرلان": "guerlain", "فالنتينو": "valentino",
    "كلوب دي نوي": "club de nuit",
    # تصحيح إملائي
    "ايسينشيال": "essential", "اسنشيال": "essential",
    "سولييل": "soleil", "بيور": "pure",
    "اوريجينال": "original", "أوريجينال": "original",
}

_NOISE_RE = re.compile(
    r"\b(عطر|تستر|تيستر|tester|"
    r"بارفيوم|بيرفيوم|برفيوم|برفان|"
    r"تواليت|كولون|اكسترايت|اكستريت|"
    r"او\s*دو|او\s*دي|أو\s*دو|أو\s*دي|"
    r"الرجالي|النسائي|للجنسين|رجالي|نسائي|"
    r"parfum|perfume|cologne|toilette|extrait|intense|"
    r"eau\s*de|pour\s*homme|pour\s*femme|for\s*men|for\s*women|unisex|"
    r"edp|edt|edc)\b"
    r"|\b\d+(?:\.\d+)?\s*(?:ml|مل|ملي|oz)\b"
    r"|\b(100|200|50|75|150|125|250|300|30|80)\b",
    re.UNICODE | re.IGNORECASE,
)

_TESTER_WORDS = [
    "tester", "تستر", "تيستر", "بدون كرتون", "بدون علبه", "بدون علبة",
    "unboxed", "no box", "no-box",
]
_SAMPLE_WORDS = [
    "sample", "mini", "miniature", "decant", "split",
    "عينة", "عينه", "سمبل", "تقسيم", "مينياتشر",
]
_SET_WORDS = ["set", "gift set", "طقم", "مجموعة", "بوكس", "باك", "coffret", "kit"]

_KNOWN_BRANDS = [
    "Dior", "Chanel", "Gucci", "Tom Ford", "Versace", "Armani", "YSL", "Prada",
    "Burberry", "Hermes", "Creed", "Montblanc", "Amouage", "Rasasi", "Lattafa",
    "Arabian Oud", "Ajmal", "Al Haramain", "Afnan", "Armaf", "Mancera", "Montale",
    "Kilian", "Jo Malone", "Carolina Herrera", "Paco Rabanne", "Mugler",
    "Ralph Lauren", "Parfums de Marly", "Nishane", "Xerjoff", "Byredo",
    "Le Labo", "Roja", "Narciso Rodriguez", "Dolce & Gabbana", "Valentino",
    "Bvlgari", "Cartier", "Hugo Boss", "Calvin Klein", "Givenchy", "Lancome",
    "Guerlain", "Jean Paul Gaultier", "Issey Miyake", "Davidoff", "Coach",
    "Michael Kors", "Initio", "Memo Paris", "Maison Margiela", "Diptyque",
    "Swiss Arabian", "Ard Al Zaafaran", "Nabeel", "Asdaaf", "Maison Alhambra",
    "Tiziana Terenzi", "Maison Francis Kurkdjian", "Club De Nuit", "Dunhill",
    "لطافة", "العربية للعود", "رصاسي", "أجمل", "الحرمين", "أرماف",
    "أمواج", "افنان", "سويس عربيان", "ارض الزعفران", "نابيل", "اصداف",
]

# ─── دوال التطبيع ────────────────────────────────────────────────────────────

def _safe_str(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none", "null", "<na>", "") else s


def normalize(text: str) -> str:
    """تطبيع كامل مع المرادفات وتوحيد الهمزات."""
    if not isinstance(text, str):
        return ""
    t = text.strip().lower()
    for k, v in _SYN.items():
        t = t.replace(k, v)
    for src, dst in [("أ","ا"),("إ","ا"),("آ","ا"),("ة","ه"),("ى","ي"),("ؤ","و"),("ئ","ي"),("ـ","")]:
        t = t.replace(src, dst)
    t = re.sub(r"[^\w\s\u0600-\u06FF.]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_name(text: str) -> str:
    """تطبيع اسم للمقارنة: يحذف الضجيج (عطر/بارفيوم/مل/edp...) مع الحفاظ على الهوية."""
    if not isinstance(text, str):
        return ""
    t = text.strip().lower()
    for k, v in _SYN.items():
        t = t.replace(k, v)
    for src, dst in [("أ","ا"),("إ","ا"),("آ","ا"),("ة","ه"),("ى","ي"),("ؤ","و"),("ئ","ي"),("ـ","")]:
        t = t.replace(src, dst)
    t = _NOISE_RE.sub(" ", t)
    t = re.sub(r"\b\d+\b", " ", t)
    t = re.sub(r"[^\w\s\u0600-\u06FF]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def extract_size(text: str) -> float:
    """استخراج الحجم بالمل من النص."""
    if not isinstance(text, str):
        return 0.0
    tl = text.lower()
    oz = re.findall(r"(\d+(?:\.\d+)?)\s*(?:oz|ounce)", tl)
    if oz:
        return round(float(oz[0]) * 29.5735, 1)
    ml = re.findall(r"(\d+(?:\.\d+)?)\s*(?:ml|مل|ملي|milliliter)", tl)
    return float(ml[0]) if ml else 0.0


def extract_concentration(text: str) -> str:
    """استخراج التركيز (EDP/EDT/EDC/EXTRAIT/INTENSE)."""
    if not isinstance(text, str):
        return ""
    n = normalize(text)
    if "extrait" in n:
        return "EXTRAIT"
    if "intense" in n:
        return "INTENSE"
    if "edp" in n or "parfum" in n:
        return "EDP"
    if "edt" in n or "toilette" in n:
        return "EDT"
    if "edc" in n or "cologne" in n:
        return "EDC"
    return ""


def extract_brand(text: str) -> str:
    """استخراج الماركة من النص — مباشر ثم تصحيح إملائي."""
    if not isinstance(text, str):
        return ""
    n = normalize(text)
    tl = text.lower()
    for b in _KNOWN_BRANDS:
        if normalize(b) in n or b.lower() in tl:
            return b.lower()
    if _HAS_RAPIDFUZZ:
        words = text.split()
        for i in range(len(words)):
            for length in [3, 2, 1]:
                if i + length <= len(words):
                    candidate = " ".join(words[i:i+length])
                    if len(candidate) < 3:
                        continue
                    res = rf_process.extractOne(
                        normalize(candidate),
                        [normalize(b) for b in _KNOWN_BRANDS],
                        scorer=fuzz.ratio,
                        score_cutoff=84,
                    )
                    if res:
                        return _KNOWN_BRANDS[res[2]].lower()
    return ""


def extract_gender(text: str) -> str:
    """استخراج الجنس (رجالي/نسائي)."""
    if not isinstance(text, str):
        return ""
    tl = text.lower()
    m = any(k in tl for k in ["pour homme","for men"," men "," man ","رجالي","للرجال","homme","uomo","mans"])
    w = any(k in tl for k in ["pour femme","for women","women"," woman ","نسائي","للنساء","lady","femme","donna"])
    if m and not w:
        return "رجالي"
    if w and not m:
        return "نسائي"
    return ""


def is_sample(text: str) -> bool:
    if not isinstance(text, str):
        return False
    tl = text.lower()
    return any(k in tl for k in _SAMPLE_WORDS)


def is_tester(text: str) -> bool:
    if not isinstance(text, str):
        return False
    tl = text.lower()
    return any(k in tl for k in _TESTER_WORDS)


def is_set(text: str) -> bool:
    if not isinstance(text, str):
        return False
    tl = text.lower()
    return any(k in tl for k in _SET_WORDS)


def classify_product_type(name: str) -> str:
    """تصنيف نوع المنتج: retail/tester/sample/set/other."""
    if not isinstance(name, str):
        return "retail"
    nl = name.lower()
    if is_sample(name):
        return "sample"
    if is_tester(name):
        return "tester"
    if is_set(name):
        return "set"
    if re.search(r"\bhair\s*mist\b|عطر\s*شعر|للشعر|\bhair\b", nl):
        return "hair_mist"
    if re.search(r"\bbody\s*mist\b|بودي\s*مست|بخاخ\s*جسم|\bbody\s*spray\b", nl):
        return "body_mist"
    if re.search(
        r"استشوار|مكواة|ماسكرا|ايلاينر|ظل\s*عيون|روج|أحمر\s*شفاه|"
        r"بلاشر|فونديشن|ظلال|كونسيلر|طلاء\s*اظافر|nail|mascara|eyeliner|"
        r"lipstick|foundation|blush|contour|makeup|مكياج",
        nl
    ):
        return "other"
    return "retail"


# ─── استخراج خط الإنتاج (الاسم المجرد بدون ماركة/كلمات شائعة) ──────────────

_PRODUCT_LINE_STOP = re.compile(
    r"\b(عطر|parfum|perfume|tester|تستر|تيستر|edp|edt|edc|extrait|eau|de|du|la|le|les|pour|"
    r"homme|femme|men|women|unisex|او|دو|دي|ال|ml|مل|ملي|new|جديد|original|اصلي)\b",
    re.IGNORECASE | re.UNICODE,
)


def extract_product_line(text: str, brand: str = "") -> str:
    """استخراج اسم خط الإنتاج (المنتج المجرد) لمقارنة دقيقة."""
    if not isinstance(text, str):
        return ""
    n = text.lower()
    if brand:
        n = n.replace(normalize(brand), " ").replace(brand.lower(), " ")
        for k, v in _SYN.items():
            if v == normalize(brand) or v == brand.lower():
                n = n.replace(k, " ")
    n = _NOISE_RE.sub(" ", n)
    n = _PRODUCT_LINE_STOP.sub(" ", n)
    n = re.sub(r"\d+(?:\.\d+)?\s*(?:ml|مل|ملي)?", " ", n)
    for src, dst in [("أ","ا"),("إ","ا"),("آ","ا"),("ة","ه"),("ى","ي")]:
        n = n.replace(src, dst)
    n = re.sub(r"[^\w\s\u0600-\u06FF]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


# ─── كشف تعارض الإصدار (flanker conflict) ────────────────────────────────────
_FLANKERS = [
    "intense", "انتنس", "extreme", "اكستريم", "elixir", "الكسير",
    "absolue", "ابسولو", "oud", "عود", "le parfum", "noir", "نوار",
    "black", "بلاك", "rose", "روز", "amber", "عنبر", "musk", "مسك",
    "fresh", "silver", "سيلفر", "gold", "جولد", "prive", "بريفيه",
    "bleu", "بلو", "sport", "سبورت", "eau fraiche",
]


def flanker_conflict(name_a: str, name_b: str) -> bool:
    """True إذا كان الاسمان ينتميان لإصدارين مختلفين من نفس العطر."""
    def _flankers_in(t: str) -> set:
        tl = t.lower()
        return {f for f in _FLANKERS if f in tl}
    fa = _flankers_in(name_a)
    fb = _flankers_in(name_b)
    if not fa and not fb:
        return False
    return fa != fb


# ─── حساب درجة التشابه ──────────────────────────────────────────────────────

def score_names(a: str, b: str) -> float:
    """درجة تشابه الأسماء (0-100) باستخدام عدة خوارزميات."""
    if not a or not b:
        return 0.0
    if not _HAS_RAPIDFUZZ:
        # fallback: Jaccard على المقاطع
        sa = set(a.split())
        sb = set(b.split())
        if not sa or not sb:
            return 0.0
        inter = len(sa & sb)
        union = len(sa | sb)
        return round(inter / union * 100, 2)
    s1 = fuzz.ratio(a, b)
    s2 = fuzz.partial_ratio(a, b)
    s3 = fuzz.token_sort_ratio(a, b)
    s4 = fuzz.token_set_ratio(a, b)
    return round(max(s1, s2 * 0.9, s3 * 0.95, s4 * 0.95), 2)


@dataclass
class NormProduct:
    """منتج موحّد جاهز للمقارنة."""
    name_raw: str
    name_norm: str
    name_bare: str        # بدون ضجيج
    product_line: str     # اسم خط الإنتاج
    brand: str
    size_ml: float
    concentration: str
    gender: str
    is_tester_flag: bool
    is_sample_flag: bool
    is_set_flag: bool
    product_type: str
    price: float
    sku: str
    image_url: str
    product_url: str
    original_index: int
    competitor_name: str = ""

    @classmethod
    def from_row(
        cls,
        row: pd.Series,
        name_col: str,
        price_col: Optional[str],
        sku_col: Optional[str],
        img_col: Optional[str],
        url_col: Optional[str],
        idx: int,
        competitor: str = "",
    ) -> "NormProduct":
        raw = _safe_str(row.get(name_col, ""))
        price = 0.0
        if price_col and price_col in row.index:
            try:
                price = float(str(row[price_col]).replace(",", "").strip())
            except (ValueError, TypeError):
                pass
        sku = ""
        if sku_col and sku_col in row.index:
            sku = _safe_str(row.get(sku_col, ""))
        img = ""
        if img_col and img_col in row.index:
            v = _safe_str(row.get(img_col, ""))
            img = v.split(",")[0].split("\n")[0].strip() if v else ""
        url = ""
        if url_col and url_col in row.index:
            url = _safe_str(row.get(url_col, ""))
        _brand = extract_brand(raw)
        _pl_raw = extract_product_line(raw, _brand)
        return cls(
            name_raw=raw,
            name_norm=normalize(raw),
            name_bare=normalize_name(raw),
            product_line=normalize(_pl_raw),   # تطبيع بعد الاستخراج لضمان مطابقة عربي/إنجليزي
            brand=_brand,
            size_ml=extract_size(raw),
            concentration=extract_concentration(raw),
            gender=extract_gender(raw),
            is_tester_flag=is_tester(raw),
            is_sample_flag=is_sample(raw),
            is_set_flag=is_set(raw),
            product_type=classify_product_type(raw),
            price=price,
            sku=sku,
            image_url=img,
            product_url=url,
            original_index=idx,
            competitor_name=competitor,
        )


# ─── كشف أعمدة الملف ────────────────────────────────────────────────────────

def _fcol(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    """بحث مرن عن عمود في DataFrame."""
    cols = list(df.columns)
    def _norm_ar(s):
        return str(s).replace("أ","ا").replace("إ","ا").replace("آ","ا").strip().lower()
    norm_map = {_norm_ar(c): c for c in cols}
    for c in candidates:
        if c in cols:
            return c
        if _norm_ar(c) in norm_map:
            return norm_map[_norm_ar(c)]
    for c in candidates:
        for col in cols:
            if c.lower() in str(col).lower():
                return col
    return None


def detect_columns(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    """اكتشاف تلقائي لأعمدة الملف."""
    name_col = _fcol(df, [
        "اسم المنتج", "أسم المنتج", "المنتج", "اسم", "name",
        "product_name", "product name", "title", "product",
    ])
    if not name_col:
        # اختر أول عمود نصي
        for col in df.columns:
            sample = df[col].dropna().astype(str).head(5)
            if sample.apply(len).mean() > 10:
                name_col = col
                break
    price_col = _fcol(df, [
        "سعر المنتج", "السعر", "سعر", "Price", "price", "PRICE",
        "سعر_المنتج", "السعر بعد الخصم",
    ])
    sku_col = _fcol(df, [
        "رقم المنتج", "معرف المنتج", "رمز المنتج", "رمز المنتج sku",
        "No.", "no.", "SKU", "sku", "product_id", "ID", "id",
        "الكود", "كود", "الباركود", "barcode",
    ])
    img_col = _fcol(df, [
        "صورة المنتج", "صوره المنتج", "image", "Image", "product_image", "الصورة",
        "thumbnail", "photo",
    ])
    url_col = _fcol(df, [
        "رابط المنتج", "الرابط", "رابط", "product_url", "link", "url", "URL",
    ])
    return {
        "name": name_col,
        "price": price_col,
        "sku": sku_col,
        "image": img_col,
        "url": url_col,
    }


# ─── قراءة الملف ─────────────────────────────────────────────────────────────

def _try_second_row_header(peek: pd.DataFrame) -> bool:
    """ملفات سلة: الصف 0 مجموعات، الصف 1 عناوين الحقول الحقيقية."""
    if peek is None or len(peek) < 2:
        return False
    row1 = [str(x).strip().lower() for x in peek.iloc[1].tolist()]
    keys = ("اسم المنتج", "سعر المنتج", "no.", "sku", "product", "name", "price",
            "صورة المنتج", "رابط المنتج", "رمز المنتج", "الماركة")
    hits = sum(1 for x in row1 if any(k in x for k in keys))
    return hits >= 3


def read_file(file_obj) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """قراءة ملف CSV/Excel مع كشف تلقائي للترميز والصف الرأسي."""
    try:
        name = file_obj.name.lower() if hasattr(file_obj, "name") else str(file_obj).lower()
        df = None

        if name.endswith(".csv"):
            for enc in ["utf-8-sig", "utf-8", "windows-1256", "cp1256", "latin-1"]:
                try:
                    file_obj.seek(0)
                    peek = pd.read_csv(file_obj, header=None, nrows=6, encoding=enc, on_bad_lines="skip")
                    file_obj.seek(0)
                    skip = 1 if _try_second_row_header(peek) else 0
                    file_obj.seek(0)
                    df = pd.read_csv(file_obj, header=skip, encoding=enc, on_bad_lines="skip")
                    if len(df) > 0:
                        break
                except Exception:
                    continue

        elif name.endswith((".xlsx", ".xls")):
            file_obj.seek(0)
            peek = pd.read_excel(file_obj, header=None, nrows=4)
            file_obj.seek(0)
            skip = 1 if _try_second_row_header(peek) else 0
            df = pd.read_excel(file_obj, header=skip)

        else:
            return None, "صيغة غير مدعومة — يُرجى استخدام CSV أو XLSX"

        if df is None or df.empty:
            return None, "الملف فارغ أو لم يمكن قراءته"

        # تنظيف أسماء الأعمدة
        df.columns = df.columns.map(lambda x: str(x).strip().replace("\ufeff", ""))
        df = df.dropna(how="all").reset_index(drop=True)
        # Phase 4: downcast large DataFrames
        if len(df) > 500:
            try:
                from utils.data_helpers import optimize_dataframe_memory
                df = optimize_dataframe_memory(df)
            except ImportError:
                pass
        return df, None

    except Exception as e:
        return None, f"خطأ في قراءة الملف: {e}"


# ─── فهرس كتالوج المتجر ──────────────────────────────────────────────────────

@dataclass
class StoreIndex:
    """فهرس مبني مسبقاً من كتالوج المتجر لبحث سريع."""
    products: List[NormProduct]
    _bare_names: List[str] = field(default_factory=list, init=False)
    _norm_names: List[str] = field(default_factory=list, init=False)
    _brands: List[str] = field(default_factory=list, init=False)

    def __post_init__(self):
        self._bare_names = [p.name_bare for p in self.products]
        self._norm_names = [p.name_norm for p in self.products]
        self._brands = [p.brand for p in self.products]

    def search(self, query: "NormProduct", top_n: int = 5) -> List[Dict[str, Any]]:
        """ابحث عن أفضل المرشحين لمنتج المنافس في كتالوجنا."""
        results = []

        if not _HAS_RAPIDFUZZ:
            # Fallback: مطابقة بسيطة
            for i, p in enumerate(self.products):
                q = query.name_bare
                s = score_names(q, p.name_bare)
                results.append({"store_product": p, "score": s})
            results.sort(key=lambda x: -x["score"])
            return results[:top_n]

        # فلترة مبكرة: إذا عرفنا الماركة، نبحث أولاً بين نفس الماركة
        brand_filtered = self.products
        brand_indices = list(range(len(self.products)))
        if query.brand:
            same_brand = [
                (i, p) for i, p in enumerate(self.products)
                if p.brand == query.brand
                or fuzz.ratio(normalize(query.brand), normalize(p.brand)) >= 88
            ]
            if len(same_brand) >= 1:
                brand_filtered = [x[1] for x in same_brand]
                brand_indices = [x[0] for x in same_brand]
            # أضف جميع المنتجات كاحتياطي إذا كانت النتائج أقل من 3
            if len(same_brand) < 3:
                brand_filtered = self.products
                brand_indices = list(range(len(self.products)))

        # بحث vectorized بـ RapidFuzz
        bare_names_filtered = [brand_filtered[j].name_bare for j in range(len(brand_filtered))]
        matches = rf_process.extract(
            query.name_bare,
            bare_names_filtered,
            scorer=fuzz.WRatio,
            limit=top_n * 2,
            score_cutoff=40,
        )

        seen = set()
        for match_text, score, local_idx in matches:
            global_idx = brand_indices[local_idx] if local_idx < len(brand_indices) else local_idx
            if global_idx in seen:
                continue
            seen.add(global_idx)
            p = self.products[global_idx]
            results.append({"store_product": p, "score": float(score)})

        results.sort(key=lambda x: -x["score"])
        return results[:top_n]


# ─── منطق المطابقة والتصنيف ──────────────────────────────────────────────────

def _check_critical_attributes(comp: NormProduct, store: NormProduct) -> Tuple[bool, str]:
    """
    فحص العوامل الحاسمة التي تمنع المطابقة.
    يُرجع (مسموح_بالمطابقة, سبب_الرفض).
    """
    # تستر vs منتج عادي
    if comp.is_tester_flag != store.is_tester_flag:
        return False, "اختلاف حالة التستر"

    # عينة vs تجاري
    if comp.is_sample_flag != store.is_sample_flag:
        return False, "اختلاف نوع العينة/التجاري"

    # طقم vs فردي
    if comp.is_set_flag != store.is_set_flag:
        return False, "اختلاف بين طقم ومنتج فردي"

    # اختلاف التركيز الحاسم (EDP vs EDT vs EXTRAIT)
    if comp.concentration and store.concentration:
        if comp.concentration != store.concentration:
            # INTENSE بعض الأحيان يُباع كـ EDP — نتسامح
            if not ({"INTENSE","EDP"} >= {comp.concentration, store.concentration}):
                return False, f"اختلاف التركيز ({comp.concentration} vs {store.concentration})"

    # اختلاف الحجم الحاسم (فقط إذا كلاهما معروف)
    if comp.size_ml > 0 and store.size_ml > 0:
        diff_ratio = abs(comp.size_ml - store.size_ml) / max(comp.size_ml, store.size_ml)
        if diff_ratio > 0.15:  # اختلاف > 15% → مختلف
            return False, f"اختلاف الحجم ({comp.size_ml:.0f}ml vs {store.size_ml:.0f}ml)"

    # تعارض الإصدار (flanker)
    if flanker_conflict(comp.name_raw, store.name_raw):
        return False, "اختلاف إصدار العطر (flanker)"

    return True, ""


def _check_soft_attributes(comp: NormProduct, store: NormProduct) -> List[str]:
    """
    العوامل اللينة التي تُرسل إلى المراجعة لكن لا تمنع المطابقة.
    """
    issues = []
    if comp.gender and store.gender and comp.gender != store.gender:
        issues.append(f"اختلاف الجنس ({comp.gender} vs {store.gender})")
    return issues


def classify_match(
    comp: NormProduct,
    store: NormProduct,
    base_score: float,
) -> Dict[str, Any]:
    """
    تصنيف المطابقة بين منتج المنافس ومنتج مخزننا.
    يُرجع قاموساً بالقرار والأسباب والدرجة.
    """
    # فحص العوامل الحاسمة
    ok, critical_reason = _check_critical_attributes(comp, store)
    if not ok:
        return {
            "decision": "REJECT",
            "score": base_score,
            "reason": critical_reason,
            "issues": [critical_reason],
        }

    # فحص العوامل اللينة
    soft_issues = _check_soft_attributes(comp, store)

    # حساب درجة مركبة
    bare_score = score_names(comp.name_bare, store.name_bare)
    pl_score = score_names(comp.product_line, store.product_line) if comp.product_line and store.product_line else base_score
    composite = round(base_score * 0.3 + bare_score * 0.45 + pl_score * 0.25, 2)

    if composite >= CONFIRMED_SCORE and not soft_issues:
        return {
            "decision": "CONFIRMED_MATCH",
            "score": composite,
            "reason": "تطابق عالي الثقة",
            "issues": [],
        }
    elif composite >= CONFIRMED_SCORE and soft_issues:
        return {
            "decision": "UNDER_REVIEW",
            "score": composite,
            "reason": "تطابق قوي مع مؤشرات تحتاج مراجعة: " + " | ".join(soft_issues),
            "issues": soft_issues,
        }
    elif composite >= REVIEW_SCORE:
        reason = "تشابه معقول يحتاج تأكيداً"
        if soft_issues:
            reason += ": " + " | ".join(soft_issues)
        return {
            "decision": "UNDER_REVIEW",
            "score": composite,
            "reason": reason,
            "issues": soft_issues,
        }
    else:
        return {
            "decision": "REJECT",
            "score": composite,
            "reason": "تشابه غير كافٍ بعد جميع طبقات التحقق",
            "issues": [],
        }


# ─── نتيجة التحليل ──────────────────────────────────────────────────────────

@dataclass
class ChallengeResult:
    confirmed_matches: pd.DataFrame
    under_review: pd.DataFrame
    confirmed_missing: pd.DataFrame
    acquisition_opportunities: pd.DataFrame
    audit_log: pd.DataFrame
    stats: Dict[str, Any]


# ─── المحرك الرئيسي ──────────────────────────────────────────────────────────

def _build_store_index(
    store_df: pd.DataFrame,
    cols: Dict[str, Optional[str]],
) -> StoreIndex:
    """بناء فهرس كتالوج المتجر."""
    products = []
    name_col = cols["name"]
    if not name_col:
        return StoreIndex(products=[])
    for i, (_, row) in enumerate(store_df.iterrows()):
        raw = _safe_str(row.get(name_col, ""))
        if not raw or raw.lower() in ("nan", "none"):
            continue
        p = NormProduct.from_row(
            row=row,
            name_col=name_col,
            price_col=cols.get("price"),
            sku_col=cols.get("sku"),
            img_col=cols.get("image"),
            url_col=cols.get("url"),
            idx=i,
        )
        if p.product_type in ("retail", "tester"):
            products.append(p)
    return StoreIndex(products=products)


def _make_output_row(
    comp: NormProduct,
    store: Optional[NormProduct],
    decision: str,
    score: float,
    reason: str,
    issues: List[str],
    price_alert: str,
) -> Dict[str, Any]:
    """بناء صف النتيجة بتنسيق موحد."""
    comp_price = comp.price
    store_price = store.price if store else 0.0
    price_diff = round(comp_price - store_price, 2) if store else 0.0
    price_diff_pct = round(abs(price_diff) / store_price * 100, 1) if store and store_price > 0 else 0.0

    return {
        # منتج المنافس
        "اسم_منتج_المنافس": comp.name_raw,
        "ماركة_المنافس": comp.brand or "—",
        "حجم_المنافس_مل": int(comp.size_ml) if comp.size_ml else "—",
        "تركيز_المنافس": comp.concentration or "—",
        "جنس_المنافس": comp.gender or "—",
        "سعر_المنافس": comp_price if comp_price else "—",
        "نوع_منتج_المنافس": comp.product_type,
        "رمز_المنافس": comp.sku or "—",
        "صورة_المنافس": comp.image_url or "—",
        "رابط_المنافس": comp.product_url or "—",
        "اسم_المنافس": comp.competitor_name,
        # منتج متجرنا
        "اسم_منتجنا": store.name_raw if store else "—",
        "ماركة_متجرنا": store.brand if store else "—",
        "سعر_متجرنا": store.price if store else "—",
        "حجم_متجرنا_مل": int(store.size_ml) if store and store.size_ml else "—",
        "تركيز_متجرنا": store.concentration if store else "—",
        "رمز_منتجنا": store.sku if store else "—",
        "صورة_منتجنا": store.image_url if store else "—",
        "رابط_منتجنا": store.product_url if store else "—",
        # نتيجة المطابقة
        "التصنيف": decision,
        "نسبة_التشابه": f"{score:.1f}%",
        "سبب_القرار": reason,
        "ملاحظات_إضافية": " | ".join(issues) if issues else "—",
        "تنبيه_السعر": price_alert or "—",
        "فرق_السعر": price_diff if store else "—",
        "نسبة_فرق_السعر": f"{price_diff_pct:.1f}%" if store else "—",
        "تاريخ_التحليل": datetime.now().strftime("%Y-%m-%d"),
    }


def run_challenge_analysis(
    store_df: pd.DataFrame,
    competitor_dfs: Dict[str, pd.DataFrame],
    store_col_map: Optional[Dict] = None,
    progress_callback=None,
) -> ChallengeResult:
    """
    المحرك الرئيسي — يُحلل منتجات المنافسين ضد كتالوج المتجر.

    Args:
        store_df: DataFrame لكتالوج المتجر الأساسي
        competitor_dfs: قاموس {اسم_المنافس: DataFrame}
        store_col_map: خريطة أعمدة اختيارية للمتجر (يُكتشف تلقائياً إن لم يُعطَ)
        progress_callback: دالة (نسبة_مئوية, رسالة) للتحديث

    Returns:
        ChallengeResult مع 5 DataFrames + إحصاءات
    """
    # ── بناء فهرس المتجر ──────────────────────────────────────────────────
    if store_col_map is None:
        store_col_map = detect_columns(store_df)

    store_index = _build_store_index(store_df, store_col_map)
    total_store = len(store_index.products)

    if total_store == 0:
        empty = pd.DataFrame()
        return ChallengeResult(
            confirmed_matches=empty,
            under_review=empty,
            confirmed_missing=empty,
            acquisition_opportunities=empty,
            audit_log=empty,
            stats={"error": "لم يتم العثور على منتجات في كتالوج المتجر"},
        )

    # ── جمع كل منتجات المنافسين (مع تحرير ذاكرة كل DataFrame بعد المعالجة) ──
    all_comp_products: List[NormProduct] = []
    for comp_name, comp_df in competitor_dfs.items():
        comp_cols = detect_columns(comp_df)
        name_col = comp_cols["name"]
        if not name_col:
            continue
        # Phase 4: iterate without holding full DataFrame in scope
        _rows_data = comp_df.to_dict("records")  # lighter than iterrows
        for i, row in enumerate(_rows_data):
            raw = _safe_str(row.get(name_col, ""))
            if not raw:
                continue
            p = NormProduct.from_row(
                row=row,
                name_col=name_col,
                price_col=comp_cols.get("price"),
                sku_col=comp_cols.get("sku"),
                img_col=comp_cols.get("image"),
                url_col=comp_cols.get("url"),
                idx=i,
                competitor=comp_name,
            )
            all_comp_products.append(p)
        del _rows_data  # Phase 4: free dict copy immediately

    total_comp = len(all_comp_products)
    if total_comp == 0:
        empty = pd.DataFrame()
        return ChallengeResult(
            confirmed_matches=empty, under_review=empty,
            confirmed_missing=empty, acquisition_opportunities=empty,
            audit_log=empty,
            stats={"error": "لم يتم العثور على منتجات في ملفات المنافسين"},
        )

    # ── المعالجة ──────────────────────────────────────────────────────────
    confirmed_rows, review_rows, missing_rows, audit_rows = [], [], [], []

    stats = {
        "total_store": total_store,
        "total_competitor": total_comp,
        "confirmed_match": 0,
        "under_review": 0,
        "confirmed_missing": 0,
        "excluded_samples": 0,
        "excluded_sets": 0,
        "acquisition_opportunities": 0,
        "price_alerts": 0,
    }

    for idx, comp_prod in enumerate(all_comp_products):
        if progress_callback:
            pct = (idx + 1) / total_comp
            progress_callback(pct, f"معالجة {idx+1}/{total_comp}: {comp_prod.name_raw[:40]}")

        # ── عينات صغيرة جداً → مستبعدة (لا تُضاف للمفقودات) ──────────────
        if comp_prod.is_sample_flag and comp_prod.size_ml <= MIN_SAMPLE_ML:
            stats["excluded_samples"] += 1
            audit_rows.append({
                "اسم_المنتج": comp_prod.name_raw,
                "المنافس": comp_prod.competitor_name,
                "القرار": "⚪ مستبعد",
                "السبب": "عينة صغيرة — لا تُعتبر فرصة استحواذ",
                "نسبة_التشابه": "—",
            })
            continue

        # ── بحث عن أفضل مرشح ───────────────────────────────────────────
        candidates = store_index.search(comp_prod, top_n=5)

        # لا يوجد أي مرشح → مفقود مباشرة
        if not candidates:
            decision_label = "🔍 مفقود مؤكد"
            reason = "لا يوجد منتج مشابه في الكتالوج"
            row = _make_output_row(comp_prod, None, decision_label, 0.0, reason, [], "")
            missing_rows.append(row)
            stats["confirmed_missing"] += 1
            audit_rows.append({
                "اسم_المنتج": comp_prod.name_raw,
                "المنافس": comp_prod.competitor_name,
                "القرار": decision_label,
                "السبب": reason,
                "نسبة_التشابه": "0%",
            })
            continue

        # ── تقييم المرشح الأول ─────────────────────────────────────────
        best = candidates[0]
        best_store = best["store_product"]
        base_score = best["score"]

        result = classify_match(comp_prod, best_store, base_score)

        # ── إذا رُفض المرشح الأول، جرّب التالين ───────────────────────
        if result["decision"] == "REJECT" and len(candidates) > 1:
            for cand in candidates[1:]:
                alt_store = cand["store_product"]
                alt_score = cand["score"]
                alt_result = classify_match(comp_prod, alt_store, alt_score)
                if alt_result["decision"] in ("CONFIRMED_MATCH", "UNDER_REVIEW"):
                    result = alt_result
                    best_store = alt_store
                    break

        # ── تنبيه السعر ────────────────────────────────────────────────
        price_alert = ""
        if result["decision"] == "CONFIRMED_MATCH" and best_store.price > 0 and comp_prod.price > 0:
            diff_pct = abs(comp_prod.price - best_store.price) / best_store.price * 100
            if diff_pct > PRICE_ALERT_PCT:
                direction = "أعلى" if comp_prod.price > best_store.price else "أقل"
                price_alert = f"⚠️ سعر المنافس {direction} بـ {diff_pct:.1f}%"
                stats["price_alerts"] += 1

        # ── التصنيف النهائي ─────────────────────────────────────────────
        if result["decision"] == "CONFIRMED_MATCH":
            decision_label = "✅ مطابق مؤكد"
            row = _make_output_row(
                comp_prod, best_store, decision_label,
                result["score"], result["reason"], result["issues"], price_alert
            )
            confirmed_rows.append(row)
            stats["confirmed_match"] += 1

        elif result["decision"] == "UNDER_REVIEW":
            decision_label = "⚠️ تحت المراجعة"
            row = _make_output_row(
                comp_prod, best_store, decision_label,
                result["score"], result["reason"], result["issues"], price_alert
            )
            review_rows.append(row)
            stats["under_review"] += 1

        else:
            # REJECT → إذا كان score ≥ REVIEW_SCORE إرسله للمراجعة أيضاً (محافظ)
            if result["score"] >= REVIEW_SCORE:
                decision_label = "⚠️ تحت المراجعة"
                row = _make_output_row(
                    comp_prod, best_store if candidates else None,
                    decision_label, result["score"],
                    "تشابه جزئي — أُرسل للمراجعة بدلاً من الحذف",
                    result["issues"], ""
                )
                review_rows.append(row)
                stats["under_review"] += 1
            else:
                decision_label = "🔍 مفقود مؤكد"
                row = _make_output_row(
                    comp_prod, None, decision_label,
                    result["score"], result["reason"], result["issues"], ""
                )
                missing_rows.append(row)
                stats["confirmed_missing"] += 1

        audit_rows.append({
            "اسم_المنتج": comp_prod.name_raw,
            "المنافس": comp_prod.competitor_name,
            "القرار": decision_label,
            "السبب": result["reason"],
            "نسبة_التشابه": f"{result['score']:.1f}%",
            "أفضل_منتج_مطابق": best_store.name_raw if candidates else "—",
        })

    # ── استخراج فرص الاستحواذ من المفقودات ──────────────────────────────
    opp_rows = []
    for r in missing_rows:
        ptype = r.get("نوع_منتج_المنافس", "")
        size = r.get("حجم_المنافس_مل", 0)
        name = r.get("اسم_منتج_المنافس", "")
        if (
            ptype in ("retail",)
            and not is_sample(name)
            and not is_tester(name)
            and not is_set(name)
            and (isinstance(size, (int, float)) and size >= MIN_RETAIL_ML or size == "—")
        ):
            opp_rows.append(r)
    stats["acquisition_opportunities"] = len(opp_rows)

    # ── بناء DataFrames ───────────────────────────────────────────────────
    def _to_df(rows):
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    confirmed_df = _to_df(confirmed_rows)
    review_df = _to_df(review_rows)
    missing_df = _to_df(missing_rows)
    opp_df = _to_df(opp_rows)

    audit_df = pd.DataFrame(audit_rows) if audit_rows else pd.DataFrame()

    # Phase 4: تنظيف شامل للذاكرة
    del all_comp_products, confirmed_rows, review_rows, missing_rows, opp_rows, audit_rows
    gc.collect()

    return ChallengeResult(
        confirmed_matches=confirmed_df,
        under_review=review_df,
        confirmed_missing=missing_df,
        acquisition_opportunities=opp_df,
        audit_log=audit_df,
        stats=stats,
    )


# ─── تصدير Excel ─────────────────────────────────────────────────────────────

def export_to_excel_bytes(df: pd.DataFrame, sheet_name: str = "النتائج") -> bytes:
    """تصدير DataFrame إلى bytes من Excel."""
    if df.empty:
        df = pd.DataFrame({"ملاحظة": ["لا توجد بيانات"]})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    buf.seek(0)
    return buf.read()


def export_all_to_excel_bytes(result: ChallengeResult) -> bytes:
    """تصدير جميع النتائج في ملف Excel واحد متعدد الأوراق."""
    buf = io.BytesIO()
    sheets = [
        (result.confirmed_matches, "✅ مطابقات مؤكدة"),
        (result.under_review, "⚠️ تحت المراجعة"),
        (result.confirmed_missing, "🔍 مفقودات مؤكدة"),
        (result.acquisition_opportunities, "🛒 فرص الاستحواذ"),
        (result.audit_log, "📋 سجل التدقيق"),
    ]
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for df, name in sheets:
            safe_name = name[:31]
            if df is None or df.empty:
                pd.DataFrame({"ملاحظة": ["لا توجد بيانات"]}).to_excel(
                    writer, index=False, sheet_name=safe_name
                )
            else:
                df.to_excel(writer, index=False, sheet_name=safe_name)
    buf.seek(0)
    return buf.read()
