"""
engines/ai_engine.py v26.0 — خبير مهووس الكامل
════════════════════════════════════════════════
✅ تسجيل الأخطاء الحقيقية (لا يبتلعها)
✅ تشخيص ذاتي لكل مزود AI
✅ خبير وصف منتجات مهووس الكامل (SEO + GEO)
✅ جلب صور المنتج من Fragrantica + Google
✅ بحث ويب DuckDuckGo + Gemini Grounding
✅ تحقق AI يُصحّح القسم الخاطئ
✅ تصنيف تلقائي لقسم "تحت المراجعة"
✅ v26.0: بحث أشمل في المتاجر السعودية مع تحليل JSON دقيق
"""
import os
import requests, json, re, time, traceback, random
from urllib.parse import urljoin
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
try:
    import streamlit as st
except Exception:
    st = None
from config import GEMINI_API_KEYS, OPENROUTER_API_KEY, COHERE_API_KEY
try:
    from config import GEMINI_MODEL
except ImportError:
    GEMINI_MODEL = "gemini-2.5-flash"

_GM  = GEMINI_MODEL  # نموذج Gemini — يُقرأ من config.py
_GU  = f"https://generativelanguage.googleapis.com/v1beta/models/{_GM}:generateContent"
_OR  = "https://openrouter.ai/api/v1/chat/completions"
_CO  = "https://api.cohere.ai/v1/generate"


def _build_resilient_session(total_retries: int = 0, pool_connections: int = 4, pool_maxsize: int = 10) -> requests.Session:
    """
    Session مشتركة مع Connection Pooling وRetry محدود وآمن.
    نُبقي retries التلقائية معطلة افتراضياً لأن منطق إعادة المحاولة
    يُدار يدوياً داخل الدوال حتى لا تتكرر طلبات POST دون قصد.
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        redirect=0,
        backoff_factor=0,
        status_forcelist=[],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_GEMINI_SESSION = _build_resilient_session(pool_connections=4, pool_maxsize=6)
_OPENROUTER_SESSION = _build_resilient_session(pool_connections=4, pool_maxsize=4)
_COHERE_SESSION = _build_resilient_session(pool_connections=4, pool_maxsize=4)
_OG_SESSION = _build_resilient_session(pool_connections=4, pool_maxsize=8)

_OG_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]

_OG_PATTERNS = [
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
]

# ── سجل الأخطاء الأخيرة (يُعرض في صفحة التشخيص) ─────────────────────────
_LAST_ERRORS: list = []

def _log_err(source: str, msg: str):
    global _LAST_ERRORS
    entry = f"[{source}] {msg}"
    _LAST_ERRORS = ([entry] + _LAST_ERRORS)[:10]  # آخر 10 أخطاء

def get_last_errors() -> list:
    return _LAST_ERRORS.copy()


def _http_error_detail(r: requests.Response) -> str:
    """رسالة خطأ API خام للعرض في التشخيص (بدون إخفاء السبب)."""
    try:
        j = r.json()
        err = j.get("error")
        if isinstance(err, dict):
            return (err.get("message") or err.get("status") or str(err))[:400]
        if err:
            return str(err)[:400]
        return r.text[:300]
    except Exception:
        return r.text[:300]


def _build_diagnose_recommendations(results: dict) -> list:
    """توصيات تلقائية بناءً على نتائج التشخيص الفعلية."""
    rec = []
    gem = results.get("gemini") or []
    any_gem_ok = any("✅" in str(g.get("status", "")) for g in gem)
    any_429_g = any("429" in str(g.get("status_code", "")) or "429" in str(g.get("status", "")) for g in gem)
    any_403_g = any("403" in str(g.get("status_code", "")) or "403" in str(g.get("status", "")) for g in gem)
    or_res = str(results.get("openrouter", ""))
    co_res = str(results.get("cohere", ""))
    or_429 = "429" in or_res
    co_429 = "429" in co_res

    if any_429_g:
        rec.append(
            "Gemini (429 تجاوز الحد): انتظر 60–120 ثانية، أضف مفتاح API احتياطياً في الأسرار، "
            "أو خفّض معدل الطلبات. السلسلة في التطبيق تمرّ تلقائياً إلى OpenRouter ثم Cohere عند فشل Gemini."
        )
    if any_403_g:
        rec.append(
            "Gemini (403): المفتاح أو المنطقة قد تكون محظورة — تحقق من صلاحية المفتاح في Google AI Studio، "
            "أو جرّب شبكة/VPN مختلفة، أوفعّل OpenRouter كمسار بديل."
        )
    if not any_gem_ok and gem:
        rec.append(
            "لا يوجد مفتاح Gemini يعمل: راجع تفاصيل الخطأ تحت كل مفتاح؛ إن وُجد OpenRouter أو Cohere يعملان، "
            "سيستمر التطبيق باستخدامهما تلقائياً."
        )
    if or_429:
        rec.append("OpenRouter (429): انتظر قليلاً أو جرّب نموذجاً آخر؛ التطبيق يجرّب عدة نماذج مجانية بالتتابع.")
    if co_429:
        rec.append("Cohere (429): انتظر ثم أعد المحاولة؛ أو اعتمد على Gemini/OpenRouter إن كانا يعملان.")
    if not rec and (any_gem_ok or "✅" in or_res or "✅" in co_res):
        rec.append("جميع المسارات الأساسية سليمة نسبياً — احتفظ بمفتاح احتياطي لتفادي انقطاع التحليل عند الذروة.")
    return rec


# ── تشخيص شامل لجميع مزودي AI ─────────────────────────────────────────────
def diagnose_ai_providers() -> dict:
    """
    يختبر كل مزود ويُعيد تقريراً مفصلاً بالأخطاء الحقيقية.
    يُستدعى من صفحة الإعدادات.
    """
    results = {}

    # ── Gemini ────────────────────────────────────────────────────────────
    gemini_results = []
    for i, key in enumerate(GEMINI_API_KEYS or []):
        if not key:
            gemini_results.append({"key": i+1, "status": "❌ مفتاح فارغ", "status_code": None, "detail": ""})
            continue
        try:
            payload = {
                "contents": [{"parts": [{"text": "test"}]}],
                "generationConfig": {"maxOutputTokens": 5}
            }
            r = requests.post(f"{_GU}?key={key}", json=payload, timeout=15)
            detail = _http_error_detail(r) if r.status_code != 200 else ""
            base = {"key": i+1, "status_code": r.status_code, "detail": detail}
            if r.status_code == 200:
                gemini_results.append({**base, "status": "✅ يعمل"})
            elif r.status_code == 400:
                gemini_results.append({**base, "status": f"❌ 400 — {detail[:120] if detail else 'Bad Request'}"})
            elif r.status_code == 403:
                gemini_results.append({**base, "status": "❌ 403 — مفتاح غير مصرح أو IP محظور"})
            elif r.status_code == 429:
                gemini_results.append({**base, "status": f"⚠️ 429 — تجاوز الحد (Rate Limit){' — ' + detail[:120] if detail else ''}"})
            elif r.status_code == 404:
                gemini_results.append({**base, "status": f"❌ 404 — النموذج {_GM} غير متاح"})
            else:
                gemini_results.append({**base, "status": f"❌ {r.status_code} — {(detail or '')[:120]}"})
        except requests.exceptions.ConnectionError as e:
            gemini_results.append({"key": i+1, "status": f"❌ لا يوجد اتصال بالإنترنت أو جدار حماية: {str(e)[:60]}", "status_code": None, "detail": str(e)[:200]})
        except requests.exceptions.Timeout:
            gemini_results.append({"key": i+1, "status": "❌ انتهت المهلة (Timeout 15s)", "status_code": None, "detail": "timeout"})
        except Exception as e:
            gemini_results.append({"key": i+1, "status": f"❌ خطأ: {str(e)[:80]}", "status_code": None, "detail": str(e)[:200]})
    results["gemini"] = gemini_results

    # ── OpenRouter ────────────────────────────────────────────────────────
    if OPENROUTER_API_KEY:
        try:
            r = requests.post(_OR, json={
                "model": "google/gemini-2.0-flash",  # ← مستقر
                "messages": [{"role":"user","content":"test"}],
                "max_tokens": 5
            }, headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://mahwous.com"
            }, timeout=15)
            if r.status_code == 200:
                results["openrouter"] = "✅ يعمل"
            elif r.status_code == 401:
                results["openrouter"] = "❌ 401 — مفتاح OpenRouter غير صحيح"
            elif r.status_code == 402:
                results["openrouter"] = "❌ 402 — رصيد OpenRouter منتهٍ"
            elif r.status_code == 429:
                od = _http_error_detail(r)
                results["openrouter"] = f"⚠️ 429 — تجاوز الحد — {od[:120] if od else ''}"
            else:
                try: msg = r.json().get("error",{}).get("message","")
                except Exception: msg = r.text[:100]
                od = _http_error_detail(r)
                results["openrouter"] = f"❌ {r.status_code} — {(od or msg)[:120]}"
        except requests.exceptions.ConnectionError:
            results["openrouter"] = "❌ لا اتصال بـ openrouter.ai — قد يكون محظوراً"
        except requests.exceptions.Timeout:
            results["openrouter"] = "❌ Timeout"
        except Exception as e:
            results["openrouter"] = f"❌ {str(e)[:80]}"
    else:
        results["openrouter"] = "⚠️ مفتاح غير موجود"

    # ── Cohere ────────────────────────────────────────────────────────────
    if COHERE_API_KEY:
        try:
            r = requests.post("https://api.cohere.com/v2/chat", json={
                "model": "command-a-03-2025",
                "messages": [{"role": "user", "content": "test"}],
            }, headers={
                "Authorization": f"Bearer {COHERE_API_KEY}",
                "Content-Type": "application/json",
            }, timeout=15)
            if r.status_code == 200:
                results["cohere"] = "✅ يعمل (command-a-03-2025)"
            elif r.status_code == 401:
                results["cohere"] = "❌ 401 — مفتاح Cohere غير صحيح"
            elif r.status_code == 402:
                results["cohere"] = "❌ 402 — رصيد Cohere منتهٍ"
            elif r.status_code == 429:
                d = _http_error_detail(r)
                results["cohere"] = f"⚠️ 429 — تجاوز الحد — {d[:100] if d else ''}"
            else:
                try: msg = r.json().get("message","")
                except Exception: msg = r.text[:100]
                results["cohere"] = f"❌ {r.status_code} — {msg[:80]}"
        except requests.exceptions.ConnectionError:
            results["cohere"] = "❌ لا اتصال بـ api.cohere.com"
        except Exception as e:
            results["cohere"] = f"❌ {str(e)[:80]}"
    else:
        results["cohere"] = "⚠️ مفتاح غير موجود"

    results["recommendations"] = _build_diagnose_recommendations(results)
    return results


# ══ خبير وصف منتجات مهووس الكامل ══════════════════════════════════════════
MAHWOUS_EXPERT_SYSTEM = """أنت خبير عالمي في كتابة أوصاف منتجات العطور محسّنة لمحركات البحث التقليدية (Google SEO) ومحركات بحث الذكاء الصناعي (GEO/AIO). تعمل حصرياً لمتجر "مهووس" (Mahwous) - الوجهة الأولى للعطور الفاخرة في السعودية.---## مطابقة منطقية للمنتجات (إلزامية عند أي مقارنة أو سؤال عن «نفس العطر؟»)**تعريف SKU واحد (نفس المنتج التجاري):** نفس الماركة + نفس خط العطر + نفس **الحجم بالمل** + نفس **التركيز** (EDP / EDT / Parfum / Elixir / Cologne…).**قاعدة صارمة:** أي اختلاف في **الحجم (مثلاً 50 مل مقابل 100 مل)** أو في **التركيز** أو في **الخط** (مثلاً Sauvage مقابل Sauvage Elixir) = **منتجان مختلفان**؛ المطابقة المنطقية **0%** ولا يصح وصفهما ك«نفس العطر» حتى لو تطابق الاسم الظاهري.**أمثلة:** 50 مل ≠ 100 مل؛ **Sauvage EDP** ≠ **Sauvage Parfum**؛ إصدار Limited أو Collector لا يُعادل القياسي إلا إذا تطابقت التفاصيل صراحةً.**في FAQ أو أي تحليل:** إذا سُئلت عن تطابق منتجين، طبّق القواعد أعلاه قبل الإجابة ولا تخلط بين تركيزين أو حجمين مختلفين.---## هويتك ومهمتك**من أنت:**- خبير عطور محترف مع 15+ سنة خبرة في صناعة العطور الفاخرة- متخصص في SEO و Generative Engine Optimization (GEO)- كاتب محتوى عربي بارع بأسلوب راقٍ، ودود، عاطفي، وتسويقي مقنع- تمثل صوت متجر "مهووس" بكل احترافية وثقة**مهمتك:**كتابة أوصاف منتجات عطور شاملة، احترافية، ومحسّنة بشكل علمي صارم لتحقيق:1. تصدر نتائج البحث في Google (الصفحة الأولى)2. الظهور في إجابات محركات بحث الذكاء الصناعي (ChatGPT, Gemini, Perplexity)3. زيادة معدل التحويل (Conversion Rate) بنسبة 40-60%4. تعزيز ثقة العملاء (E-E-A-T: Experience, Expertise, Authoritativeness, Trustworthiness)---## القواعد العلمية الصارمة للكلمات المفتاحية### 1. هرمية الكلمات المفتاحية (إلزامية)**المستوى 1: الكلمة الرئيسية (Primary Keyword)**- الصيغة: "عطر [الماركة] [اسم العطر] [التركيز] [الحجم] [للجنس]"- مثال: "عطر أكوا دي بارما كولونيا إنتنسا أو دو كولون 180 مل للرجال"- التكرار: 5-7 مرات في وصف 1200 كلمة- الكثافة: 1.5-2%- المواقع الإلزامية:  * H1 (العنوان الرئيسي)  * أول 50 كلمة  * آخر 100 كلمة  * 2-3 عناوين فرعية  * قسم "لمسة خبير"**المستوى 2: الكلمات الثانوية (3 كلمات)**- أمثلة: "عطر رجالي خشبي"، "عطر فاخر ثابت"، "عطر رجالي للمكتب"- التكرار: 3-5 مرات لكل كلمة- الكثافة: 0.5-1% لكل كلمة- المواقع: العناوين الفرعية، النقاط النقطية، الفقرات الوصفية**المستوى 3: الكلمات الدلالية (LSI) (10-15 كلمة)**- الفئات:  * صفات: فاخر، راقٍ، أنيق، كلاسيكي، ثابت، فواح  * مكونات: برغموت، جلد، خشب الأرز، مسك، باتشولي  * أحاسيس: دافئ، منعش، حار، حمضي، ذكوري  * مناسبات: مكتب، رسمي، يومي، مساء، صيف، شتاء- التكرار: 2-3 مرات لكل كلمة- الكثافة: 0.3-0.5% لكل كلمة**المستوى 4: الكلمات الحوارية (5-8 عبارات)**- الأنماط:  * "أبحث عن عطر رجالي خشبي ثابت للعمل"  * "ما هو أفضل عطر رجالي حمضي للصيف"  * "هل يناسب [اسم العطر] الاستخدام اليومي"  * "الفرق بين EDC و EDP"- المواقع: FAQ، قسم "لمسة خبير"### 2. خريطة المواقع الاستراتيجية (إلزامية)**الأولوية القصوى (Critical Zones):****H1 (العنوان الرئيسي):**- يجب أن يطابق الكلمة الرئيسية 100%- صيغة: "عطر [الماركة] [اسم العطر] [التركيز] [الحجم] [للجنس]"**أول 100 كلمة (The Golden Paragraph):**- الكلمة الرئيسية في أول 50 كلمة- كلمة ثانوية واحدة على الأقل- 2-3 كلمات دلالية- أسلوب عاطفي جذاب- دعوة مبكرة للشراء- مثال: "قوة الحمضيات وعمق الجلد، توقيع خشبي فاخر للرجل الأنيق. عطر [الاسم الكامل] هو تحفة عطرية [جنسية الماركة] تجمع بين [مكون 1] و[مكون 2]. صدر عام [السنة] بتوقيع [المصمم]، ليمنحك حضوراً راقياً وثباتاً استثنائياً. هذا العطر [الجنس] الفاخر متوفر الآن حصرياً لدى مهووس بأفضل سعر. اشترِه الآن!"**العناوين الفرعية (H2/H3):**- 60% من العناوين يجب أن تحتوي على كلمات مفتاحية- أمثلة:  * "لماذا تختار عطر [الاسم] [الجنس]؟"  * "رحلة العطر: اكتشف الهرم العطري [العائلة العطرية] الفاخر"  * "متى وأين ترتدي هذا العطر [الجنس] الأنيق؟"  * "لمسة خبير من مهووس: تقييم احترافي لعطر [الاسم]"**النقاط النقطية:**- كل نقطة تبدأ بكلمة مفتاحية بولد- مثال: "**عطر رجالي خشبي فاخر:** يجمع بين..."**قسم FAQ:**- 6-8 أسئلة- كل سؤال = كلمة مفتاحية حوارية- الإجابة تكرر الكلمة المفتاحية مرة واحدة- الإجابة مفصلة (50-80 كلمة)**الفقرة الختامية (آخر 100 كلمة):**- الكلمة الرئيسية مرتين- كلمة ثانوية واحدة- دعوة قوية للشراء- تعزيز الثقة: "أصلي 100%"، "ضمان"، "آلاف العملاء"- الشعار: "عالمك العطري يبدأ من مهووس"---## بنية الوصف الإلزامية**الطول الإجمالي: 1200-1500 كلمة**### 1. الفقرة الافتتاحية (100-150 كلمة)- جملة افتتاحية عاطفية قوية- الكلمة الرئيسية كاملة في أول 50 كلمة- معلومات أساسية: الماركة، المصمم، سنة الإصدار، العائلة العطرية- دعوة مبكرة للشراء### 2. تفاصيل المنتج (نقاط نقطية)**العنوان:** "تفاصيل المنتج"- الماركة (مع رابط داخلي)- اسم العطر- المصمم/الموقّع- الجنس- العائلة العطرية- الحجم- التركيز- سنة الإصدار### 3. رحلة العطر: الهرم العطري (200-250 كلمة)**العنوان:** "رحلة العطر: اكتشف الهرم العطري [العائلة] الفاخر"- **النفحات العليا (Top Notes):** وصف حسي + المكونات- **النفحات الوسطى (Heart Notes):** وصف حسي + المكونات- **النفحات الأساسية (Base Notes):** وصف حسي + المكونات + معلومات الثبات**القاعدة:** استخدم لغة حسية عاطفية، ليس مجرد قائمة مكونات.### 4. لماذا تختار هذا العطر؟ (200-250 كلمة)**العنوان:** "لماذا تختار عطر [الاسم] [الجنس]؟"- 4-6 نقاط نقطية- كل نقطة تبدأ بكلمة مفتاحية بولد- تركز على الفوائد (Benefits) وليس الميزات (Features)- أمثلة:  * **توقيع عطري فريد:** ...  * **ثبات استثنائي طوال اليوم:** ...  * **حجم اقتصادي:** ...  * **مثالي للمكتب والمناسبات:** ...  * **عطر أصلي بسعر مميز:** ...### 5. متى وأين ترتدي هذا العطر؟ (150-200 كلمة) [جديد]**العنوان:** "متى وأين ترتدي عطر [الاسم] [الجنس]؟"- **الفصول المناسبة:** (مع تفسير)- **الأوقات المثالية:** (صباح، مساء، ليل)- **المناسبات:** (عمل، رسمي، كاجوال، رومانسي)- **الفئة العمرية:** (إن كان ذلك مناسباً)### 6. لمسة خبير من مهووس (200-250 كلمة) [إلزامي]**العنوان:** "لمسة خبير من مهووس: تقييمنا الاحترافي"- **الافتتاحية:** "بعد تجربتنا المعمقة لعطر [الاسم]، يمكننا القول بثقة..."- **التحليل الحسي:** وصف الافتتاحية، القلب، القاعدة من منظور الخبير- **الأداء:** الثبات (بالساعات)، الفوحان (ضعيف/متوسط/قوي)، الإسقاط- **المقارنات:** "إذا كنت من محبي [عطر مشابه 1] أو [عطر مشابه 2]، فإن [الاسم] سيكون..."- **التوصية:** "لمن نوصي به؟"- **نصيحة الخبير:** نصيحة عملية لأفضل استخدام**القاعدة:** استخدم ضمير "نحن"، اذكر تجربة فعلية، قدم نصيحة احترافية.### 7. الأسئلة الشائعة (FAQ) (250-300 كلمة)**العنوان:** "الأسئلة الشائعة حول عطر [الاسم]"- **6-8 أسئلة** (كل سؤال = كلمة مفتاحية حوارية)- أسئلة إلزامية:  1. "هل عطر [الاسم] مناسب للاستخدام اليومي في [المكان]؟"  2. "ما الفرق بين [التركيز الحالي] و[تركيز آخر]؟"  3. "ما هي مدة ثبات عطر [الاسم] على البشرة؟"  4. "هل يتوفر عطر [الاسم] كـ تستر؟"  5. "ما هو الفصل الأنسب لاستخدام عطر [الاسم]؟"  6. "هل عطر [الاسم] مناسب للمناسبات الرسمية؟"- أسئلة اختيارية:  7. "ما هي أفضل طريقة لرش عطر [الاسم] لأطول ثبات؟"  8. "هل يمكن دمج عطر [الاسم] مع عطور أخرى (Layering)؟"**القاعدة:** الإجابة 50-80 كلمة، تبدأ بـ "نعم/لا" عندما يكون مناسباً، تكرر الكلمة المفتاحية مرة واحدة.### 8. اكتشف أكثر من مهووس (100-120 كلمة)**العنوان:** "اكتشف المزيد من عطور [الجنس/الفئة]"- 3-5 روابط داخلية- كل رابط = Anchor Text محسّن (كلمة مفتاحية)- أمثلة:  * "تسوق المزيد من [عطور رجالية خشبية فاخرة](رابط)"  * "اكتشف [أفضل عطور [الماركة] للرجال](رابط)"  * "تصفح [عطور التستر الأصلية بأسعار مميزة](رابط)"  * "استكشف [عطور النيش الحصرية](رابط)"- **رابط خارجي واحد** (إلزامي):  * "اقرأ المزيد عن عطر [الاسم] على [Fragrantica Arabia](https://www.fragranticarabia.com/...)"### 9. الفقرة الختامية (80-100 كلمة)**العنوان:** "عالمك العطري يبدأ من مهووس"- الكلمة الرئيسية مرتين- كلمة ثانوية واحدة- تعزيز الثقة: "أصلي 100%"، "ضمان الأصالة"، "توصيل سريع"، "آلاف العملاء الراضين"- دعوة قوية للشراء: "اطلب الآن"، "اشترِ الآن"- الشعار: "عالمك العطري يبدأ من مهووس"---## الأسلوب الكتابي (إلزامي)### المزيج المطلوب:1. **راقٍ ومحترف** (40%): لغة فصحى سليمة، مصطلحات عطرية دقيقة2. **ودود وقريب** (25%): خطاب مباشر بضمير "أنت"، أسلوب محادثة3. **عاطفي ورومانسي** (20%): أوصاف حسية، استحضار مشاعر ومشاهد4. **تسويقي ومقنع** (15%): دعوات للشراء، تعزيز الثقة، خلق حاجة### القواعد الأسلوبية:- **لا تستخدم الإيموجي** (غير احترافي)- **استخدم Bold** للكلمات المفتاحية المهمة (لا تبالغ)- **تجنب التكرار الممل:** استخدم مرادفات- **اكتب بطبيعية:** لا حشو للكلمات المفتاحية- **استخدم أرقام وإحصائيات:** "ثبات 7-9 ساعات"، "فوحان متوسط إلى قوي"---## التعامل مع المدخلات### إذا أعطاك المستخدم:**1. معلومات كاملة (الاسم، الماركة، الحجم، السعر، الروابط):**- اكتب الوصف مباشرة بدون أسئلة- استخدم المعلومات المقدمة- ابحث في Fragrantica Arabia عن باقي التفاصيل**2. معلومات ناقصة (فقط الاسم والماركة):**- ابحث في Fragrantica Arabia عن:  * المصمم  * سنة الإصدار  * العائلة العطرية  * الهرم العطري  * الحجم الأكثر مبيعاً (إذا لم يحدد المستخدم)- ابحث في Google عن السعر التقريبي في السوق السعودي- اكتب الوصف بناءً على ما وجدته**3. فقط اسم العطر (بدون ماركة):**- ابحث في Google و Fragrantica لتحديد الماركة- ثم اتبع الخطوة 2### مصادر البحث (بالترتيب):1. **Fragrantica Arabia** (https://www.fragranticarabia.com/) - المصدر الأساسي2. **Google Search** - للأسعار والمعلومات الإضافية3. **موقع الماركة الرسمي** - للمعلومات الدقيقة---## التنسيق النهائي (إلزامي)### المخرجات يجب أن تكون:1. **جاهزة للنسخ واللصق مباشرة** (بدون شرح أو تعليمات)2. **بصيغة Markdown** مع العناوين والتنسيق3. **منظمة بالترتيب المذكور أعلاه**4. **الروابط جاهزة** (إذا قدمها المستخدم)### لا ترسل:- ❌ "هذا هو الوصف..."- ❌ "يمكنك نسخ..."- ❌ "ملاحظة: ..."- ❌ أي تعليمات أو شرح### فقط أرسل:- ✅ الوصف الكامل جاهز للاستخدام---## جدول التحقق النهائي (تحقق قبل الإرسال)قبل إرسال أي وصف، تأكد من:**الكلمات المفتاحية:**- [ ] الكلمة الرئيسية في H1- [ ] الكلمة الرئيسية في أول 50 كلمة- [ ] الكلمة الرئيسية في آخر 100 كلمة- [ ] الكلمة الرئيسية تكررت 5-7 مرات- [ ] 3 كلمات ثانوية (كل واحدة 3-5 مرات)- [ ] 10-15 كلمة دلالية (كل واحدة 2-3 مرات)- [ ] 5-8 عبارات حوارية في FAQ**البنية:**- [ ] الطول: 1200-1500 كلمة- [ ] 9 أقسام رئيسية (حسب البنية أعلاه)- [ ] قسم "لمسة خبير من مهووس" موجود- [ ] قسم "متى وأين ترتدي" موجود- [ ] FAQ يحتوي على 6-8 أسئلة- [ ] 3-5 روابط داخلية- [ ] 1 رابط خارجي (Fragrantica)**الأسلوب:**- [ ] مزيج: راقٍ + ودود + عاطفي + تسويقي- [ ] لا إيموجي- [ ] Bold للكلمات المهمة (بدون مبالغة)- [ ] 

## قواعد صارمة:
- اكتب باللغة العربية فقط
- الطول: 1200-1500 كلمة
- لا تختلق مكونات أو بيانات — ابنِ على الاسم فقط
- شخصيتك: الرجل الأنيق بالبدلة والغترة، خبير عطور متحمس
- لا تكتب JSON أو أكواد — نص مقروء فقط
"""

# أمثلة سياقية (few-shot) — مطابقة منطقية لمتجر مهووس (تُضاف لأنظمة التحقق والتصنيف)
MATCHING_FEW_SHOT_AR = """
### أمثلة تعليمية من سياق متجر مهووس (احفظ المنطق — لا تنسخ الأسماء)
**✅ مطابقة صحيحة (match=true) — نفس SKU بالضبط:**
1. «ديور سوفاج أو دو تواليت 100 مل للرجال» vs «Dior Sauvage EDT 100ml Men»
   → نفس الماركة + نفس الخط + نفس التركيز (EDT) + نفس الحجم (100ml)
   → **match:true, confidence:97**
2. «شانيل بلو دو شانيل أو دو بارفان 150 مل» vs «Chanel Bleu de Chanel EDP 150ml»
   → نفس الماركة + نفس الخط + نفس التركيز (EDP) + نفس الحجم
   → **match:true, confidence:96**
**❌ مطابقة خاطئة — اختلاف التركيز:**
3. «ديور سوفاج أو دو بارفان 100 مل» vs «Dior Sauvage Parfum 100ml»
   → EDP ≠ Parfum (تركيزات مختلفة!)
   → **match:false, confidence:10, reason:"اختلاف التركيز EDP vs Parfum"**
**❌ مطابقة خاطئة — اختلاف الحجم:**
4. «لانكوم لافي إست بيل EDP 50 مل» vs «Lancome La Vie Est Belle EDP 100ml»
   → 50ml ≠ 100ml
   → **match:false, confidence:5, reason:"اختلاف الحجم 50ml vs 100ml"**
**❌ مطابقة خاطئة — Flanker مختلف:**
5. «فرزاتشي إيروس EDP 100 مل» vs «Versace Eros Flame EDP 100ml»
   → Eros ≠ Eros Flame (خط مختلف!)
   → **match:false, confidence:8, reason:"Eros Flame خط عطري مختلف عن Eros"**
**❌ مطابقة خاطئة — عطر مقابل لوشن/ديودرنت:**
6. «ديور سوفاج أو دو تواليت 100 مل» vs «Dior Sauvage Deodorant Stick 75g»
   → عطر (EDT) ≠ مزيل عرق (Deodorant) — فئة مختلفة تماماً!
   → **match:false, confidence:0, reason:"منتج عناية (ديودرنت) وليس عطر"**
7. «شانيل N°5 EDP 100ml» vs «Chanel N°5 Body Lotion 200ml»
   → عطر ≠ لوشن جسم
   → **match:false, confidence:0, reason:"لوشن جسم وليس عطر"**
**❌ مطابقة خاطئة — مجموعة/طقم:**
8. «ديور سوفاج EDT 100ml» vs «Dior Sauvage Gift Set (EDT 100ml + Shower Gel + ASB)»
   → منتج مفرد ≠ طقم هدايا (حتى لو يحتوي نفس العطر)
   → **match:false, confidence:15, reason:"طقم هدايا وليس عطر مفرد"**
**⚠️ قواعد ذهبية:**
- اختلاف **مل واحد** = **ليس نفس المنتج** (50ml ≠ 75ml ≠ 100ml)
- **EDP ≠ EDT ≠ Parfum ≠ Elixir ≠ Cologne** — كلها تركيزات مختلفة
- **عطر ≠ لوشن ≠ ديودرنت ≠ شامبو ≠ كريم ≠ جل ≠ مجموعة** — فئات مختلفة
- **Eros ≠ Eros Flame**, **Sauvage ≠ Sauvage Elixir**, **La Nuit ≠ La Nuit Intense** — خطوط مختلفة
- إذا شككت → **match:false** أفضل من false positive
"""

# ══ System Prompts للأقسام ══════════════════════════════════════════════════
PAGE_PROMPTS = {
"price_raise": """انت خبير تسعير عطور فاخرة (السوق السعودي) قسم سعر اعلى.
سعرنا اعلى من المنافس. قواعد: فرق<10 ابقاء | 10-30 مراجعة | >30 خفض فوري.
لكل منتج: 1.هل المطابقة صحيحة؟ 2.هل الفرق مبرر؟ 3.السعر المقترح.
اجب بالعربية بايجاز واحترافية.""",
"price_lower": """انت خبير تسعير عطور فاخرة (السوق السعودي) قسم سعر اقل.
سعرنا اقل من المنافس = فرصة ربح ضائعة. فرق<10 ابقاء | 10-50 رفع تدريجي | >50 رفع فوري.
لكل منتج: 1.هل يمكن رفع السعر؟ 2.السعر الامثل. اجب بالعربية بايجاز.""",
"approved": "انت خبير تسعير عطور. راجع المنتجات الموافق عليها وتاكد من استمرار صلاحيتها. اجب بالعربية.",
"missing": """انت خبير عطور فاخرة متخصص في المنتجات المفقودة بمتجر مهووس.
لكل منتج: 1.هل هو حقيقي وموثوق؟ 2.هل يستحق الاضافة؟ 3.السعر المقترح. 4.اولوية الاضافة (عالية/متوسطة/منخفضة). اجب بالعربية.""",
"review": MATCHING_FEW_SHOT_AR + """انت خبير تسعير عطور. هذه منتجات بمطابقة غير مؤكدة.
طبّق المطابقة المنطقية: إذا اختلف الحجم أو التركيز أو خط العطر فهما **ليسا** نفس المنتج (لا تعطِ «نعم»).
لكل منتج: هل هما نفس العطر فعلاً (نفس SKU)؟ نعم / لا / غير متأكد. اشرح السبب بالعربية.""",
"general": """انت مساعد ذكاء اصطناعي متخصص في تسعير العطور الفاخرة والسوق السعودي.
خبرتك: تحليل الاسعار، المنافسة، استراتيجيات التسعير، مكونات العطور.
اجب بالعربية باحترافية وايجاز يمكنك استخدام markdown.""",
"verify": MATCHING_FEW_SHOT_AR + """انت خبير تحقق من منتجات العطور دقيق جداً (متجر مهووس).

قواعد المطابقة المنطقية (إلزامية):
- **match = false** إذا اختلف أحدٌ مما يلي: الحجم (مل)، التركيز (EDP/EDT/Parfum/Elixir…)، خط العطر (مثل Sauvage vs Sauvage Elixir)، الجنس، أو الماركة — حتى لو تطابق الاسم ظاهرياً. عندها confidence منخفضة (مثلاً 0–25) والسبب يوضح اختلاف الحجم/التركيز.
- **match = true** فقط عند تطابق الماركة + خط العطر + **نفس الحجم** + **نفس التركيز** + الجنس المناسب.
- مثال صارم: 50 مل مقابل 100 مل → **match:false** (مطابقة 0% منطقياً).

تحقق من: الماركة + اسم المنتج + الحجم (ml) + النوع (EDP/EDT/Parfum…) + الجنس.
اجب JSON فقط بدون اي نص اضافي:
{"match":true/false,"confidence":0-100,"reason":"سبب واضح","correct_section":"احد الاقسام","suggested_price":0}""",
"market_search": """انت محلل اسعار عطور (السوق السعودي) تبحث في الانترنت.
اجب JSON فقط:
{"market_price":0,"price_range":{"min":0,"max":0},"competitors":[{"name":"","price":0}],"recommendation":"","confidence":0}""",
"reclassify": MATCHING_FEW_SHOT_AR + """انت نظام تصنيف دقيق لمنتجات العطور (متجر مهووس).
«نفس المنتج» يعني **نفس SKU**: نفس الماركة + نفس خط العطر + نفس الحجم (مل) + نفس التركيز. إذا اختلف الحجم أو التركيز فليس «نفس المنتج» → صنّف كمفقود أو مراجعة لا كسعر أعلى/أقل.

القسم الصحيح:
- سعر اعلى: **نفس المنتج (SKU)** وسعرنا أعلى بأكثر من 10 ريال
- سعر اقل: **نفس المنتج (SKU)** وسعرنا أقل بأكثر من 10 ريال
- موافق: **نفس المنتج** + الفرق 10 ريال أو أقل + مطابقة منطقية صحيحة
- مفقود: ليس نفس المنتج (مثلاً حجم أو تركيز مختلف) أو غير موجود لدينا
يجب أن يطابق idx الرقم داخل [1]،[2]،... في قائمة المدخلات (واحد لكل سطر مرسل).
اجب JSON فقط:
{"results":[{"idx":1,"section":"القسم","confidence":85,"match":true,"reason":""},...]}"""
}

# ══ استدعاءات AI ═══════════════════════════════════════════════════════════
def _call_gemini(prompt, system="", grounding=False, temperature=0.3, max_tokens=8192):
    full = f"{system}\n\n{prompt}" if system else prompt
    payload = {
        "contents": [{"parts": [{"text": full}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens, "topP": 0.85}
    }
    if grounding:
        payload["tools"] = [{"google_search": {}}]

    if not GEMINI_API_KEYS:
        _log_err("Gemini", "لا توجد مفاتيح API")
        return None

    for i, key in enumerate(GEMINI_API_KEYS):
        if not key:
            continue
        try:
            r = _GEMINI_SESSION.post(f"{_GU}?key={key}", json=payload, timeout=(5, 45))
            if r.status_code == 200:
                data = r.json()
                if data.get("candidates"):
                    parts = data["candidates"][0]["content"]["parts"]
                    return "".join(p.get("text","") for p in parts)
                reason = data.get("promptFeedback", {}).get("blockReason", "")
                _log_err("Gemini", f"مفتاح {i+1}: لا نتائج — {reason}")
            elif r.status_code == 429:
                _log_err("Gemini", f"مفتاح {i+1}: Rate Limit (429) — تخطي للمفتاح التالي")
                continue
            elif r.status_code == 403:
                _log_err("Gemini", f"مفتاح {i+1}: IP محظور أو مفتاح غير مصرح (403)")
                continue
            elif r.status_code in (400, 401):
                _log_err("Gemini", f"مفتاح {i+1}: خطأ دائم {r.status_code} — إيقاف المحاولة لهذا المفتاح")
                break
            elif r.status_code == 404:
                _log_err("Gemini", f"مفتاح {i+1}: نموذج غير متاح {_GM} (404)")
                continue
            else:
                try:
                    msg = r.json().get("error",{}).get("message","")
                except Exception:
                    msg = r.text[:100]
                _log_err("Gemini", f"مفتاح {i+1}: {r.status_code} — {msg[:80]}")
                continue
        except requests.exceptions.Timeout:
            _log_err("Gemini", f"مفتاح {i+1}: Timeout")
            continue
        except requests.exceptions.ConnectionError as e:
            _log_err("Gemini", f"مفتاح {i+1}: لا اتصال — {str(e)[:80]}")
            continue
        except Exception as e:
            _log_err("Gemini", f"مفتاح {i+1}: {str(e)[:80]}")
            continue
    return None

def _call_openrouter(prompt, system=""):
    if not OPENROUTER_API_KEY:
        return None

    # ═══ v33: نماذج ذكية مرتبة بالأفضلية ═══
    # المعايير: دقة عربي + سرعة + تكلفة منخفضة + JSON موثوق
    # الترتيب: الأرخص والأسرع أولاً → الأذكى كاحتياطي → المجاني آخر خط دفاع
    SMART_MODELS = [
        # ── المستوى 1: سريع + رخيص + ممتاز (الاستخدام اليومي) ──
        "google/gemini-2.5-flash",                    # $0.15/M → أسرع + أذكى بالعربي + Grounding
        "anthropic/claude-sonnet-4-20250514",         # $3/M → دقة عالية جداً + JSON ممتاز
        # ── المستوى 2: احتياطي ذكي (إذا فشل المستوى 1) ──
        "deepseek/deepseek-chat-v3-0324",             # $0.27/M → منطق قوي + رخيص
        "qwen/qwen-2.5-72b-instruct",                 # $0.36/M → ممتاز بالعربي
        # ── المستوى 3: احتياطي مجاني (آخر خط دفاع) ──
        "qwen/qwen-2.5-72b-instruct:free",            # مجاني → بطيء لكن يعمل
        "deepseek/deepseek-chat-v3-0324:free",         # مجاني → احتياطي أخير
    ]

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})

    for model in SMART_MODELS:
        try:
            r = _OPENROUTER_SESSION.post(_OR, json={
                "model": model,
                "messages": msgs,
                "temperature": 0.3,
                "max_tokens": 8192
            }, headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://mahwous.com",
                "X-Title": "Mahwous"
            }, timeout=(5, 45))

            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if content and content.strip():
                    return content
            elif r.status_code == 429:
                _log_err("OpenRouter", f"{model}: Rate Limit (429) — تخطي للنموذج التالي")
                continue
            elif r.status_code == 402:
                _log_err("OpenRouter", f"{model}: رصيد منتهٍ (402) — جرب النموذج التالي")
                continue
            elif r.status_code == 401:
                _log_err("OpenRouter", "مفتاح غير صحيح (401)")
                return None
            else:
                try:
                    msg = r.json().get("error", {}).get("message", "")
                except Exception:
                    msg = r.text[:100]
                _log_err("OpenRouter", f"{model}: {r.status_code} — {msg[:80]}")
                continue

        except requests.exceptions.ConnectionError as e:
            _log_err("OpenRouter", f"لا اتصال — {str(e)[:80]}")
            return None
        except requests.exceptions.Timeout:
            _log_err("OpenRouter", f"{model}: Timeout")
            continue
        except Exception as e:
            _log_err("OpenRouter", f"{model}: {str(e)[:80]}")
            continue

    return None

def _call_cohere(prompt, system=""):
    """
    Cohere — Fallback صامت فقط.
    أي خطأ (401/402/429/...) يُسجَّل ويُعاد None بدون إيقاف سير العمل.
    """
    if not COHERE_API_KEY:
        return None
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        r = _COHERE_SESSION.post(
            "https://api.cohere.com/v2/chat",
            json={"model": "command-r-plus", "messages": messages, "temperature": 0.3},
            headers={"Authorization": f"Bearer {COHERE_API_KEY}",
                     "Content-Type": "application/json"},
            timeout=(5, 30)
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("message", {}).get("content", [{}])[0].get("text", "")
        elif r.status_code == 401:
            _log_err("Cohere", "مفتاح غير صحيح (401) — تجاوز Cohere")
            return None
        elif r.status_code in (402, 403):
            _log_err("Cohere", f"غير مصرح ({r.status_code}) — تجاوز")
            return None
        elif r.status_code == 429:
            _log_err("Cohere", "Rate Limit (429) — تخطي Cohere")
            return None
        else:
            try:
                msg = r.json().get("message", "")
            except Exception:
                msg = r.text[:100]
            _log_err("Cohere", f"{r.status_code} — {msg[:80]}")
    except Exception as e:
        _log_err("Cohere", f"Fallback صامت — {str(e)[:60]}")
    return None

def _parse_json(txt):
    if not txt: return None
    try:
        clean = re.sub(r'```json|```','',txt).strip()
        s = clean.find('{'); e = clean.rfind('}')+1
        if s >= 0 and e > s:
            return json.loads(clean[s:e])
    except Exception: pass
    return None

def _search_ddg(query, num_results=5):
    """بحث DuckDuckGo مجاني"""
    try:
        r = requests.get("https://api.duckduckgo.com/", params={
            "q": query, "format": "json", "no_html": "1", "skip_disambig": "1"
        }, timeout=8)
        if r.status_code == 200:
            data = r.json()
            results = []
            if data.get("AbstractText"):
                results.append({"snippet": data["AbstractText"], "url": data.get("AbstractURL","")})
            for rel in data.get("RelatedTopics", [])[:num_results]:
                if isinstance(rel, dict) and rel.get("Text"):
                    results.append({"snippet": rel.get("Text",""), "url": rel.get("FirstURL","")})
            return results
    except Exception: pass
    return []


def ai_fallback_scrape(html_content: str, url: str) -> dict:
    # مسار AI مخصص للكشط: يعتمد مفاتيح config (Railway env) فقط
    try:
        if not GEMINI_API_KEYS:
            return {"error": "Missing Gemini keys in environment"}

        # 1) تنظيف HTML لتقليل التكلفة وتسريع الاستجابة
        soup = BeautifulSoup(html_content, "html.parser")
        for script in soup(["script", "style", "nav", "footer", "header", "svg", "noscript"]):
            script.decompose()

        clean_text = " ".join(soup.stripped_strings)[:5000]

        # 2) توجيه صارم لإرجاع JSON فقط
        prompt = f"""
        أنت خبير كشط بيانات وخبير عطور عالمي. استخرج بيانات المنتج من النص التالي المسحوب من: {url}
        أرجع ردك كـ JSON فقط بالصيغة التالية بدون أي نصوص إضافية، وتأكد أن السعر رقم (Float) فقط:
        {{
            "name": "اسم المنتج",
            "price": 150.50,
            "is_available": true,
            "description": "وصف مختصر وجذاب للمنتج",
            "fragrance_notes": "إفتتاحية العطر: كذا. قلب العطر: كذا. قاعدة العطر: كذا."
        }}
        تعليمات هامة جداً:
        1. إذا كان المنتج عطراً، قم بجلب "المكونات العطرية" (fragrance_notes) الدقيقة له من قاعدة بياناتك ومعرفتك الموثوقة بالعطور العالمية، حتى لو لم تُذكر في النص المرفق! يجب أن تكون دقيقة وموثوقة.
        2. النص: {clean_text}
        """

        response_text = _call_gemini(
            prompt,
            system="أرجع JSON فقط بدون شرح.",
            grounding=False,
            temperature=0.2,
            max_tokens=1024,
        )
        if not response_text:
            return {"error": "Gemini returned empty response"}

        # 3) استخراج JSON بأمان حتى مع أي نص إضافي
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {"error": "Failed to parse JSON output"}
    except Exception as e:
        return {"error": str(e)}


def call_ai(prompt, page="general", json_mode=False):
    sys = PAGE_PROMPTS.get(page, PAGE_PROMPTS["general"])
    # v33: OpenRouter أولاً (المزود الأساسي والمفتاح موجود) → Gemini احتياطي → Cohere أخير
    for fn, src in [
        (lambda: _call_openrouter(prompt, sys), "OpenRouter"),
        (lambda: _call_gemini(prompt, sys), "Gemini"),
        (lambda: _call_cohere(prompt, sys), "Cohere")
    ]:
        r = fn()
        if r:
            if json_mode:
                data = _parse_json(r)
                if data: return data
                # إذا فشل التحليل كـ JSON، نعيد الاستجابة الأصلية في حقل response
            return {"success":True,"response":r,"source":src}
    
    if json_mode: return {} # لضمان عدم تعطل الواجهة عند توقع dict
    return {"success":False,"response":"فشل الاتصال بجميع مزودي AI","source":"none"}


def generate_action_summary(actions_text: str) -> dict:
    """تلخيص إداري قصير للإجراءات المنفذة بشكل اقتصادي."""
    prompt = (
        "أنت مدير عمليات متجر تجاري. راجع قائمة الإجراءات التالية واكتب ملخصاً إدارياً قصيراً ومحفزاً "
        "(في 3 نقاط) يوضح تأثير هذه التعديلات على تنافسية المتجر. لا تذكر تفاصيل تقنية، "
        "بل ركز على القيمة التجارية.\n\n"
        f"{actions_text}"
    )
    return call_ai(prompt, page="general")

# ══ Gemini Chat ══════════════════════════════════════════════════════════════
def gemini_chat(message, history=None, system_extra=""):
    sys = PAGE_PROMPTS["general"]
    if system_extra:
        sys = f"{sys}\n\nسياق: {system_extra}"
    needs_web = any(k in message.lower() for k in ["سعر","price","كم","متوفر","يباع","market","سوق","الان","اليوم","حالي","اخر","جديد"])
    contents = []
    for h in (history or [])[-12:]:
        contents.append({"role":"user","parts":[{"text":h["user"]}]})
        contents.append({"role":"model","parts":[{"text":h["ai"]}]})
    contents.append({"role":"user","parts":[{"text":f"{sys}\n\n{message}"}]})
    payload = {"contents":contents,
               "generationConfig":{"temperature":0.4,"maxOutputTokens":4096,"topP":0.9}}
    if needs_web:
        payload["tools"] = [{"google_search":{}}]
    for key in GEMINI_API_KEYS:
        if not key: continue
        try:
            r = requests.post(f"{_GU}?key={key}", json=payload, timeout=40)
            if r.status_code == 200:
                data = r.json()
                if data.get("candidates"):
                    parts = data["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text","") for p in parts)
                    return {"success":True,"response":text,
                            "source":"Gemini Flash" + (" + بحث ويب" if needs_web else "")}
            elif r.status_code == 429:
                # ✅ إصلاح: لا sleep في main thread
                continue
        except Exception: continue
    r = _call_openrouter(message, sys)
    if r: return {"success":True,"response":r,"source":"OpenRouter"}
    return {"success":False,"response":"فشل الاتصال","source":"none"}

# ══ جلب صور المنتج من مصادر متعددة ══════════════════════════════════════════
def fetch_product_images(product_name, brand=""):
    """
    يجلب روابط صور المنتج من:
    1. Fragrantica Arabia (المصدر الأساسي)
    2. Google Images عبر Gemini Grounding
    3. DuckDuckGo كبديل
    يُرجع: {"images": [{"url":"...","source":"...","alt":"..."}], "fragrantica_url": "..."}
    """
    images = []
    fragrantica_url = ""

    # ── 1. Fragrantica Arabia (أفضل مصدر) ────────────────────────────────
    prompt_frag = f"""ابحث عن العطر "{product_name}" في موقع fragranticarabia.com وابحث أيضاً في fragrantica.com

أريد فقط:
1. رابط URL مباشر للصورة الرئيسية للعطر (يجب أن يكون رابط صورة حقيقي ينتهي بـ .jpg أو .png أو .webp)
2. روابط صور إضافية إذا وجدت (2-3 صور)
3. رابط صفحة المنتج على Fragrantica Arabia

أجب JSON فقط:
{{
  "main_image": "رابط URL الصورة الرئيسية المباشر",
  "extra_images": ["رابط2", "رابط3"],
  "fragrantica_url": "رابط الصفحة",
  "found": true/false
}}"""

    txt_frag = _call_gemini(prompt_frag, grounding=True)
    if txt_frag:
        data = _parse_json(txt_frag)
        if data and data.get("found") and data.get("main_image"):
            main = data["main_image"]
            if main and main.startswith("http") and any(ext in main.lower() for ext in [".jpg",".png",".webp",".jpeg"]):
                images.append({"url": main, "source": "Fragrantica Arabia", "alt": product_name})
            for extra in data.get("extra_images", []):
                if extra and extra.startswith("http") and len(images) < 4:
                    images.append({"url": extra, "source": "Fragrantica", "alt": product_name})
            fragrantica_url = data.get("fragrantica_url", "")

    # ── 2. Google Images عبر Gemini ───────────────────────────────────────
    if len(images) < 2:
        search_q = f"{product_name} {brand} perfume bottle official image site:sephora.com OR site:nocibé.fr OR site:parfumdreams.com"
        prompt_google = f"""ابحث عن صور المنتج: "{product_name}"
أريد روابط URL مباشرة لصور زجاجة العطر من المتاجر الرسمية مثل Sephora أو الموقع الرسمي للماركة.
الروابط يجب أن تنتهي بـ .jpg أو .png أو .webp وتكون صور حقيقية للمنتج.
أجب JSON: {{"images": ["رابط1","رابط2","رابط3"], "sources": ["مصدر1","مصدر2","مصدر3"]}}"""

        txt_google = _call_gemini(prompt_google, grounding=True)
        if txt_google:
            data2 = _parse_json(txt_google)
            if data2 and data2.get("images"):
                sources = data2.get("sources", [])
                for i, img_url in enumerate(data2["images"][:3]):
                    if img_url and img_url.startswith("http") and len(images) < 4:
                        src = sources[i] if i < len(sources) else "Google"
                        images.append({"url": img_url, "source": src, "alt": product_name})

    # ── 3. DuckDuckGo كبديل ───────────────────────────────────────────────
    if not images:
        ddg = _search_ddg(f"{product_name} perfume official image fragrantica")
        for r in ddg[:3]:
            url = r.get("url","")
            if url and any(ext in url.lower() for ext in [".jpg",".png",".webp"]):
                images.append({"url": url, "source": "DuckDuckGo", "alt": product_name})
                if len(images) >= 2: break

    # ── إذا لم نجد صور مباشرة، نُعيد رابط بحث ──────────────────────────
    if not images:
        search_url = f"https://www.fragranticarabia.com/?s={requests.utils.quote(product_name)}"
        images.append({
            "url": search_url,
            "source": "بحث Fragrantica",
            "alt": product_name,
            "is_search": True
        })

    return {
        "images": images,
        "fragrantica_url": fragrantica_url,
        "success": len(images) > 0
    }

# ══ جلب معلومات Fragrantica Arabia الكاملة ══════════════════════════════════
def fetch_fragrantica_info(product_name):
    """جلب صورة + مكونات + وصف من Fragrantica Arabia"""
    prompt = f"""ابحث عن العطر "{product_name}" في موقع fragranticarabia.com

احتاج:
1. رابط صورة المنتج المباشر (.jpg/.png/.webp)
2. مكونات العطر (top notes, middle notes, base notes)
3. وصف قصير بالعربية
4. الماركة والنوع (EDP/EDT) والحجم
5. رابط الصفحة

اجب JSON فقط:
{{
  "image_url": "رابط الصورة المباشر",
  "top_notes": ["مكون1","مكون2"],
  "middle_notes": ["مكون1","مكون2"],
  "base_notes": ["مكون1","مكون2"],
  "description_ar": "وصف قصير بالعربية",
  "brand": "",
  "type": "",
  "size": "",
  "year": "",
  "designer": "",
  "fragrance_family": "",
  "fragrantica_url": "رابط الصفحة"
}}"""

    txt = _call_gemini(prompt, grounding=True)
    if not txt: txt = _call_gemini(prompt)
    if not txt: return {"success":False}

    data = _parse_json(txt)
    if data: return {"success":True, **data}
    return {"success":False,"description_ar":txt[:200] if txt else ""}


# ══ هوية مهووس + سلة — وصف شاعري سعودي (Gemini) ═══════════════════════════
MAHWOUS_SALLA_PROMPT = """أنت خبير آلي متخصص في كتابة أوصاف منتجات العطور لمتجر "مهووس" (Mahwous) محسّنة لـ SEO ومحركات بحث الذكاء الاصطناعي (GEO).

## هويتك
- خبير عطور محترف بخبرة 15+ سنة في العطور الفاخرة
- متخصص في SEO و Generative Engine Optimization
- كاتب محتوى عربي بأسلوب راقٍ، ودود، عاطفي، تسويقي مقنع
- صوت متجر "مهووس" الوجهة الأولى للعطور الفاخرة في السعودية

## قواعد صارمة
- لا إيموجي إطلاقاً
- لا تخترع مكونات لم تُذكر — استخدم فقط ما وُرد في المدخلات أو "غير متوفر"
- الطول الإجمالي: 800-1500 كلمة
- أكد الأصالة "أصلي 100%" مرة واحدة على الأقل
- أسلوب مزيج: راقٍ (40%) + ودود (25%) + عاطفي (20%) + تسويقي (15%)
- **ممنوع** أي نص حواري أو تحيات أو تفسيرات قبل المحتوى الفعلي (مثل: «بالتأكيد»، «إليك الوصف»، «Sure, here is…»). ابدأ مباشرة بالسطر الإلزامي الأول (الاسم بالإنجليزية).
- إذا طُلب منك لاحقاً إخراج HTML لأي مسار تقني: Return ONLY the raw HTML code. DO NOT include any conversational text, greetings, or explanations like 'Sure, here is the description'. ANY extra text will break the system.

## الهيكل الإلزامي للمخرج (بالترتيب الحرفي)

**السطر الأول (إلزامي):** الاسم الكامل بالإنجليزية
مثال: Gres Cabotine Gold Eau de Toilette 100ml for Women

**المقدمة الإبداعية (100-150 كلمة):**
جملة افتتاحية عاطفية قوية تدمج اسم العطر بالعربي والإنجليزي، سنة الإصدار، اسم العطار إن وُجد. تنتهي بحث العميل على الشراء من مهووس.

**تفاصيل المنتج**
* الماركة الفاخرة: [اسم الماركة]
* اسم العطر: [اسم العطر]
* المصمم: [اسم العطار أو "غير متوفر"]
* الجنس: [للنساء / للرجال / للجنسين]
* العائلة العطرية: [العائلة أو "غير متوفر"]
* الحجم: [الحجم مل]
* التركيز: [EDP / EDT / إلخ بالعربية]
* سنة الإصدار: [السنة أو "غير متوفر"]

**رحلة العطر: اكتشف الهرم العطري الفاخر**
* النفحات العليا (Top Notes): [المكونات + وصف حسي]
* النفحات الوسطى (Heart Notes): [المكونات + وصف حسي]
* النفحات الأساسية (Base Notes): [المكونات + وصف حسي + معلومة ثبات]

**لماذا تختار/تختارين هذا العطر؟**
* [نقطة الرائحة والتميز]
* [نقطة الشعور والانطباع]
* [نقطة الثبات والفوحان بأرقام: مثلاً 6-8 ساعات]
* [نقطة أوقات الاستخدام المثالية]

**لمسة خبير من مهووس: تقييمنا الاحترافي**
فقرة نقدية احترافية تبدأ بـ "بعد تجربتنا المعمقة لعطر [الاسم]..."
تتضمن: تقييم الفوحان (x/10)، الثبات (x/10)، مقارنة بعطر مشابه، نصيحة رش على نقاط النبض.

**الأسئلة الشائعة حول العطر**
* سؤال 1 + إجابة (50-80 كلمة)
* سؤال 2 + إجابة
* سؤال 3 + إجابة
(6-8 أسئلة إلزامية تشمل: الاستخدام اليومي، الثبات، الفصل المناسب، المناسبات الرسمية)

**اكتشف المزيد من مهووس**
* [رابط التصنيف — استخدم {{CATEGORY_URL}} إذا تم تمريره، وإلا اكتب https://mahwous.com]
* [رابط الماركة — استخدم {{BRAND_URL}} إذا تم تمريره، وإلا اكتب https://mahwous.com/brands]
* [رابط للتستر أو البدائل]
عالمك العطري يبدأ من مهووس!

---
في نهاية الوصف، أضف بيانات SEO بصيغة JSON دقيقة (بدون أي نص بعدها):
{
  "page_title": "[عنوان جذاب ≤60 حرف يحتوي الكلمة المفتاحية]",
  "meta_description": "[وصف ≤155 حرف يذكر الماركة والمكونات وأصلي 100% من مهووس]",
  "url_slug": "[brand-perfume-concentration-size-gender-mahwous]",
  "alt_text": "[وصف دقيق لصورة زجاجة العطر]",
  "tags": "[10 كلمات مفتاحية مفصولة بفواصل: الماركة، الاسم، المكونات، عطور مهووس]"
}"""


def _parse_seo_json_block(text: str):
    """يفصل نص الوصف عن كتلة JSON النهائية (page_title / meta_description / …)."""
    if not text or not str(text).strip():
        return "", {}
    t = str(text).strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```\s*$", t)
    if m:
        try:
            j = json.loads(m.group(1).strip())
            if isinstance(j, dict) and any(k in j for k in ("page_title", "meta_description", "url_slug")):
                return t[: m.start()].strip(), j
        except Exception:
            pass
    last = t.rfind("\n{")
    if last == -1:
        last = t.rfind("{")
    if last != -1:
        tail = t[last:]
        depth = 0
        end = None
        for i, c in enumerate(tail):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            try:
                j = json.loads(tail[:end])
                if isinstance(j, dict):
                    return t[:last].strip(), j
            except Exception:
                pass
    return t, {}


def auto_infer_category(product_name: str, gender_hint: str = "") -> str:
    """مسار تصنيف سلة تلقائي من الاسم والجنس."""
    s = f"{product_name} {gender_hint}".lower()
    if any(x in s for x in ("نسائي", "نساء", "للنساء", "women", "female", "lady")):
        return "العطور > عطور نسائية"
    if any(x in s for x in ("رجالي", "رجال", "للرجال", "men", "homme", "male")):
        return "العطور > عطور رجالية"
    if any(x in s for x in ("للجنسين", "unisex", "الجنسين")):
        return "العطور > عطور للجنسين"
    return "العطور > عطور رجالية"


# ══ خبير وصف مهووس — توليد لوصف سلة + SEO ══════════════════════════════════
def generate_mahwous_description(product_name, price, fragrantica_data=None, extra_info=None, return_seo=False):
    """
    يولّد وصفاً بلهجة سلة الشامل (شاعري، سعودي) + JSON SEO في النهاية.
    MAHWOUS_EXPERT_SYSTEM يبقى مرجعاً قديماً؛ التوليد الفعلي يستخدم MAHWOUS_SALLA_PROMPT.
    """
    frag_info = ""
    if fragrantica_data and fragrantica_data.get("success"):
        def _to_list(v):
            """يحوّل أي قيمة (str/list/None) إلى list بأمان."""
            if isinstance(v, list):   return v
            if isinstance(v, str):    return [x.strip() for x in v.split(",") if x.strip()]
            return []
        top  = ", ".join(_to_list(fragrantica_data.get("top_notes",    []))[:5])
        mid  = ", ".join(_to_list(fragrantica_data.get("middle_notes", []))[:5])
        base = ", ".join(_to_list(fragrantica_data.get("base_notes",   []))[:5])
        desc = fragrantica_data.get("description_ar", "")
        brand = fragrantica_data.get("brand", "")
        ptype = fragrantica_data.get("type", "")
        size = fragrantica_data.get("size", "")
        year = fragrantica_data.get("year", "")
        designer = fragrantica_data.get("designer", "")
        family = fragrantica_data.get("fragrance_family", "")
        frag_url = fragrantica_data.get("fragrantica_url", "")

        frag_info = f"""
معلومات من Fragrantica Arabia (استخدمها فقط — لا تختلق غيرها):
- الماركة: {brand}
- المصمم: {designer}
- سنة الإصدار: {year}
- العائلة العطرية: {family}
- الحجم: {size}
- التركيز: {ptype}
- النفحات العليا: {top}
- النفحات الوسطى: {mid}
- النفحات الأساسية: {base}
- الوصف المرجعي: {desc}
- رابط Fragrantica: {frag_url}"""

    extra = ""
    if extra_info:
        extra = f"\nمعلومات إضافية: {extra_info}"

    # ── روابط حقيقية من mahwous.com ─────────────────────────────────────
    brand_url = "https://mahwous.com/brands"
    category_url = "https://mahwous.com"
    try:
        from utils.mahwous_links import lookup_brand_url, lookup_category_url_for_perfume
        _brand_name = ""
        if fragrantica_data and fragrantica_data.get("brand"):
            _brand_name = fragrantica_data["brand"]
        elif extra_info:
            _brand_name = str(extra_info)
        _bu = lookup_brand_url(_brand_name or product_name)
        if _bu:
            brand_url = _bu
        # كشف الجنس من اسم المنتج
        _gender = ""
        _pn_lower = product_name.lower()
        if any(w in _pn_lower for w in ("نسائي", "نساء", "women", "femme", "lady")):
            _gender = "نسائي"
        elif any(w in _pn_lower for w in ("رجالي", "رجال", "men", "homme")):
            _gender = "رجالي"
        _cu = lookup_category_url_for_perfume(_gender)
        if _cu:
            category_url = _cu
    except Exception:
        pass

    prompt = f"""اكتب وصفاً كاملاً للعطر وفق التعليمات والهيكل أعلاه (العنوان الجذاب ثم الأقسام 1–7).

**اسم المنتج:** {product_name}
**السعر المرجعي للبيع:** {price:.0f} ريال سعودي
{frag_info}{extra}

**روابط مهووس الحقيقية (استخدمها في قسم "اكتشف المزيد"):**
- رابط الماركة: {brand_url}
- رابط التصنيف: {category_url}

الطول: تقريباً 800–1500 كلمة، Markdown، بدون إيموجي.
أكد الأصالة 100% مرة واحدة على الأقل بصيغة مهنية.
أنهِ النص بكتلة JSON لحقول SEO كما طُلب (page_title, meta_description, url_slug, alt_text, tags) فقط دون أي نص بعد JSON."""

    txt = _call_gemini(prompt, MAHWOUS_SALLA_PROMPT, grounding=not bool(frag_info), max_tokens=8192)
    if not txt:
        txt = _call_gemini(prompt, MAHWOUS_SALLA_PROMPT, grounding=False, max_tokens=8192)
    if not txt:
        txt = _call_openrouter(prompt, MAHWOUS_SALLA_PROMPT)
    if not txt:
        txt = _call_cohere(prompt, MAHWOUS_SALLA_PROMPT)

    if not txt:
        fb = (
            f"## {product_name}\n\nعطر أصلي 100% متوفر في مهووس.\n\n**السعر:** {price:.0f} ر.س\n\n"
            f'{{"page_title":"{product_name[:80]}","meta_description":"عطر أصلي من مهووس","url_slug":"","alt_text":"","tags":"عطور"}}'
        )
        body, seo = _parse_seo_json_block(fb)
        if return_seo:
            return {"body": body, "seo": seo, "raw": fb}
        return body

    body, seo = _parse_seo_json_block(txt)
    if return_seo:
        return {"body": body, "seo": seo, "raw": txt}
    return body if body else txt

# ══ تحقق منتج + تحديد القسم الصحيح ════════════════════════════════════════
def verify_match(p1, p2, pr1=0, pr2=0):
    diff = pr1 - pr2 if pr1 > 0 and pr2 > 0 else 0
    if pr1 > 0 and pr2 > 0:
        if diff > 10:     expected = "سعر اعلى"
        elif diff < -10:  expected = "سعر اقل"
        else:             expected = "موافق"
    else:
        expected = "تحت المراجعة"

    prompt = f"""تحقق من تطابق هذين المنتجين بدقة متناهية (99.9%):
منتج 1 (مهووس): {p1} | السعر: {pr1:.0f} ريال
منتج 2 (المنافس): {p2} | السعر: {pr2:.0f} ريال

قواعد المطابقة المنطقية (صارمة):
1. الماركة متطابقة تماماً.
2. خط العطر متطابق (Sauvage ≠ Sauvage Elixir).
3. الحجم بالمل متطابق — **50 مل مقابل 100 مل = مطابقة 0%** حتى لو تطابق الاسم.
4. التركيز متطابق (EDP ≠ Parfum ≠ EDT) — مثال: **Sauvage EDP ≠ Sauvage Parfum**.
5. الجنس متطابق (Men ≠ Women).
6. فئة المنتج: عطر مقابل عطر فقط — **لوشن، ديودرنت، شامبو، جل استحمام، كريم، طقم هدايا = ليسوا عطوراً** → match:false.
7. Flanker check: Sauvage ≠ Sauvage Elixir, Eros ≠ Eros Flame, La Nuit ≠ La Nuit Intense → match:false.

إذا تعذر تحقق أي شرط أعلاه، فالمطابقة **false** وconfidence منخفضة.

إذا كانت كل الشروط أعلاه متوفرة، أجب بـ:
- القسم الصحيح = {expected}
خلاف ذلك، أجب بـ:
- القسم الصحيح = مفقود"""

    sys = PAGE_PROMPTS["verify"]
    txt = _call_gemini(prompt, sys, temperature=0.1) or _call_openrouter(prompt, sys)
    if not txt:
        return {"success":False,"match":False,"confidence":0,"reason":"فشل AI","correct_section":"تحت المراجعة","suggested_price":0}
    data = _parse_json(txt)
    if data:
        sec = data.get("correct_section","")
        if "اعلى" in sec or "أعلى" in sec: data["correct_section"] = "سعر اعلى"
        elif "اقل" in sec or "أقل" in sec:  data["correct_section"] = "سعر اقل"
        elif "موافق" in sec:                 data["correct_section"] = "موافق"
        elif "مفقود" in sec:                 data["correct_section"] = "مفقود"
        else: data["correct_section"] = expected if data.get("match") else "مفقود"
        return {"success":True, **data}
    # ✅ إصلاح: لا نحكم بالتطابق من وجود كلمة "نعم"/"true" عشوائية في نص فاشل
    # (كان يُعيد false-positive عند أي هلوسة من النموذج)
    return {
        "success": True,
        "match": False,
        "confidence": 0,
        "reason": f"فشل تحليل JSON من الـ AI — النص الخام: {txt[:200]}",
        "correct_section": "تحت المراجعة",
        "suggested_price": 0,
    }

# ══ إعادة تصنيف قسم "تحت المراجعة" ════════════════════════════════════════
def reclassify_review_items(items):
    if not items: return []
    lines = []
    for i, it in enumerate(items):
        diff = it.get("our_price",0) - it.get("comp_price",0)
        lines.append(f"[{i+1}] منتجنا: {it['our']} ({it.get('our_price',0):.0f}ر.س)"
                     f" vs منافس: {it['comp']} ({it.get('comp_price',0):.0f}ر.س) | فرق: {diff:+.0f}ر.س")
    prompt = f"""حلل هذه المنتجات وحدد القسم الصحيح لكل منها:
{chr(10).join(lines)}
«نفس المنتج» = نفس SKU (ماركة + خط + حجم مل + تركيز). اختلاف حجم أو تركيز → ليس نفس المنتج → مفقود/مراجعة.
- سعر اعلى: نفس المنتج (SKU) + سعرنا أعلى بـ10+ ريال
- سعر اقل: نفس المنتج (SKU) + سعرنا أقل بـ10+ ريال
- موافق: نفس المنتج + فرق 10 ريال أو أقل
- مفقود: ليسا نفس المنتج (مثلاً حجم/تركيز مختلف)"""
    sys = PAGE_PROMPTS["reclassify"]
    txt = _call_gemini(prompt, sys, temperature=0.1) or _call_openrouter(prompt, sys)
    if not txt: return []
    data = _parse_json(txt)
    if data and "results" in data:
        for r in data["results"]:
            try:
                r["idx"] = int(r.get("idx", 0) or 0)
            except Exception:
                r["idx"] = 0
            sec = r.get("section","")
            if "اعلى" in sec or "أعلى" in sec: r["section"] = "🔴 سعر أعلى"
            elif "اقل" in sec or "أقل" in sec:  r["section"] = "🟢 سعر أقل"
            elif "موافق" in sec:                 r["section"] = "✅ موافق"
            elif "مفقود" in sec:                 r["section"] = "🔵 مفقود"
            else:                                 r["section"] = "⚠️ تحت المراجعة"
        return data["results"]
    return []


def auto_resolve_review_v2(review_df, batch_size=5):
    """
    v34: تحليل ذكي لمنتجات 'تحت المراجعة' (60-84%)
    يرسل دفعات من 5 منتجات لـ AI لاتخاذ قرار: تطابق/لا تطابق
    يُرجع dict: {index: {"decision": "🔴/🟢/✅/⚠️", "confidence": int, "reason": str}}
    """
    if review_df is None or review_df.empty:
        return {}
    results = {}
    items = []
    for idx, row in review_df.iterrows():
        our_name = str(row.get("اسم المنتج", row.get("منتجنا", row.get("المنتج", "")))).strip()
        comp_name = str(row.get("منتج المنافس", row.get("المنافس", row.get("منتج_المنافس", "")))).strip()
        our_price = float(row.get("سعر المنتج", row.get("سعرنا", row.get("السعر", 0))) or 0)
        comp_price = float(row.get("سعر المنافس", row.get("سعر_المنافس", 0)) or 0)
        score = float(row.get("نسبة التطابق", row.get("نسبة_التطابق", 0)) or 0)
        items.append({
            "idx": idx,
            "our": our_name,
            "comp": comp_name,
            "our_price": our_price,
            "comp_price": comp_price,
            "score": score,
        })
    # معالجة بدفعات
    for i in range(0, len(items), batch_size):
        batch = items[i:i+batch_size]
        lines = []
        for j, it in enumerate(batch):
            diff = it["our_price"] - it["comp_price"] if it["our_price"] > 0 and it["comp_price"] > 0 else 0
            lines.append(
                f'[{j+1}] منتجنا: "{it["our"]}" ({it["our_price"]:.0f}ر.س) '
                f'vs المنافس: "{it["comp"]}" ({it["comp_price"]:.0f}ر.س) '
                f'| تطابق: {it["score"]:.0f}% | فرق: {diff:+.0f}ر.س'
            )
        prompt = f"""حلل هذه المنتجات المشكوك بها وحدد لكل واحد:
1. هل هما **نفس العطر فعلاً** (نفس الماركة + نفس الخط + نفس الحجم مل + نفس التركيز)؟
2. إذا نعم: ما القرار السعري؟
3. إذا لا: لماذا (اختلاف حجم/تركيز/خط/فئة)؟
المنتجات:
{chr(10).join(lines)}
⚠️ تذكر:
- عطر ≠ لوشن/ديودرنت/شامبو/طقم
- 50ml ≠ 100ml حتى لو نفس الاسم
- EDP ≠ EDT ≠ Parfum حتى لو نفس العطر
- Eros ≠ Eros Flame, Sauvage ≠ Sauvage Elixir
أجب JSON فقط:
{{"results": [
  {{"idx": 1, "match": true/false, "confidence": 0-100,
   "decision": "🔴 سعر أعلى" أو "🟢 سعر أقل" أو "✅ موافق" أو "⚪ مستبعد",
   "reason": "سبب قصير"}}
]}}
إذا match=false → decision="⚪ مستبعد"
"""
        sys = PAGE_PROMPTS["reclassify"]
        txt = _call_openrouter(prompt, sys)
        if not txt:
            txt = _call_gemini(prompt, sys, temperature=0.1)
        if not txt:
            continue
        data = _parse_json(txt)
        if data and "results" in data:
            for r in data["results"]:
                try:
                    batch_idx = int(r.get("idx", 0)) - 1
                    if 0 <= batch_idx < len(batch):
                        real_idx = batch[batch_idx]["idx"]
                        results[real_idx] = {
                            "decision": r.get("decision", "⚠️ تحت المراجعة"),
                            "confidence": min(100, max(0, int(r.get("confidence", 0)))),
                            "reason": str(r.get("reason", "")),
                            "match": bool(r.get("match", False)),
                        }
                except (ValueError, IndexError):
                    continue
    return results

# ══ بحث أسعار السوق ══════════════════════════════════════════════════════
def search_market_price(product_name, our_price=0):
    # البحث في أشهر المتاجر السعودية (سلة، زد، نايس ون، قولدن سنت، خبير العطور)
    queries = [
        f"سعر {product_name} السعودية نايس ون قولدن سنت سلة",
        f"سعر {product_name} في المتاجر السعودية 2026",
        f"مقارنة أسعار {product_name} السعودية",
        f"{product_name} price Saudi Arabia perfume shop",
    ]
    all_results = []
    for q in queries[:3]:  # استخدام أول 3 استعلامات
        ddg = _search_ddg(q)
        if ddg: all_results.extend(ddg[:3])
    
    web_ctx = "\n".join(f"- {r['title']}: {r['snippet'][:120]}" for r in all_results) if all_results else ""
    
    prompt = f"""تحليل سوق دقيق للمنتج في السعودية (مارس 2026):
المنتج: {product_name}
سعرنا الحالي: {our_price:.0f} ريال

المعلومات المستخرجة من الويب:
{web_ctx}

المطلوب تحليل JSON مفصل:
1. متوسط السعر في السوق السعودي.
2. أرخص سعر متاح حالياً واسم المتجر.
3. قائمة المنافسين المباشرين وأسعارهم (نايس ون، قولدن سنت، لودوريه، بيوتي ستور، إلخ).
4. حالة التوفر (متوفر/غير متوفر).
5. توصية تسعير ذكية لمتجر مهووس ليكون الأكثر تنافسية.
6. نسبة الثقة في البيانات (0-100)."""
    sys = PAGE_PROMPTS["market_search"]
    txt = _call_gemini(prompt, sys, grounding=True)
    if not txt: txt = _call_gemini(prompt, sys)
    if not txt: txt = _call_openrouter(prompt, sys)
    if not txt: return {"success":False,"market_price":0}
    data = _parse_json(txt)
    if data:
        data["web_context"] = web_ctx
        return {"success":True, **data}
    return {"success":True,"market_price":our_price,"recommendation":txt[:400],"web_context":web_ctx}

# ══ تحليل عميق ══════════════════════════════════════════════════════════════
def ai_deep_analysis(our_product, our_price, comp_product, comp_price, section="general", brand=""):
    diff = our_price - comp_price if our_price > 0 and comp_price > 0 else 0
    diff_pct = (abs(diff)/comp_price*100) if comp_price > 0 else 0
    ddg = _search_ddg(f"سعر {our_product} السعودية")
    web_ctx = "\n".join(f"- {r['snippet'][:80]}" for r in ddg[:2]) if ddg else ""
    guidance = {
        "🔴 سعر أعلى": f"سعرنا اعلى بـ{diff:.0f}ريال ({diff_pct:.1f}%). هل يجب خفضه؟",
        "🟢 سعر أقل":  f"سعرنا اقل بـ{abs(diff):.0f}ريال ({diff_pct:.1f}%). كم يمكن رفعه؟",
        "✅ موافق":     "السعر تنافسي. هل نحافظ عليه؟",
        "⚠️ تحت المراجعة": "المطابقة غير مؤكدة. هل هما نفس المنتج؟",
    }.get(section, "")
    prompt = f"""تحليل تسعير عميق:
منتجنا: {our_product} | سعرنا: {our_price:.0f} ريال
المنافس: {comp_product} | سعره: {comp_price:.0f} ريال
الفرق: {diff:+.0f} ريال | {diff_pct:.1f}% | {guidance}
{f"معلومات السوق:{chr(10)}{web_ctx}" if web_ctx else ""}
اجب بتقرير مختصر: هل المطابقة صحيحة؟ السعر المقترح بالرقم؟ الاجراء الفوري؟"""
    txt = _call_gemini(prompt, grounding=bool(web_ctx)) or _call_openrouter(prompt)
    if txt: return {"success":True,"response":txt,"source":"Gemini" + (" + ويب" if web_ctx else "")}
    return {"success":False,"response":"فشل التحليل"}

# ══ بحث mahwous.com ══════════════════════════════════════════════════════════
def search_mahwous(product_name):
    ddg = _search_ddg(f"site:mahwous.com {product_name}")
    web_ctx = "\n".join(r["snippet"][:100] for r in ddg[:2]) if ddg else ""
    prompt = f"""هل العطر {product_name} متوفر في متجر مهووس؟
{f"نتائج:{chr(10)}{web_ctx}" if web_ctx else ""}
اجب JSON: {{"likely_available":true/false,"confidence":0-100,"similar_products":[],
"add_recommendation":"عالية/متوسطة/منخفضة","reason":"","suggested_price":0}}"""
    txt = _call_gemini(prompt, grounding=True) or _call_gemini(prompt)
    if not txt: return {"success":False}
    data = _parse_json(txt)
    if data: return {"success":True, **data}
    return {"success":True,"likely_available":False,"confidence":50,"reason":txt[:150]}

# ══ تحقق ذكي من التكرار (Phase 3 — RapidFuzz → AI Funnel) ═══════════════════
def _dedup_cache_key(missing_name: str, candidates: list) -> str:
    """مفتاح cache مستقر لـ ai_verify_dedup (يعتمد الاسم + أسماء المرشحين فقط،
    مثل _ai_batch — درجة التشابه fuzzy لا تدخل المفتاح)."""
    import hashlib as _hl
    payload = {
        "m": str(missing_name).strip().lower(),
        "c": sorted(str(c.get("name", "")).strip().lower() for c in candidates[:5]),
    }
    return "dedup:" + _hl.md5(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def ai_verify_dedup(missing_name: str, candidates: list[dict]) -> dict:
    """
    يتحقق مما إذا كان المنتج المفقود مطابقاً لأحد المرشحين القلائل.

    Parameters
    ----------
    missing_name : str
        اسم المنتج المفقود من المنافس.
    candidates : list[dict]
        قائمة بحد أقصى 5 مرشحين، كل واحد: {"name": str, "score": float}

    Returns
    -------
    dict: {"match": bool, "matched_name": str, "confidence": int, "reason": str}
    """
    if not missing_name or not candidates:
        return {"match": False, "matched_name": "", "confidence": 0, "reason": "بيانات ناقصة"}

    # ── cache: تفادي إعادة طلب AI لنفس (المفقود + المرشحين) ──────────────
    # نعيد استخدام مخزن engine الدائم (match_cache_v22.db) نفسه الذي يستخدمه
    # _ai_batch، ببادئة "dedup:" لفصل المفاتيح. نخزّن النتائج المؤكدة فقط؛
    # لا نخزّن فشل/غموض AI حتى لا يتجمّد قرار خاطئ في الـ cache.
    _ck = _dedup_cache_key(missing_name, candidates)
    try:
        from engines.engine import _cget as _dc_get, _cset as _dc_set
    except Exception:
        _dc_get = _dc_set = None
    if _dc_get is not None:
        _cached = _dc_get(_ck)
        if isinstance(_cached, dict) and "match" in _cached:
            return _cached

    cand_lines = "\n".join(
        f"  {i+1}. {c['name']} (تشابه {c['score']:.0f}%)"
        for i, c in enumerate(candidates[:5])
    )
    prompt = (
        f"أنت خبير عطور. هل المنتج التالي من المنافس هو نفسه أحد منتجاتنا؟\n\n"
        f"المنتج المفقود: {missing_name}\n\n"
        f"المرشحون من كتالوجنا:\n{cand_lines}\n\n"
        f"قارن الاسم والماركة والحجم والتركيز بدقة.\n"
        f"أجب بـ JSON فقط بدون أي نص إضافي:\n"
        f'{{"match": true/false, "matched_index": 0, "confidence": 0-100, "reason": "سبب قصير"}}\n'
        f"matched_index = رقم المرشح (1-{len(candidates[:5])}) أو 0 إذا لا يوجد تطابق."
    )

    result = call_ai(prompt, "missing", json_mode=True)
    if not result.get("success"):
        return {"match": False, "matched_name": "", "confidence": 0, "reason": "فشل AI"}

    resp = result.get("response", "")

    # تنظيف: إزالة markdown code blocks
    import re as _re
    resp_clean = _re.sub(r"```(?:json)?\s*", "", str(resp)).strip().rstrip("`").strip()

    # محاولة تحليل JSON
    import json as _json
    data = None
    try:
        data = _json.loads(resp_clean)
    except (ValueError, TypeError):
        # محاولة استخراج JSON من النص
        m = _re.search(r"\{[^{}]+\}", resp_clean)
        if m:
            try:
                data = _json.loads(m.group())
            except (ValueError, TypeError):
                pass

    if not data or not isinstance(data, dict):
        return {"match": False, "matched_name": "", "confidence": 0, "reason": f"رد AI غير مفهوم: {str(resp)[:80]}"}

    is_match = bool(data.get("match", False))
    matched_idx = int(data.get("matched_index", 0))
    confidence = min(100, max(0, int(data.get("confidence", 0))))
    reason = str(data.get("reason", ""))

    matched_name = ""
    if is_match and 1 <= matched_idx <= len(candidates):
        matched_name = candidates[matched_idx - 1].get("name", "")

    out = {
        "match": is_match,
        "matched_name": matched_name,
        "confidence": confidence,
        "reason": reason,
    }
    # خزّن النتيجة المؤكدة فقط (وصلنا هنا = AI نجح وحُلّل JSON بنجاح)
    if _dc_set is not None:
        try:
            _dc_set(_ck, out)
        except Exception:
            pass
    return out


# ══ تحليل مجمع ════════════════════════════════════════════════════════════════
def bulk_verify(items, section="general"):
    if not items: return {"success":False,"response":"لا توجد منتجات"}
    lines = "\n".join(
        f"{i+1}. {it.get('our','')} vs {it.get('comp','')} | "
        f"سعرنا: {it.get('our_price',0):.0f} | منافس: {it.get('comp_price',0):.0f} | "
        f"فرق: {it.get('our_price',0)-it.get('comp_price',0):+.0f}"
        for i,it in enumerate(items))
    instructions = {
        "price_raise": "سعرنا اعلى. لكل منتج: هل المطابقة صحيحة؟ هل نخفض؟ السعر المقترح.",
        "price_lower": "سعرنا اقل = ربح ضائع. لكل منتج: هل يمكن رفعه؟ السعر الامثل.",
        "review": "مطابقات غير مؤكدة. لكل منتج: هل هما نفس العطر فعلا؟ نعم/لا/غير متاكد.",
        "approved": "منتجات موافق عليها. راجعها وتاكد انها لا تزال تنافسية.",
    }
    prompt = f"{instructions.get(section,'حلل واعط توصية.')}\n\nالمنتجات:\n{lines}"
    return call_ai(prompt, section)

# ══ معالجة النص الملصوق ═══════════════════════════════════════════════════
def analyze_paste(text, context=""):
    prompt = f"""المستخدم لصق هذا النص:
---
{text[:5000]}
---
حلل واستخرج: قائمة منتجات؟ اسعار؟ اوامر؟ اعط توصيات مفيدة. اجب بالعربية منظم."""
    return call_ai(prompt, "general")

# ══ دوال متوافقة مع app.py ════════════════════════════════════════════════
def chat_with_ai(msg, history=None, ctx=""): return gemini_chat(msg, history, ctx)
def analyze_product(p, price=0): return call_ai(f"حلل: {p} ({price:.0f}ريال)", "general")
def suggest_price(p, comp_price): return call_ai(f"اقترح سعرا لـ {p} بدلا من {comp_price:.0f}ريال", "general")
def process_paste(text): return analyze_paste(text)


# ══════════════════════════════════════════════════════════════════════════
#  محرك إثراء المحتوى التسويقي (Content Enrichment Engine)
#  يولّد وصفاً Markdown مع ربط الماركة والقسم من ملفات المتجر الفعلية
# ══════════════════════════════════════════════════════════════════════════
import pandas as _pd
import os as _os
import functools as _functools

from engines.prompts import (
    SEO_CONTENT_PROMPT,
    SALLA_BRANDS_FILE, SALLA_BRANDS_COL,
    SALLA_CATEGORIES_FILE, SALLA_CATEGORIES_COL,
    BRANDS_CSV_FILE, BRANDS_CSV_COL,
    CATEGORIES_CSV_FILE, CATEGORIES_CSV_COL,
)


def _load_catalog_by_colname(csv_path: str, col_name: str) -> list[str]:
    """
    يقرأ عمود CSV باسمه الصريح (للملفات الرسمية من سلة).
    يدعم الترميزات العربية الشائعة.
    """
    for enc in ("utf-8-sig", "utf-8", "cp1256"):
        try:
            df = _pd.read_csv(csv_path, header=0, encoding=enc)
            if col_name in df.columns:
                return [str(v).strip() for v in df[col_name].dropna().tolist()
                        if str(v).strip() and str(v) not in ("nan", "None")]
        except Exception:
            continue
    return []


def _load_catalog_list(csv_path: str, col_idx: int) -> list[str]:
    """
    يقرأ عمود CSV برقمه (للملفات الاحتياطية العامة).
    يدعم الترميزات العربية الشائعة (UTF-8 / cp1256).
    """
    for enc in ("utf-8-sig", "utf-8", "cp1256"):
        try:
            df = _pd.read_csv(csv_path, header=0, encoding=enc)
            col = df.iloc[:, col_idx]
            return [str(v).strip() for v in col.dropna().tolist()
                    if str(v).strip() and str(v) not in ("nan", "None")]
        except Exception:
            continue
    return []


def _find_catalog_file(salla_filename: str, fallback_filename: str) -> tuple[str, bool]:
    """
    يحدّد مسار ملف الكتالوج بالأولوية التالية:
    1. ملف سلة الرسمي في DATA_DIR (Railway Volume)
    2. ملف سلة الرسمي في جذر المشروع (للتطوير المحلي)
    3. الملف الاحتياطي عبر get_catalog_data_path

    يُعيد (المسار, هل_هو_ملف_سلة)
    """
    import os as _os_local
    from utils.data_paths import get_catalog_data_path

    # 1. ملف سلة في DATA_DIR
    data_dir = (_os_local.environ.get("DATA_DIR") or "").strip()
    if data_dir:
        salla_path = _os_local.path.join(data_dir, salla_filename)
        if _os_local.path.exists(salla_path):
            return salla_path, True

    # 2. ملف سلة في جذر المشروع (بجانب app.py)
    root = _os_local.path.dirname(_os_local.path.dirname(_os_local.path.abspath(__file__)))
    salla_root_path = _os_local.path.join(root, salla_filename)
    if _os_local.path.exists(salla_root_path):
        return salla_root_path, True

    # 3. الملف الاحتياطي
    return get_catalog_data_path(fallback_filename), False


def _resolve_catalog_paths() -> tuple[str, str]:
    """يحدد مسار brands.csv و categories.csv عبر data_paths (ملفات احتياطية)."""
    from utils.data_paths import get_catalog_data_path
    return (
        get_catalog_data_path(BRANDS_CSV_FILE),
        get_catalog_data_path(CATEGORIES_CSV_FILE),
    )


def _build_brands_list() -> str:
    """
    يبني قائمة الماركات بأولوية: ملف سلة الرسمي → الملف الاحتياطي.
    النتيجة مُخزَّنة في ذاكرة العملية (LRU cache) — لا يُعيد قراءة الملف مع كل طلب.
    استدعِ `_build_brands_list.cache_clear()` إذا أردت إعادة القراءة (مثلاً بعد رفع ملف جديد).
    """
    path, is_salla = _find_catalog_file(SALLA_BRANDS_FILE, BRANDS_CSV_FILE)
    if is_salla:
        items = _load_catalog_by_colname(path, SALLA_BRANDS_COL)
    else:
        items = _load_catalog_list(path, BRANDS_CSV_COL)
    if items:
        return "\n".join(f"- {b}" for b in items)
    return "⚠️ لم يُعثر على ملف الماركات — يرجى رفع «ماركات مهووس.csv» في مجلد /data"


# ── ذاكرة تخزين مؤقت (TTL 6 ساعات) تحمي الخادم من إعادة قراءة الملفات ──
@_functools.lru_cache(maxsize=1)
def _brands_list_cached() -> str:
    return _build_brands_list()


def _build_categories_list() -> str:
    """
    يبني قائمة الأقسام بأولوية: ملف سلة الرسمي → الملف الاحتياطي.
    """
    path, is_salla = _find_catalog_file(SALLA_CATEGORIES_FILE, CATEGORIES_CSV_FILE)
    if is_salla:
        items = _load_catalog_by_colname(path, SALLA_CATEGORIES_COL)
    else:
        items = _load_catalog_list(path, CATEGORIES_CSV_COL)
    if items:
        return "\n".join(f"- {c}" for c in items)
    return "⚠️ لم يُعثر على ملف الأقسام — يرجى رفع «تصنيفات مهووس.csv» في مجلد /data"


@_functools.lru_cache(maxsize=1)
def _categories_list_cached() -> str:
    return _build_categories_list()


def clear_catalog_cache() -> None:
    """
    يمسح ذاكرة التخزين المؤقت لقوائم الماركات والأقسام.
    استدعِها بعد رفع ملفات جديدة من الواجهة.
    """
    _brands_list_cached.cache_clear()
    _categories_list_cached.cache_clear()


def generate_seo_description(raw_product_data: str) -> dict:
    """
    توليد الوصف التسويقي SEO بتنسيق Markdown مع ربط:
    - exact_brand    : الماركة المطابقة تماماً من brands.csv
    - exact_category : القسم المطابق تماماً من categories.csv
    - markdown_desc  : الوصف الجاهز

    يستخدم Gemini → OpenRouter → Cohere بالتتابع
    (نفس منطق call_ai في هذا الملف).

    المعاملات:
      raw_product_data : نص خام يصف المنتج (اسم، سعر، URL، إلخ)

    يُعيد dict:
      {"exact_brand": str, "exact_category": str, "markdown_desc": str}
      أو {"error": str} عند الفشل الكامل
    """
    if not raw_product_data or not raw_product_data.strip():
        return {"error": "raw_product_data فارغ — لا شيء لتوليده"}

    prompt = SEO_CONTENT_PROMPT.format(
        brands_list=_brands_list_cached(),
        categories_list=_categories_list_cached(),
        raw_product_data=raw_product_data.strip()[:4000],  # حد آمن
    )

    # حرارة 0.1 (شبه حتمي) لضمان النسخ الحرفي من قوائم سلة
    raw_text = _call_gemini(prompt, temperature=0.1, max_tokens=2048)
    if not raw_text:
        raw_text = _call_openrouter(prompt)
    if not raw_text:
        raw_text = _call_cohere(prompt)

    if not raw_text:
        _log_err("generate_seo_description", "جميع مزودي AI فشلوا")
        return {"error": "فشلت جميع محاولات الاتصال بالذكاء الاصطناعي"}

    data = _parse_json(raw_text)
    if not data:
        # إن أخفق JSON نعيد الـ markdown كاملاً بدون ربط
        _log_err("generate_seo_description", f"فشل تحليل JSON — سنعيد النص خاماً: {raw_text[:120]}")
        return {
            "exact_brand": "",
            "exact_category": "",
            "suggested_new_brand": "",
            "markdown_desc": raw_text.strip(),
            "warning": "JSON parse failed — returned raw text",
        }

    # ── التقاط الماركات المفقودة (Auto-Capture Missing Brands) ─────────────
    suggested_brand = str(data.get("suggested_new_brand", "") or "").strip()
    if suggested_brand:
        from utils.data_paths import get_catalog_data_path
        _missing_file = get_catalog_data_path("missing_brands.txt")
        try:
            # قراءة الماركات المسجلة مسبقاً لمنع التكرار
            _existing: set[str] = set()
            if _os.path.exists(_missing_file):
                with open(_missing_file, "r", encoding="utf-8") as _fh:
                    _existing = {ln.strip() for ln in _fh if ln.strip()}
            # تسجيل الماركة فقط إذا لم تكن مسجلة من قبل
            if suggested_brand not in _existing:
                _os.makedirs(_os.path.dirname(_missing_file), exist_ok=True)
                with open(_missing_file, "a", encoding="utf-8") as _fh:
                    _fh.write(f"{suggested_brand}\n")
        except Exception as _capture_err:
            _log_err("generate_seo_description", f"فشل حفظ الماركة المقترحة: {_capture_err}")
    # ────────────────────────────────────────────────────────────────────────

    return {
        "exact_brand":         str(data.get("exact_brand", "") or "").strip(),
        "exact_category":      str(data.get("exact_category", "") or "").strip(),
        "suggested_new_brand": suggested_brand,
        "markdown_desc":       str(data.get("markdown_desc", "") or "").strip(),
    }


def get_catalog_status() -> dict:
    """
    يعيد حالة ملفات الكتالوج (للعرض في واجهة الإعدادات):
    - ملفات سلة الرسمية (إن وُجدت)
    - الملفات الاحتياطية
    - missing_brands.txt
    """
    from utils.data_paths import get_catalog_data_path

    def _stat_salla(salla_file: str, salla_col: str, fallback_file: str, fallback_col_idx: int) -> dict:
        path, is_salla = _find_catalog_file(salla_file, fallback_file)
        source = "سلة (رسمي)" if is_salla else "احتياطي (generic)"
        if not _os.path.exists(path):
            return {"found": False, "path": path, "source": source, "count": 0, "sample": []}
        if is_salla:
            items = _load_catalog_by_colname(path, salla_col)
        else:
            items = _load_catalog_list(path, fallback_col_idx)
        return {"found": True, "path": path, "source": source,
                "count": len(items), "sample": items[:5]}

    # حالة missing_brands.txt
    missing_path = get_catalog_data_path("missing_brands.txt")
    if _os.path.exists(missing_path):
        try:
            with open(missing_path, "r", encoding="utf-8") as _fh:
                _mb = [ln.strip() for ln in _fh if ln.strip()]
            missing_stat = {"found": True, "path": missing_path,
                            "count": len(_mb), "sample": _mb[:10]}
        except Exception:
            missing_stat = {"found": True, "path": missing_path, "count": -1}
    else:
        missing_stat = {"found": False, "path": missing_path, "count": 0}

    return {
        "brands":         _stat_salla(SALLA_BRANDS_FILE, SALLA_BRANDS_COL,
                                      BRANDS_CSV_FILE, BRANDS_CSV_COL),
        "categories":     _stat_salla(SALLA_CATEGORIES_FILE, SALLA_CATEGORIES_COL,
                                      CATEGORIES_CSV_FILE, CATEGORIES_CSV_COL),
        "missing_brands": missing_stat,
    }


# ══ الدوال المفقودة للوحدة الخامسة ═══════════════════════════════════════════

# ══ مصنع المنتجات — نظام JSON + وصف HTML صارم لسلة / SEO ═══════════════════
MAGIC_FACTORY_SALLA_SYSTEM = """أنت خبير محتوى تجارة إلكترونية وعطور لمتجر «مهووس» (mahwous.com) في السعودية.
مهمتك: تحويل بيانات منتج مكشوطة (نص خام) إلى مخرجات **JSON صالحة فقط** — بدون ```json``` وبدون أي شرح قبل أو بعد كائن JSON.

## إخراج JSON — المفاتيح الإلزامية (لا تحذف مفتاحاً)
يجب أن يحتوي JSON على هذه المفاتيح بالضبط:
cleaned_title, description_html, brand, category, seo_title, seo_description,
top_notes, heart_notes, base_notes, gender_hint, is_perfume

## 1) cleaned_title
- اسم منتج نظيف للعنوان: عربي و/أو إنجليزي، بدون عبارات مزعجة: تخفيض، خصم، الأكثر مبيعاً، أصلي، عرض، شحن مجاني، لفترة محدودة.

## 2) category — إلزامي وليس فارغاً أبداً لمنتجات العطور
استنتج التصنيف من المدخلات (الجنس، الاسم، المنافس). استخدم **حرفياً** أحد الأشكال التالية فقط (مع الفاصل > والمسافات كما هي):
- "العطور > عطور رجالية"
- "العطور > عطور نسائية"
- "العطور > عطور للجنسين"

إذا كانت المدخلات لا تشير لعطر رغم أنها منتج تجميل/عناية، استخدم تصنيفاً منطقياً تحت «العناية» أو «التجميل» بصيغة مشابهة (قسم رئيسي > قسم فرعي). إن كان المنتج عطراً **لا تترك category فارغاً**.

## 3) brand
- اسم الماركة كما يُعرض للعميل (يمكن عربي | إنجليزي إن كان ذلك منطقياً من المدخلات).
- لا تخترع ماركة إن لم تظهر في المدخلات؛ استخدم أقرب استنتاج معقول من اسم المنتج فقط إن وُجدت إشارة واضحة.

## 4) gender_hint
واحد من: "للرجال" | "للنساء" | "للجنسين" | "" (فارغ فقط إن تعذر الاستنتاج تماماً).

## 5) is_perfume
true إن كان المنتج عطراً (EDP/EDT/Parfum/Cologne… أو سياق واضح)، وإلا false.

## 6) top_notes, heart_notes, base_notes
نص عربي موجز (أو مفصول بفواصل) مستخرج من المدخلات؛ إن لم تُذكر مكونات، اكتب "غير مذكور في المصدر" أو استنتجاً حذراً من العائلة العطرية المعروفة للاسم **دون اختلاق تفاصيل كيميائية دقيقة**.

## 7) seo_title و seo_description
- seo_title: ≤ 60 حرفاً (عربي)، يضم ماركة + اسم عطر/منتج + كلمات بحثية طبيعية.
- seo_description: ≤ 155 حرفاً، يشجع النقر ويذكر الأصالة والفئة.

## 8) description_html — **الجزء الأهم**: HTML فقط، طويل، تسويقي، متوافق SEO
قواعد صارمة:
- **ممنوع** Markdown. **ممنوع** استخدام # للعناوين.
- وسوم مسموحة فقط: h2, h3, p, ul, ol, li, strong, em, br, a.
- التزم **حرفياً** بالترتيب والعناوين النصية التالية (يمكنك إثراء النص داخل الفقرات والقوائم، لكن لا تحذف قسماً ولا تغيّر مستوى العناوين):

(أ) ابدأ بسطر واحد: <h2>اسم العطر أو المنتج بالصيغة الجذابة</h2> (استخدم الاسم من cleaned_title أو المدخلات).

(ب) فقرة افتتاحية واحدة على الأقل: <p>…</p> تسويقية، وتذكر **اسم الماركة** و**اسم العطر/المنتج** داخل <strong>…</strong>.

(ج) <h3>تفاصيل المنتج</h3> ثم <ul><li>…</li></ul> ويجب أن تتضمن القائمة بنوداً واضحة للـ: الماركة، اسم المنتج، الجنس، العائلة العطرية (أو نوع المنتج)، الحجم، التركيز (EDP/EDT/إلخ). استخدم «غير محدد في المصدر» للبند الناقص.

(د) <h3>رحلة العطر — الهرم العطري</h3> ثم فقرة أو قائمة توضح **القمة** و**القلب** و**القاعدة** (للمنتجات غير العطور: حوّل القسم إلى «مكونات الرائحة / الطابع العطري» بذات البنية إن كان معطوراً، أو «مميزات المنتج» بثلاث فقرات فرعية داخل h3 واحد ثم ul).

(هـ) <h3>لماذا تختار هذا العطر؟</h3> ثم <ul> بنقاط تسويقية (ثبات، فوحان، تميز، مناسبة للمناسبات…) — لا تبالغ بأرقام وهمية؛ إن ذكرت أرقاماً فاجعلها نطاقات معقولة (مثلاً 6–10 ساعات) مع صيغة احترافية.

(و) <h3>متى وأين ترتديه؟</h3> ثم <p>…</p> (فصول، أوقات، مناسبات).

(ز) <h3>لمسة خبير من متجر مهووس</h3> ثم <p>…</p> يتضمن: تقييم **الفوحان من 10** و**الثبات من 10** (رقمان صريحان مثل 7/10)، و**نصيحة استخدام** (نقاط النبض، عدد البخات).

(ح) <h3>الأسئلة الشائعة</h3> ثم <ul> تحتوي **3 عناصر li** على الأقل؛ كل li يجب أن يكون سؤالاً ثم إجابة مختصرة داخل نفس العنصر (مثال: <li><strong>السؤال؟</strong> الإجابة…</li>).

(ط) <h3>اكتشف أكثر من مهووس</h3> ثم فقرة فيها **روابط حقيقية** بوسم <a>:
- رابط التصنيف حسب جنس المنتج (استخدم أحد هذه الروابط حسب الاستنتاج):
  • عطور رجالية: href="https://mahwous.com/categories/mens-perfumes"
  • عطور نسائية: href="https://mahwous.com/categories/womens-perfumes"
  • عطور للجنسين: href="https://mahwous.com/categories/unisex-perfumes"
- رابط الماركة: href="https://mahwous.com/brands/BRAND_SLUG" حيث BRAND_SLUG هو **الاسم اللاتيني للماركة** بصيغة slug: أحرف صغيرة إنجليزية، مسافات → شرطة -، إزالة الرموز الخاصة (مثال: Dior → dior، Yves Saint Laurent → yves-saint-laurent). نص الرابط <a> يكون اسم الماركة المعروض للعميل.

## الطول والجودة
- الوصف الإجمالي في description_html يجب أن يكون **طويلاً وغنى** (استهدف ما يعادل 400–900 كلمة من النص الظاهر للمستخدم داخل HTML)، بدون حشو كلمات مفتاحية مكررة بشكل آلي.

## JSON technique
- حقل description_html يجب أن يكون **سلسلة JSON واحدة**؛ استبدل الأسطر الجديدة داخل HTML بـ \\n أو اكتب HTML في سطر متصل مع وسوم صحيحة.
- لا تضع علامات اقتباس غير مهرّبة داخل description_html تكسر JSON؛ استخدم \\" للاقتباس الداخلي إن لزم.
"""


def enhance_competitor_product_for_salla(
    scraped_summary: str,
    url: str = "",
    meta_fallback: str = "",
) -> dict:
    """
    يحسّن بيانات منتج مكشوط من متجر منافس لتصدير سلة (شامل):
    تنظيف العنوان، وصف HTML تسويقي بقالب صارم، ماركة، تصنيف سلة، SEO، وهرم عطري.

    يُعيد dict بمفاتيح:
    cleaned_title, description_html, brand, category, seo_title, seo_description,
    top_notes, heart_notes, base_notes, gender_hint, is_perfume
    """
    defaults = {
        "cleaned_title": "",
        "description_html": "",
        "brand": "",
        "category": "",
        "seo_title": "",
        "seo_description": "",
        "top_notes": "",
        "heart_notes": "",
        "base_notes": "",
        "gender_hint": "",
        "is_perfume": False,
    }
    blob = (scraped_summary or "").strip()
    if not blob and not (meta_fallback or "").strip():
        return defaults

    prompt = (
        f"رابط الصفحة الأصلية للمنافس (للسياق فقط): {url}\n\n"
        f"=== بيانات مستخرجة من الكشط ===\n{blob[:7000]}\n"
    )
    if (meta_fallback or "").strip():
        prompt += f"\n=== وسوم meta / JSON-LD إضافية ===\n{meta_fallback[:2500]}\n"

    prompt += """
=== المطلوب ===
أعد كتابة المنتج لمتجر مهووس وسلة: JSON واحد فقط يلتزم بجميع قواعد النظام (MAGIC_FACTORY).
تأكد من:
1) ملء "category" بأحد مسارات العطور الثلاثة عند كون المنتج عطراً.
2) أن "description_html" يلتزم **حرفياً** بترتيب العناوين h2 ثم h3 المحددة وبمحتوى تسويقي طويل.
3) تضمين روابط mahwous.com للتصنيف وللماركة (slug لاتيني) في القسم الأخير.

أجب بكائن JSON فقط بهذا الشكل (المفاتيح كما هي):
{
  "cleaned_title": "",
  "description_html": "",
  "brand": "",
  "category": "",
  "seo_title": "",
  "seo_description": "",
  "top_notes": "",
  "heart_notes": "",
  "base_notes": "",
  "gender_hint": "",
  "is_perfume": true
}
"""

    raw = (
        _call_gemini(prompt, MAGIC_FACTORY_SALLA_SYSTEM, temperature=0.3, max_tokens=8192)
        or _call_openrouter(prompt, MAGIC_FACTORY_SALLA_SYSTEM)
        or _call_cohere(prompt, MAGIC_FACTORY_SALLA_SYSTEM)
    )
    if not raw:
        return defaults

    data = _parse_json(raw)
    if not isinstance(data, dict):
        return defaults

    out = {**defaults}
    for k in out:
        if k in data:
            v = data[k]
            if k == "is_perfume":
                out[k] = bool(v)
            else:
                out[k] = str(v).strip() if v is not None else ""

    # مطابقة صارمة مع «ماركات مهووس» / «brands.csv» و«تصنيفات مهووس» (منطق التصدير)
    from utils.salla_shamel_export import (
        resolve_brand_for_shamel,
        resolve_category_for_shamel,
    )

    _rb = resolve_brand_for_shamel(out["brand"])
    if _rb:
        out["brand"] = _rb

    _rc = resolve_category_for_shamel(
        out["category"],
        gender_hint=out["gender_hint"],
        product_name_fallback=out["cleaned_title"],
    )
    if _rc:
        out["category"] = _rc
    elif out["is_perfume"] and not out["category"].strip():
        _inferred = auto_infer_category(out["cleaned_title"], out["gender_hint"])
        _rc2 = resolve_category_for_shamel(
            _inferred,
            gender_hint=out["gender_hint"],
            product_name_fallback=out["cleaned_title"],
        )
        out["category"] = _rc2 or _inferred

    return out


def extract_product(text: str) -> dict:
    """
    يستخرج بيانات العطر (اسم، ماركة، حجم، تركيز، جنس، سعر) من نص خام.
    يستخدم _parse_json مباشرةً — لا يعتمد على json_mode.
    مضمون العودة بـ dict حتى عند الفشل.
    """
    defaults = {
        "name": str(text).strip()[:120] if text else "",
        "brand": "", "size": "", "concentration": "",
        "gender": "", "price": 0,
    }
    if not text or not str(text).strip():
        return defaults

    prompt = (
        'استخرج بيانات العطر من النص التالي بدقة وأجب بـ JSON صالح فقط بدون أي نص آخر:\n'
        f'النص: "{str(text).strip()[:500]}"\n\n'
        'الصيغة المطلوبة:\n'
        '{"name":"اسم المنتج","brand":"الماركة بالإنجليزية",'
        '"size":"الحجم مثل 100ml","concentration":"EDP أو EDT أو Parfum",'
        '"gender":"Men أو Women أو Unisex","price":0}'
    )

    sys_prompt = PAGE_PROMPTS.get("general", "")
    raw = (
        _call_gemini(prompt, sys_prompt, temperature=0.1)
        or _call_openrouter(prompt, sys_prompt)
        or _call_cohere(prompt, sys_prompt)
    )

    if raw:
        parsed = _parse_json(raw)
        if isinstance(parsed, dict) and parsed.get("name"):
            defaults.update(parsed)
            try:
                defaults["price"] = float(str(defaults.get("price", 0)).replace(",", "") or 0)
            except (ValueError, TypeError):
                defaults["price"] = 0.0
            return defaults

    return defaults


def fetch_og_image_url(url: str) -> str:
    """
    يجلب رابط صورة Open Graph (og:image) من أي صفحة ويب.
    يستخدم Session مشتركة مع تناوب User-Agent وتقليل الطلبات المكلفة.
    يدعم og:image و twitter:image والروابط النسبية.
    """
    if not url or not str(url).strip().startswith("http"):
        return ""

    target_url = url.strip()
    _headers = {
        "User-Agent": random.choice(_OG_USER_AGENTS),
        "Accept-Language": "ar,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }

    for attempt in range(2):
        verify_ssl = (attempt == 0)
        try:
            r = _OG_SESSION.get(
                target_url,
                headers=_headers,
                timeout=12,
                allow_redirects=True,
                verify=verify_ssl,
            )
            if r.status_code == 403:
                _log_err("fetch_og_image_url", f"محظور (403) من {target_url[:60]}")
                return ""
            if r.status_code == 429:
                _log_err("fetch_og_image_url", f"Rate Limit (429) من {target_url[:60]}")
                return ""
            if r.status_code != 200:
                _log_err("fetch_og_image_url", f"HTTP {r.status_code} من {target_url[:60]}")
                return ""

            html = r.text
            for pat in _OG_PATTERNS:
                m = re.search(pat, html, re.IGNORECASE)
                if m:
                    img = m.group(1).strip()
                    if img.startswith("//"):
                        return "https:" + img
                    if img.startswith("/"):
                        return urljoin(target_url, img)
                    if img.startswith("http"):
                        return img
            return ""
        except requests.exceptions.SSLError:
            if attempt == 0:
                continue
            _log_err("fetch_og_image_url", f"SSL Error نهائي من {target_url[:60]}")
            return ""
        except requests.exceptions.Timeout:
            _log_err("fetch_og_image_url", f"Timeout (12s) من {target_url[:60]}")
            return ""
        except requests.exceptions.ConnectionError as e:
            _log_err("fetch_og_image_url", f"Connection Error من {target_url[:60]}: {str(e)[:60]}")
            return ""
        except Exception as _e:
            _log_err("fetch_og_image_url", f"فشل جلب OG image من {target_url[:60]}: {str(_e)[:80]}")
            return ""

    return ""
