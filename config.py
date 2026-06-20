"""
config.py - الإعدادات المركزية v30.0 (Manus Optimized + Full Constants)
المفاتيح: أولاً os.environ (Railway / Docker)، ثم Streamlit Secrets عند التوفر.
"""
import json as _json
import os as _os

# ── تحميل .env تلقائياً ──────────────────────────────────────────────────
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)
except ImportError:
    # تحميل يدوي بدون مكتبة خارجية
    from pathlib import Path as _Path
    _env_file = _Path(__file__).parent / ".env"
    if _env_file.exists():
        try:
            with open(_env_file, encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#"):
                        continue
                    if "=" in _line:
                        _k, _, _v = _line.partition("=")
                        _k = _k.strip()
                        _v = _v.strip().strip('"').strip("'")
                        if _k and not _k.startswith("#"):
                            _os.environ.setdefault(_k, _v)
        except Exception:
            pass

from utils.data_paths import get_data_db_path

# ===== معلومات التطبيق =====
APP_TITLE   = "نظام التسعير الذكي - مهووس"
APP_NAME    = APP_TITLE
APP_VERSION = "v30.0"
APP_ICON    = "🧪"

# ═══════════════════════════════════════════════════
#  نماذج Gemini — اختيار ذكي لكل مهمة
# ═══════════════════════════════════════════════════
GEMINI_MODEL       = "gemini-2.5-flash"    # النموذج السريع للمطابقة والتحليل
GEMINI_MODEL_DEEP  = "gemini-2.5-pro"      # النموذج العميق للأوصاف والتحليل المعقد

# ══════════════════════════════════════════════════
#  قراءة Secrets بطريقة آمنة 100%
# ══════════════════════════════════════════════════
def _s(key, default=""):
    v = _os.environ.get(key, "")
    if v:
        return v
    try:
        import streamlit as st
        v = st.secrets[key]
        if v is not None:
            return str(v) if not isinstance(v, (list, dict)) else v
    except Exception:
        pass
    return default


def _parse_gemini_keys():
    keys = []
    raw = _s("GEMINI_API_KEYS", "")
    if isinstance(raw, list):
        keys = [k for k in raw if k and isinstance(k, str)]
    elif raw and isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith('['):
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    keys = [k for k in parsed if k]
            except Exception:
                clean = raw.strip("[]").replace('"','').replace("'",'')
                keys = [k.strip() for k in clean.split(',') if k.strip()]
        elif raw:
            keys = [raw]
    single = _s("GEMINI_API_KEY", "")
    if single and single not in keys:
        keys.append(single)
    # يدعم صيغتي الترقيم حتى 50 مفتاحاً: GEMINI_KEY_N و GEMINI_API_KEY_N (تدوير المفاتيح)
    for i in range(1, 51):
        for n in (f"GEMINI_API_KEY_{i}", f"GEMINI_KEY_{i}"):
            k = _s(n, "")
            if k and k not in keys:
                keys.append(k)
    keys = [k.strip() for k in keys if k and len(k) > 20]
    return keys

# ══════════════════════════════════════════════════
#  المفاتيح الفعلية
# ══════════════════════════════════════════════════
GEMINI_API_KEYS    = _parse_gemini_keys()
GEMINI_API_KEY     = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else ""
OPENROUTER_API_KEY = _s("OPENROUTER_API_KEY") or _s("OPENROUTER_KEY") or ""
COHERE_API_KEY     = _s("COHERE_API_KEY") or ""
EXTRA_API_KEY      = _s("EXTRA_API_KEY")

def any_ai_provider_configured() -> bool:
    if GEMINI_API_KEYS or (OPENROUTER_API_KEY or "").strip() or (COHERE_API_KEY or "").strip():
        return True
    return False

ANY_AI_PROVIDER_CONFIGURED = any_ai_provider_configured()

# ══════════════════════════════════════════════════
#  Make Webhooks
# ══════════════════════════════════════════════════
WEBHOOK_UPDATE_PRICES = _s("WEBHOOK_UPDATE_PRICES") or _os.environ.get("MAKE_WEBHOOK_URL", "")
WEBHOOK_NEW_PRODUCTS = _s("WEBHOOK_NEW_PRODUCTS") or _os.environ.get("MAKE_WEBHOOK_URL_2", "")

# ══════════════════════════════════════════════════
#  إعدادات الألوان والمطابقة
# ══════════════════════════════════════════════════
COLORS = {"raise": "#dc3545", "lower": "#00C853", "approved": "#28a745", "missing": "#007bff", "review": "#ff9800", "excluded": "#9e9e9e", "primary": "#6C63FF"}
MATCH_THRESHOLD = 85
HIGH_CONFIDENCE = 95
REVIEW_THRESHOLD = 75
PRICE_TOLERANCE = 5

# عتبات كشف المنتجات المفقودة (المصدر الوحيد — موصولة بالمسار الحيّ في
# app.py::_compute_missing_from_store). القيم مضبوطة يدوياً بتحقّق عيّنة:
#   CONFIRMED=82 «نملكه» (إخفاء) · REVIEW=65 «محتمل موجود» (يبقى ظاهراً للمراجعة).
# M1: صُحّحت REVIEW 70→65 لتطابق القيمة الحيّة المُتحقَّقة قبل الوصل (لا تغيير سلوك).
MISSING_CONFIRMED_THRESHOLD = 82
MISSING_REVIEW_THRESHOLD = 65
MISSING_BARRIER_THRESHOLD = 85  # غير مستخدم حالياً (لا مفهوم حاجز في المسار الحيّ)

# ══════════════════════════════════════════════════
#  كلمات الاستبعاد والتصنيف
# ══════════════════════════════════════════════════
REJECT_KEYWORDS = [
    "sample", "عينة", "عينه", "decant", "تقسيم", "تقسيمة",
    "split", "miniature", "مينياتشر", "0.5ml", "1ml", "2ml", "3ml",
]

TESTER_KEYWORDS = ["tester", "تستر", "تيستر"]

SET_KEYWORDS = ["set", "طقم", "مجموعة", "gift set", "coffret", "هدية"]

# ══════════════════════════════════════════════════
#  قائمة الماركات المعروفة (عربي + إنجليزي)
# ══════════════════════════════════════════════════
KNOWN_BRANDS = [
    # ── ماركات عالمية ──
    "Dior","Chanel","Gucci","Tom Ford","Versace","Armani","YSL","Prada","Burberry",
    "Hermes","Creed","Montblanc","Amouage","Rasasi","Lattafa","Arabian Oud","Ajmal",
    "Al Haramain","Afnan","Armaf","Mancera","Montale","Kilian","Jo Malone",
    "Carolina Herrera","Paco Rabanne","Mugler","Ralph Lauren","Parfums de Marly",
    "Nishane","Xerjoff","Byredo","Le Labo","Roja","Narciso Rodriguez",
    "Dolce & Gabbana","Valentino","Bvlgari","Cartier","Hugo Boss","Calvin Klein",
    "Givenchy","Lancome","Guerlain","Jean Paul Gaultier","Issey Miyake","Davidoff",
    "Coach","Michael Kors","Initio","Memo Paris","Maison Margiela","Diptyque",
    "Missoni","Juicy Couture","Moschino","Dunhill","Bentley","Jaguar",
    "Boucheron","Chopard","Elie Saab","Escada","Ferragamo","Fendi",
    "Kenzo","Lacoste","Loewe","Rochas","Roberto Cavalli","Tiffany",
    "Van Cleef","Azzaro","Chloe","Elizabeth Arden","Swiss Arabian",
    "Penhaligons","Clive Christian","Floris","Acqua di Parma",
    "Ard Al Zaafaran","Nabeel","Asdaaf","Maison Alhambra",
    "Tiziana Terenzi","Maison Francis Kurkdjian","Serge Lutens",
    "Frederic Malle","Ormonde Jayne","Zoologist","Tauer",
    "Banana Republic","Benetton","Bottega Veneta","Celine","Dsquared2",
    "Ermenegildo Zegna","Sisley","Mexx","Amadou","Thameen",
    "Nasomatto","Nicolai","Replica","Atelier Cologne","Aerin",
    "Angel Schlesser","Annick Goutal","Antonio Banderas","Balenciaga",
    "Bond No 9","Boadicea","Carner Barcelona","Clean","Commodity",
    "Costume National","Derek Lam","Diptique","Estee Lauder",
    "Franck Olivier","Giorgio Beverly Hills","Guess",
    "Histoires de Parfums","Illuminum","Jimmy Choo","Kenneth Cole",
    "Lalique","Lolita Lempicka","Lubin","Miu Miu","Moresque",
    "Nobile 1942","Oscar de la Renta","Oud Elite","Philipp Plein",
    "Police","Reminiscence","Salvatore Ferragamo",
    "Stella McCartney","Ted Lapidus","Ungaro","Vera Wang","Viktor Rolf",
    "Zadig Voltaire","Zegna","Ajwad","Club de Nuit","Milestone",
    # ── ماركات بالعربي ──
    "لطافة","العربية للعود","رصاسي","أجمل","الحرمين","أرماف",
    "أمواج","كريد","توم فورد","ديور","شانيل","غوتشي","برادا",
    "ميسوني","جوسي كوتور","موسكينو","دانهيل","بنتلي",
    "كينزو","لاكوست","فندي","ايلي صعب","ازارو",
    "كيليان","نيشان","زيرجوف","بنهاليغونز","مارلي","جيرلان",
    "تيزيانا ترينزي","مايزون فرانسيس","بايريدو","لي لابو",
    "مانسيرا","مونتالي","روجا","جو مالون","ثمين","أمادو",
    "ناسوماتو","ميزون مارجيلا","نيكولاي",
    "جيمي تشو","لاليك","بوليس","فيكتور رولف",
    "كلوي","بالنسياغا","ميو ميو",
]

# ══════════════════════════════════════════════════
#  استبدالات الكلمات (المرادفات المخصصة)
# ══════════════════════════════════════════════════
WORD_REPLACEMENTS = {}

# ══════════════════════════════════════════════════
#  أقسام التطبيق
# ══════════════════════════════════════════════════
SECTIONS = ["✨ مصنع المنتجات", "📊 لوحة التحكم", "🔴 سعر أعلى", "🟢 سعر أقل", "✅ موافق عليها", "🔍 منتجات مفقودة", "⚠️ تحت المراجعة", "⚪ مستبعد (لا يوجد تطابق)", "✅ تمت المعالجة", "⚡ أتمتة Make", "🔄 الأتمتة الذكية", "🕷️ كشط المنافسين", "🗑️ سلة المحذوفات", "⚙️ الإعدادات"]
SIDEBAR_SECTIONS = SECTIONS
PAGES_PER_TABLE  = 25
DB_PATH          = get_data_db_path("perfume_pricing.db")

# ══════════════════════════════════════════════════
#  Google Cloud Platform (GCP) Settings
# ══════════════════════════════════════════════════
GCP_PROJECT_ID            = _s("GCP_PROJECT_ID") or "mahwous-smart-pricing-v30"
GCS_BUCKET_NAME           = _s("GCS_BUCKET_NAME") or "mahwous-pricing-storage"
GCS_DB_BLOB_NAME          = _s("GCS_DB_BLOB_NAME") or "vision2030/pricing_v30.db"
CLOUD_SQL_CONNECTION_NAME = _s("CLOUD_SQL_CONNECTION_NAME")
DB_USER                   = _s("DB_USER")
DB_PASS                   = _s("DB_PASS")
DB_NAME                   = _s("DB_NAME") or "vision2030"
USE_FIRESTORE             = _s("USE_FIRESTORE", "false").lower() == "true"

GCP_ENABLED = bool(GCS_BUCKET_NAME or CLOUD_SQL_CONNECTION_NAME or USE_FIRESTORE)
