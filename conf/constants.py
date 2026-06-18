"""config/constants.py — الثوابت الموحّدة (المصدر الوحيد للحقيقة).

يجمع كل القيم الثابتة المنقولة حرفياً من ``app.py`` و``config.py``:
أسماء الأعمدة، العتبات، قواعد التصنيف، كلمات الحجب/الاستبعاد، الماركات.

⚠️ قاعدة صارمة: أسماء الأعمدة هنا يعتمدها تصدير Make.com وSalla —
ممنوع تغييرها. أي تعديل يكسر التكامل الخارجي.
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

from core.enums import SectionType

# ════════════════════════════════════════════════════════════════════
#  المسارات (تُحسب نسبةً لجذر المشروع: <root>/mahwous-pricing-v2/config/)
# ════════════════════════════════════════════════════════════════════
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"
DEFAULT_DB_PATH: Final[Path] = DATA_DIR / "perfume_pricing.db"

# ════════════════════════════════════════════════════════════════════
#  أسماء الأعمدة (#PRESERVED_LOGIC — من app.py، حرفية لا تُغيَّر)
# ════════════════════════════════════════════════════════════════════
COL_OUR_NAME: Final[str] = "المنتج"
COL_OUR_ID: Final[str] = "معرف_المنتج"
COL_OUR_PRICE: Final[str] = "السعر"
COL_BRAND: Final[str] = "الماركة"
COL_SIZE: Final[str] = "الحجم"
COL_TYPE: Final[str] = "النوع"
COL_GENDER: Final[str] = "الجنس"
COL_DECISION: Final[str] = "القرار"
COL_CONFIDENCE: Final[str] = "مستوى_الثقة"
COL_MATCH_RATIO: Final[str] = "نسبة_التطابق"
COL_DIFF: Final[str] = "الفرق"
COL_SUGGESTED_PRICE: Final[str] = "السعر_المقترح"
COL_REASON: Final[str] = "السبب"
COL_COMP_NAME: Final[str] = "منتج_المنافس"
COL_COMP_STORE: Final[str] = "المنافس"
COL_COMP_PRICE: Final[str] = "سعر_المنافس"
COL_COMP_ID: Final[str] = "معرف_المنافس"
COL_COMP_IMAGE: Final[str] = "صورة_المنافس"
COL_COMP_LINK: Final[str] = "رابط_المنافس"
COL_COMP_DETAILS: Final[str] = "تفاصيل_المنافسين"
COL_COMP_COUNT: Final[str] = "عدد_المنافسين"

# ════════════════════════════════════════════════════════════════════
#  العتبات (#PRESERVED_LOGIC — config.py:118-129 و app.py:745/894/895)
# ════════════════════════════════════════════════════════════════════
MATCH_CONFIRMED_THRESHOLD: Final[int] = 82   # «نملكه» → إخفاء (0 إيجابيات كاذبة)
MATCH_REVIEW_THRESHOLD: Final[int] = 65      # «محتمل موجود» → يبقى للمراجعة
SIZE_TOLERANCE_ML: Final[float] = 8.0        # تسامح فرق الحجم لاعتبار حجمين متطابقين
MATCH_THRESHOLD: Final[int] = 85             # config.MATCH_THRESHOLD
HIGH_CONFIDENCE: Final[int] = 95
REVIEW_THRESHOLD: Final[int] = 75
PRICE_TOLERANCE: Final[int] = 5

# عتبات فلترة المفقودات (#PRESERVED_LOGIC — قيود _compute_missing_from_store)
MISSING_MIN_PRICE: Final[float] = 20.0
MISSING_MAX_PRICE: Final[float] = 15000.0
MISSING_MIN_NAME_LEN: Final[int] = 8
MISSING_MIN_SIZE_ML: Final[float] = 10.0
MISSING_CACHE_VERSION: Final[str] = "F4v2"   # توقيع الكاش: F4v2|catalog_len|db_size

# ════════════════════════════════════════════════════════════════════
#  قواعد التصنيف (#PRESERVED_LOGIC — _split_results، app.py:472-489)
#  الترتيب مهم: تُطبَّق القواعد بالتسلسل، والمنتج غير المُوزَّع → EXCLUDED.
# ════════════════════════════════════════════════════════════════════
SECTION_PREFIX_RULES: Final[tuple[tuple[SectionType, tuple[str, ...]], ...]] = (
    (SectionType.PRICE_RAISE, ("🔴",)),
    (SectionType.PRICE_LOWER, ("🟢",)),
    (SectionType.APPROVED, ("✅",)),
    (SectionType.REVIEW, ("⚠️", "🔍")),
    (SectionType.EXCLUDED, ("⚪",)),
)
PRICE_LOWER_CONTAINS: Final[str] = "سعر أقل"   # شرط إضافي لقسم «سعر أقل»
ORPHAN_SECTION: Final[SectionType] = SectionType.EXCLUDED  # شبكة الأمان

# ════════════════════════════════════════════════════════════════════
#  تطبيع المطابقة (#PRESERVED_LOGIC — app.py:618-642)
# ════════════════════════════════════════════════════════════════════
# كلمات تُسقَط عند بناء الاسم المجرّد (_miss_bare).
MISS_STOPWORDS: Final[frozenset[str]] = frozenset(
    "عطر عينه عينة تستر سامبل ماء او دو دي بارفيوم برفيوم بارفان تواليت توالت "
    "كولونيا كولن مل غرام للرجال للنساء رجالي نسائي".split()
)
# الحروف العربية الضعيفة المتغيّرة إملائياً (تُزال في الهيكل العظمي للحجب).
AR_WEAK_CHARS: Final[str] = "اويهءأإآةىؤئ"

# ════════════════════════════════════════════════════════════════════
#  كلمات الاستبعاد والتصنيف (#PRESERVED_LOGIC — config.py:134-141)
# ════════════════════════════════════════════════════════════════════
REJECT_KEYWORDS: Final[tuple[str, ...]] = (
    "sample", "عينة", "عينه", "decant", "تقسيم", "تقسيمة",
    "split", "miniature", "مينياتشر", "0.5ml", "1ml", "2ml", "3ml",
)
TESTER_KEYWORDS: Final[tuple[str, ...]] = ("tester", "تستر", "تيستر")
SET_KEYWORDS: Final[tuple[str, ...]] = (
    "set", "طقم", "مجموعة", "gift set", "coffret", "هدية",
)
# فئات غير عطرية تُسقَط من المفقودات.
NON_PERFUME_KEYWORDS: Final[tuple[str, ...]] = (
    "deodorant", "مزيل", "body mist", "بادي مست", "مست",
    "lotion", "لوشن", "soap", "صابون", "gel", "جل", "شاور",
)

# ════════════════════════════════════════════════════════════════════
#  الألوان والأقسام (#PRESERVED_LOGIC — config.py:117,198)
# ════════════════════════════════════════════════════════════════════
COLORS: Final[dict[str, str]] = {
    "raise": "#dc3545", "lower": "#00C853", "approved": "#28a745",
    "missing": "#007bff", "review": "#ff9800", "excluded": "#9e9e9e",
    "primary": "#6C63FF",
}
SECTION_LABELS: Final[dict[SectionType, str]] = {
    SectionType.PRICE_RAISE: "🔴 سعر أعلى",
    SectionType.PRICE_LOWER: "🟢 سعر أقل",
    SectionType.APPROVED: "✅ موافق عليها",
    SectionType.MISSING: "🔍 منتجات مفقودة",
    SectionType.REVIEW: "⚠️ تحت المراجعة",
    SectionType.EXCLUDED: "⚪ مستبعد (لا يوجد تطابق)",
}
PAGES_PER_TABLE: Final[int] = 25
CARDS_PER_PAGE: Final[int] = 12

# ════════════════════════════════════════════════════════════════════
#  الماركات المعروفة (#PRESERVED_LOGIC — config.py:146-188، منقولة حرفياً)
# ════════════════════════════════════════════════════════════════════
KNOWN_BRANDS: Final[tuple[str, ...]] = (
    "Dior", "Chanel", "Gucci", "Tom Ford", "Versace", "Armani", "YSL", "Prada",
    "Burberry", "Hermes", "Creed", "Montblanc", "Amouage", "Rasasi", "Lattafa",
    "Arabian Oud", "Ajmal", "Al Haramain", "Afnan", "Armaf", "Mancera", "Montale",
    "Kilian", "Jo Malone", "Carolina Herrera", "Paco Rabanne", "Mugler",
    "Ralph Lauren", "Parfums de Marly", "Nishane", "Xerjoff", "Byredo", "Le Labo",
    "Roja", "Narciso Rodriguez", "Dolce & Gabbana", "Valentino", "Bvlgari",
    "Cartier", "Hugo Boss", "Calvin Klein", "Givenchy", "Lancome", "Guerlain",
    "Jean Paul Gaultier", "Issey Miyake", "Davidoff", "Coach", "Michael Kors",
    "Initio", "Memo Paris", "Maison Margiela", "Diptyque", "Missoni",
    "Juicy Couture", "Moschino", "Dunhill", "Bentley", "Jaguar", "Boucheron",
    "Chopard", "Elie Saab", "Escada", "Ferragamo", "Fendi", "Kenzo", "Lacoste",
    "Loewe", "Rochas", "Roberto Cavalli", "Tiffany", "Van Cleef", "Azzaro",
    "Chloe", "Elizabeth Arden", "Swiss Arabian", "Penhaligons", "Clive Christian",
    "Floris", "Acqua di Parma", "Ard Al Zaafaran", "Nabeel", "Asdaaf",
    "Maison Alhambra", "Tiziana Terenzi", "Maison Francis Kurkdjian",
    "Serge Lutens", "Frederic Malle", "Ormonde Jayne", "Zoologist", "Tauer",
    "Banana Republic", "Benetton", "Bottega Veneta", "Celine", "Dsquared2",
    "Ermenegildo Zegna", "Sisley", "Mexx", "Amadou", "Thameen", "Nasomatto",
    "Nicolai", "Replica", "Atelier Cologne", "Aerin", "Angel Schlesser",
    "Annick Goutal", "Antonio Banderas", "Balenciaga", "Bond No 9", "Boadicea",
    "Carner Barcelona", "Clean", "Commodity", "Costume National", "Derek Lam",
    "Diptique", "Estee Lauder", "Franck Olivier", "Giorgio Beverly Hills",
    "Guess", "Histoires de Parfums", "Illuminum", "Jimmy Choo", "Kenneth Cole",
    "Lalique", "Lolita Lempicka", "Lubin", "Miu Miu", "Moresque", "Nobile 1942",
    "Oscar de la Renta", "Oud Elite", "Philipp Plein", "Police", "Reminiscence",
    "Salvatore Ferragamo", "Stella McCartney", "Ted Lapidus", "Ungaro",
    "Vera Wang", "Viktor Rolf", "Zadig Voltaire", "Zegna", "Ajwad",
    "Club de Nuit", "Milestone",
    # ── ماركات بالعربي ──
    "لطافة", "العربية للعود", "رصاسي", "أجمل", "الحرمين", "أرماف", "أمواج",
    "كريد", "توم فورد", "ديور", "شانيل", "غوتشي", "برادا", "ميسوني", "جوسي كوتور",
    "موسكينو", "دانهيل", "بنتلي", "كينزو", "لاكوست", "فندي", "ايلي صعب", "ازارو",
    "كيليان", "نيشان", "زيرجوف", "بنهاليغونز", "مارلي", "جيرلان", "تيزيانا ترينزي",
    "مايزون فرانسيس", "بايريدو", "لي لابو", "مانسيرا", "مونتالي", "روجا",
    "جو مالون", "ثمين", "أمادو", "ناسوماتو", "ميزون مارجيلا", "نيكولاي",
    "جيمي تشو", "لاليك", "بوليس", "فيكتور رولف", "كلوي", "بالنسياغا", "ميو ميو",
)
