"""
engines/missing_products_engine.py — Smart Missing-Products Extractor (v1.1)
════════════════════════════════════════════════════════════════════════════
يستخرج «المنتجات المفقودة» الحقيقية: متوفرة عند المنافس، غير متوفرة لدينا.

Pipeline:
  1) تطبيع أسماء كتالوجنا (No., أسم المنتج, سعر المنتج)
  2) مطابقة RapidFuzz لكل منتج منافس → ≥85% = مرشح للموجود
  2b) تحقق هيكلي (ماركة + حجم) — إذا اختلف الحجم أو الماركة → مفقود
  3) تحقق AI للحالات الرمادية (70-85%) لمنع False-Positive
  4) إزالة التكرار بين المنافسين (أعلى سعر يبقى)
  5) تصنيف الماركة ضد `ماركات مهووس` → إذا مفقودة → ملف new_brands
  6) تصنيف Category ضد `تصنيفات مهووس` (AI)
  7) توليد وصف بتنسيق mahwous عبر Gemini (grounded)
  8) تصدير: new_products.xlsx + new_brands.csv
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from rapidfuzz import fuzz, process as rf_process

try:
    from engines.ai_engine import _call_gemini
except Exception:
    _call_gemini = None

# ── استيراد دوال الاستخراج الهيكلي من المحرك الرئيسي ──────────────────────
try:
    from engines.engine import extract_brand as _extract_brand
    from engines.engine import extract_size as _extract_size
    from engines.engine import _SYN
except ImportError:
    _extract_brand = None
    _extract_size = None
    _SYN = {}

logger = logging.getLogger("MissingProductsEngine")

# ── Thresholds ─────────────────────────────────────────────────────────────
EXISTS_THRESHOLD   = 85.0   # ≥85% → مرشح للموجود (يحتاج تحقق هيكلي)
UNCERTAIN_LOWER    = 70.0   # 70-85% → AI verify
# <70% → مفقود مؤكد

BRAND_MATCH_THRESHOLD    = 88.0
CATEGORY_MATCH_THRESHOLD = 85.0

_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670]")
_NON_ALNUM_AR = re.compile(r"[^\w\u0600-\u06FF\s]")
_WS = re.compile(r"\s+")

_NOISE_WORDS = {
    "عينة", "او", "أو", "دو", "بارفيوم", "برفيوم", "بارفان",
    "eau", "de", "parfum", "edp", "edt", "toilette", "cologne",
    "ml", "مل", "رجالي", "نسائي", "للرجال", "للنساء", "فرنسي",
}

# ── مرادفات من المحرك الرئيسي لتحسين التطبيع ──────────────────────────────
_LOCAL_SYN = {
    "eau de parfum": "edp", "او دو بارفان": "edp", "بارفان": "edp",
    "eau de toilette": "edt", "او دو تواليت": "edt", "تواليت": "edt",
    "eau de cologne": "edc", "كولون": "edc", "cologne": "edc",
}
if _SYN:
    _LOCAL_SYN.update(_SYN)


def _normalize(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = _ARABIC_DIACRITICS.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ة", "ه").replace("ى", "ي")
    s = _NON_ALNUM_AR.sub(" ", s)
    s = s.lower()
    # تطبيق المرادفات
    for k, v in _LOCAL_SYN.items():
        s = s.replace(k, v)
    parts = [w for w in _WS.split(s) if w and w not in _NOISE_WORDS]
    return " ".join(parts).strip()


def _hash_key(s: str) -> str:
    return hashlib.md5(_normalize(s).encode("utf-8")).hexdigest()[:12]


# ── تحقق هيكلي: ماركة + حجم ────────────────────────────────────────────────
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ml|مل|ملي)", re.I)

def _extract_size_local(name: str) -> float:
    """استخراج الحجم بالمل — يستخدم المحرك الرئيسي أولاً، ثم regex محلي."""
    if _extract_size:
        try:
            sz = _extract_size(name)
            if sz and sz > 0:
                return sz
        except Exception:
            pass
    m = _SIZE_RE.search(name or "")
    return float(m.group(1)) if m else 0.0


def _extract_brand_local(name: str) -> str:
    """استخراج الماركة — يستخدم المحرك الرئيسي أولاً."""
    if _extract_brand:
        try:
            br = _extract_brand(name)
            if br:
                return br.lower().strip()
        except Exception:
            pass
    return ""


def _structural_match(comp_name: str, catalog_name: str) -> bool:
    """
    تحقق هيكلي: هل المنتجان هما فعلاً نفس المنتج؟
    - إذا كلاهما لهما حجم → يجب أن يتطابق الحجم
    - إذا كلاهما لهما ماركة → يجب أن تتطابق الماركة
    
    Returns True إذا المنتجان متطابقان هيكلياً (أو لا يمكن الحكم).
    Returns False إذا مؤكد أنهما مختلفان.
    """
    # فحص الحجم
    comp_sz = _extract_size_local(comp_name)
    cat_sz = _extract_size_local(catalog_name)
    if comp_sz > 0 and cat_sz > 0 and comp_sz != cat_sz:
        return False  # 50ml ≠ 100ml → ليس نفس المنتج

    # فحص الماركة
    comp_br = _extract_brand_local(comp_name)
    cat_br = _extract_brand_local(catalog_name)
    if comp_br and cat_br and comp_br != cat_br:
        return False  # Dior ≠ Chanel → ليس نفس المنتج

    return True  # إما متطابق أو لا يمكن الحكم


# ══════════════════════════════════════════════════════════════════════════
#  Data loaders
# ══════════════════════════════════════════════════════════════════════════
def load_catalog(path: str) -> pd.DataFrame:
    """كتالوجنا: No. | أسم المنتج | سعر المنتج — مع تطبيع اسم."""
    df = pd.read_excel(path) if str(path).lower().endswith(("xlsx", "xls")) \
         else pd.read_csv(path, encoding="utf-8-sig")
    name_col = next((c for c in df.columns if "اسم" in str(c) or "أسم" in str(c) or "name" in str(c).lower()), None)
    no_col   = next((c for c in df.columns if str(c).strip() in ("No.", "NO", "No", "no")), None)
    df = df.rename(columns={name_col: "أسم المنتج"} if name_col else {})
    if no_col and no_col != "No.":
        df = df.rename(columns={no_col: "No."})
    df["_norm"] = df["أسم المنتج"].fillna("").astype(str).apply(_normalize)
    df = df[df["_norm"].str.len() > 0].reset_index(drop=True)
    logger.info("📦 كتالوجنا: %d منتج", len(df))
    return df


def load_competitors(paths: List[str]) -> pd.DataFrame:
    """دمج ملفات المنافسين في DataFrame موحّد."""
    frames = []
    for p in paths:
        try:
            df = pd.read_csv(p, encoding="utf-8-sig") if str(p).lower().endswith("csv") \
                 else pd.read_excel(p)
        except Exception as e:
            logger.warning("تعذّر قراءة %s: %s", p, e)
            continue
        name_col  = next((c for c in df.columns if "اسم" in str(c) or "name" in str(c).lower() or "المنتج" in str(c)), None)
        price_col = next((c for c in df.columns if "سعر" in str(c) or "price" in str(c).lower()), None)
        url_col   = next((c for c in df.columns if "رابط" in str(c) or "url" in str(c).lower()), None)
        img_col   = next((c for c in df.columns if "صورة" in str(c) or "image" in str(c).lower()), None)
        if not name_col or not price_col:
            logger.warning("تخطي %s — أعمدة ناقصة", p)
            continue
        comp_name = Path(p).stem
        sub = pd.DataFrame({
            "اسم_المنتج": df[name_col].fillna("").astype(str),
            "السعر":      pd.to_numeric(df[price_col], errors="coerce"),
            "الرابط":     df[url_col].fillna("").astype(str) if url_col else "",
            "الصورة":     df[img_col].fillna("").astype(str) if img_col else "",
            "المنافس":    comp_name,
        })
        frames.append(sub)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if out.empty:
        return out
    out["_norm"] = out["اسم_المنتج"].apply(_normalize)
    out = out[out["_norm"].str.len() > 0].reset_index(drop=True)
    logger.info("🏪 منتجات منافسين: %d", len(out))
    return out


def load_brands(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig") if str(path).lower().endswith("csv") \
         else pd.read_excel(path)
    brand_col = next((c for c in df.columns if "اسم" in str(c) and "الماركة" in str(c)), df.columns[0])
    df = df.rename(columns={brand_col: "اسم الماركة"})
    df["_norm"] = df["اسم الماركة"].fillna("").astype(str).apply(_normalize)
    logger.info("🏷️ ماركاتنا: %d", len(df))
    return df


def load_categories(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig") if str(path).lower().endswith("csv") \
         else pd.read_excel(path)
    cat_col = next((c for c in df.columns if "تصنيف" in str(c)), df.columns[0])
    df = df.rename(columns={cat_col: "التصنيفات"})
    df["_norm"] = df["التصنيفات"].fillna("").astype(str).apply(_normalize)
    logger.info("📁 تصنيفاتنا: %d", len(df))
    return df


# ══════════════════════════════════════════════════════════════════════════
#  Core: Missing detection
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class MissingProduct:
    name: str
    price: float
    competitor: str
    url: str
    image: str
    key: str                     # hash key لمنع التكرار
    confidence: str              # "sure_missing" | "ai_verified"


def detect_missing(comp_df: pd.DataFrame, catalog: pd.DataFrame,
                   use_ai: bool = True, ledger=None) -> List[MissingProduct]:
    """
    يصنّف كل منتج منافس: موجود / مشكوك (AI) / مفقود.

    Phase 0: when a ``ledger`` (observability.CompetitorIntakeLedger) is
    provided, every row is ingested up-front and transitioned to a terminal
    ledger state (CONFIRMED_MATCH / CONFIRMED_MISSING / RETRY_PENDING) so the
    invariant holds without changing any existing detection logic.
    """
    from observability.ledger import (
        NullLedger, CONFIRMED_MATCH, CONFIRMED_MISSING, RETRY_PENDING,
    )
    _led = ledger if ledger is not None else NullLedger()

    catalog_norms = catalog["_norm"].tolist()
    seen_keys: Dict[str, MissingProduct] = {}
    uncertain: List[Tuple[int, pd.Series, float]] = []
    # Phase 0: idx → (comp_id, confidence_tag) so ambiguous rows can be
    # settled in the AI-verify pass without a second scan.
    row_ids: Dict[int, str] = {}

    for idx, row in comp_df.iterrows():
        q = row["_norm"]
        if not q:
            # Phase 0: empty-normalized rows still exist as scraped products.
            # Record them so the invariant isn't silently broken.
            try:
                nm = str(row.get("اسم_المنتج", "")).strip()
                if nm:
                    cid = _led.mark_ingested(
                        str(row.get("المنافس", "")), nm,
                        url=str(row.get("الرابط", "")),
                    )
                    _led.mark_state(cid, "REJECTED_STRUCTURAL",
                                    reason_code="empty_normalized_name")
            except Exception:
                pass
            continue

        # Ingest BEFORE any classification.
        cid = _led.mark_ingested(
            str(row.get("المنافس", "")),
            str(row.get("اسم_المنتج", "")),
            url=str(row.get("الرابط", "")),
            raw={"price": float(row.get("السعر") or 0)},
        )
        row_ids[int(idx)] = cid

        m = rf_process.extractOne(q, catalog_norms, scorer=fuzz.token_set_ratio)
        score = m[1] if m else 0.0

        if score >= EXISTS_THRESHOLD:
            # ── تحقق هيكلي: الاسم قريب لكن هل الحجم والماركة متطابقان؟ ──
            matched_catalog_name = catalog.iloc[m[2]]["أسم المنتج"] if m else ""
            comp_raw_name = str(row.get("اسم_المنتج", ""))
            if _structural_match(comp_raw_name, str(matched_catalog_name)):
                _led.mark_state(cid, CONFIRMED_MATCH,
                                reason_code="exists_in_catalog",
                                last_score=float(score))
                continue                                          # موجود عندنا
            else:
                # الاسم متشابه لكن الحجم أو الماركة مختلف → مشكوك (ليس موجود)
                logger.debug("🔍 هيكلي: '%s' ≠ '%s' (score=%.0f) — حجم/ماركة مختلف",
                             comp_raw_name[:60], str(matched_catalog_name)[:60], score)
                uncertain.append((idx, row, score))
                continue
        if UNCERTAIN_LOWER <= score < EXISTS_THRESHOLD:
            uncertain.append((idx, row, score))
            continue
        # مفقود مؤكد
        k = _hash_key(row["اسم_المنتج"])
        if k in seen_keys:
            # Duplicate within competitors: keep the higher price but make
            # sure the ledger still sees this row as CONFIRMED_MISSING so the
            # invariant counts it.
            if float(row["السعر"] or 0) > seen_keys[k].price:
                seen_keys[k].price = float(row["السعر"] or 0)
                seen_keys[k].competitor = row["المنافس"]
                seen_keys[k].url = row["الرابط"]
            _led.mark_state(cid, CONFIRMED_MISSING,
                            reason_code="duplicate_missing",
                            last_score=float(score))
            continue
        seen_keys[k] = MissingProduct(
            name=row["اسم_المنتج"], price=float(row["السعر"] or 0),
            competitor=row["المنافس"], url=row["الرابط"],
            image=row["الصورة"], key=k, confidence="sure_missing",
        )
        _led.mark_state(cid, CONFIRMED_MISSING,
                        reason_code="sure_missing",
                        last_score=float(score))

    # AI verify للمشكوكين
    # FIX: لا نُسقط المنتجات المشكوكة صامتاً. عند توفر AI نستخدمه للتحقق،
    # وإلا نُبقيها بدرجة ثقة "uncertain" حتى لا نفقد الفرص (المنافس فعلاً
    # يعرضها وكتالوجنا لا يطابق بشكل قاطع).
    if use_ai and uncertain and _call_gemini:
        for idx, row, score in uncertain:
            cid = row_ids.get(int(idx), "")
            if _ai_is_missing(row["اسم_المنتج"], catalog, score):
                k = _hash_key(row["اسم_المنتج"])
                if k not in seen_keys:
                    seen_keys[k] = MissingProduct(
                        name=row["اسم_المنتج"], price=float(row["السعر"] or 0),
                        competitor=row["المنافس"], url=row["الرابط"],
                        image=row["الصورة"], key=k, confidence="ai_verified",
                    )
                if cid:
                    _led.mark_state(cid, CONFIRMED_MISSING,
                                    reason_code="ai_verified_missing",
                                    last_score=float(score))
            else:
                if cid:
                    _led.mark_state(cid, CONFIRMED_MATCH,
                                    reason_code="ai_verified_exists",
                                    last_score=float(score))
    elif uncertain:
        # AI غير متاح: احتفظ بالمشكوكين كـ "uncertain" بدل إسقاطهم.
        # Phase 0: these become RETRY_PENDING in the ledger, not a silent
        # "uncertain" limbo — Phase 4 will drive them to a terminal state.
        for idx, row, score in uncertain:
            k = _hash_key(row["اسم_المنتج"])
            cid = row_ids.get(int(idx), "")
            if k not in seen_keys:
                seen_keys[k] = MissingProduct(
                    name=row["اسم_المنتج"], price=float(row["السعر"] or 0),
                    competitor=row["المنافس"], url=row["الرابط"],
                    image=row["الصورة"], key=k, confidence="uncertain",
                )
            if cid:
                _led.mark_state(cid, RETRY_PENDING,
                                reason_code="ai_unavailable",
                                last_score=float(score))

    logger.info("✅ مفقودات نهائية: %d (مشكوك %d)", len(seen_keys), len(uncertain))
    return list(seen_keys.values())


def _ai_is_missing(name: str, catalog: pd.DataFrame, score: float) -> bool:
    """Gemini check: هل هذا المنتج = أحد المنتجات في كتالوجنا؟"""
    if not _call_gemini:
        return False
    top5 = rf_process.extract(_normalize(name), catalog["_norm"].tolist(),
                              scorer=fuzz.token_set_ratio, limit=5)
    candidates = [catalog.iloc[i]["أسم المنتج"] for _, _, i in top5]
    prompt = (
        f"منتج منافس: «{name}»\n"
        f"أقرب 5 منتجات في كتالوجنا:\n"
        + "\n".join(f"{i+1}. {c}" for i, c in enumerate(candidates))
        + "\n\nهل المنتج المنافس يطابق أي منها (نفس الاسم/الحجم/النوع)؟ "
          "أجب بـ: نعم / لا فقط."
    )
    try:
        ans = (_call_gemini(prompt, temperature=0.1, max_tokens=10) or "").strip()
        # FIX: "no" كانت تطابق أي كلمة تحتويها (مثل "not sure").
        # نستخدم مطابقة دقيقة: الإجابة تبدأ بـ لا/no، ونتعامل مع الشك كمفقود
        # للحفاظ على الفرص (better to review than lose).
        low = ans.lower().strip(" .!،")
        if ans.startswith("لا") or low == "no" or low.startswith("no "):
            return True  # لا يطابق → مفقود
        if ans.startswith("نعم") or low == "yes" or low.startswith("yes "):
            return False  # يطابق → موجود
        # غير واضح → اعتبره مفقوداً (لا تُسقط الفرصة)
        return True
    except Exception:
        # فشل AI → اعتبره مفقوداً (حفاظاً على الفرص)
        return True


# ══════════════════════════════════════════════════════════════════════════
#  Brand + Category resolution
# ══════════════════════════════════════════════════════════════════════════
def resolve_brand(name: str, brands: pd.DataFrame) -> Tuple[str, bool]:
    """يعيد (اسم_الماركة_الرسمي, is_existing)."""
    q = _normalize(name)
    m = rf_process.extractOne(q, brands["_norm"].tolist(), scorer=fuzz.partial_ratio)
    if m and m[1] >= BRAND_MATCH_THRESHOLD:
        return brands.iloc[m[2]]["اسم الماركة"], True
    # AI extract brand name
    if _call_gemini:
        try:
            ans = _call_gemini(
                f"استخرج اسم الماركة فقط (بالإنجليزية) من اسم المنتج: «{name}»\n"
                "مثال: 'Dior Sauvage EDP 100ml' → Dior\nأجب بكلمة واحدة فقط.",
                temperature=0.1, max_tokens=20,
            ) or ""
            brand_guess = ans.strip().split("\n")[0][:40]
            if brand_guess:
                return brand_guess, False
        except Exception:
            pass
    return "غير محدد", False


def resolve_category(name: str, categories: pd.DataFrame) -> str:
    """يصنّف المنتج ضمن أحد تصنيفاتنا."""
    if categories.empty:
        return ""
    if not _call_gemini:
        return categories.iloc[0]["التصنيفات"]
    cats = categories["التصنيفات"].tolist()
    try:
        ans = _call_gemini(
            f"صنّف المنتج: «{name}»\nضمن إحدى هذه التصنيفات فقط:\n"
            + "\n".join(f"- {c}" for c in cats)
            + "\nأجب باسم التصنيف فقط.",
            temperature=0.1, max_tokens=40,
        ) or ""
        pick = ans.strip().split("\n")[0]
        m = rf_process.extractOne(pick, cats, scorer=fuzz.partial_ratio)
        if m and m[1] >= CATEGORY_MATCH_THRESHOLD:
            return cats[m[2]]
    except Exception:
        pass
    return cats[0] if cats else ""


# ══════════════════════════════════════════════════════════════════════════
#  Description generator (Mahwous style)
# ══════════════════════════════════════════════════════════════════════════
_DESCRIPTION_PROMPT = """اكتب وصفاً لمنتج عطر بتنسيق HTML مطابق لأسلوب mahwous.com:

المنتج: {name}
الماركة: {brand}

التنسيق المطلوب (HTML فعلي، استبدل القيم):
<h2>{name} من {brand}</h2>
<p>اكتشف سحر {name} من {brand} — عطر فاخر يجمع بين الأصالة والتميز. متوفر الآن في متجر مهووس، وجهتك الأولى لأرقى العطور العالمية.</p>
<h3>تفاصيل المنتج</h3>
<ul>
<li><strong>الماركة:</strong> {brand}</li>
<li><strong>الجنس:</strong> [رجالي/نسائي/مشترك]</li>
<li><strong>نوع المنتج:</strong> عطور</li>
<li><strong>شخصية العطر:</strong> [وصفان]</li>
<li><strong>العائلة العطرية:</strong> [زهري-خشبي]</li>
<li><strong>الحجم:</strong> [مل]</li>
<li><strong>نسبة التركيز:</strong> [EDP/EDT]</li>
</ul>
<h3>الهرم العطري (المكونات العطرية)</h3>
<ul>
<li><strong>مقدمة العطر:</strong> ...</li>
<li><strong>قلب العطر:</strong> ...</li>
<li><strong>قاعدة العطر:</strong> ...</li>
</ul>
<h3>لماذا تختار هذا العطر؟</h3>
<ul><li>...</li></ul>
<h3>لمسة خبير من مهووس</h3>
<p>ننصح برشه على نقاط النبض للحصول على أفضل أداء وفوحان.</p>
<p><strong>عالمك العطري يبدأ من مهووس.</strong> أصلي 100% | شحن سريع داخل السعودية.</p>

اعتمد على Fragrantica/Parfumo/الموقع الرسمي. لا تخترع مكوّنات.
أخرج HTML فقط — لا شرح خارجي. لا تستخدم الإيموجي."""


def generate_description(name: str, brand: str) -> str:
    if not _call_gemini:
        return f"<p>{name}</p>"
    try:
        txt = _call_gemini(
            _DESCRIPTION_PROMPT.format(name=name, brand=brand),
            grounding=True, temperature=0.4, max_tokens=2000,
        ) or ""
        return txt.strip() or f"<p>{name}</p>"
    except Exception:
        return f"<p>{name}</p>"


def fetch_brand_logo(brand: str) -> str:
    """يحاول جلب رابط شعار الماركة عبر Gemini grounding."""
    if not _call_gemini:
        return ""
    try:
        ans = _call_gemini(
            f"ابحث عن الرابط المباشر لشعار ماركة العطور «{brand}» (PNG/JPG شفاف إن أمكن). "
            "أجب برابط واحد فقط بدون شرح.",
            grounding=True, temperature=0.1, max_tokens=100,
        ) or ""
        url = ans.strip().split()[0] if ans.strip() else ""
        return url if url.startswith(("http://", "https://")) else ""
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════════
#  Full pipeline + export
# ══════════════════════════════════════════════════════════════════════════
def build_missing_exports(
    catalog_path: str,
    competitor_paths: List[str],
    brands_path: str,
    categories_path: str,
    output_dir: str = "",
    use_ai: bool = True,
    generate_descriptions: bool = True,
) -> Dict[str, str]:
    """
    ينفّذ كامل المسار ويكتب ملفين:
      - new_products_{ts}.xlsx   بتنسيق قريب من منتج جديد.csv
      - new_brands_{ts}.csv      للماركات الجديدة فقط
    يعيد dict بمسارات الملفات.
    """
    import os as _os
    from datetime import datetime
    if not output_dir:
        _base = (_os.environ.get("DATA_DIR") or "data").rstrip("/")
        output_dir = _os.path.join(_base, "exports")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")

    catalog    = load_catalog(catalog_path)
    comp_df    = load_competitors(competitor_paths)
    brands     = load_brands(brands_path)
    categories = load_categories(categories_path)

    missing = detect_missing(comp_df, catalog, use_ai=use_ai)

    products_rows: List[Dict[str, Any]] = []
    new_brands_rows: List[Dict[str, Any]] = []
    seen_new_brands: set = set()

    for mp in missing:
        brand_name, exists = resolve_brand(mp.name, brands)
        if not exists and brand_name not in seen_new_brands and brand_name != "غير محدد":
            seen_new_brands.add(brand_name)
            new_brands_rows.append({
                "اسم الماركة":                brand_name,
                "نص مقدمة عن الماركة":        f"ماركة {brand_name} المميزة.",
                "رابط شعار الماركة":          fetch_brand_logo(brand_name) if use_ai else "",
                "(الترتيب) صفحة الماركة":     "",
                "(Page Title) عنوان صفحة الماركة التسويقية":
                    f"{brand_name} | عطور أصلية - مهووس",
                "(SEO Page URL) رابط صفحة الماركة التسويقية":
                    re.sub(r"\s+", "-", brand_name.lower()),
                "(Page Description) وصف صفحة الماركة التسويقية":
                    f"تسوق عطور {brand_name} الأصلية من مهووس.",
            })

        category = resolve_category(mp.name, categories) if use_ai else ""
        description = generate_description(mp.name, brand_name) if generate_descriptions else ""

        _name_lower = (mp.name or "").lower()
        if re.search(r"tester|تستر|عينة|sample", _name_lower):
            _availability = "تستر"
        else:
            _availability = ""

        products_rows.append({
            "أسم المنتج":          mp.name,
            "نوع_متاح":            _availability,
            "الحالة":              "نشط",
            "تصنيف المنتج":        category,
            "صورة المنتج":         mp.image,
            "اسم صورة المنتج":     mp.name[:80],
            "حالة المنتج":         "جديد",
            "نص المنتج":           description,
            "الوصف":               description,
            "description":         description,
            "الماركة":             brand_name,
            "رمز المنتج sku":      "",
            "سعر المنتج":          float(mp.price),
            "السعر المخفض":        0,
            "تكلفة المنتج":        0,
            "كمية المنتج":         0,
            "أقل كمية للتنبيه":    0,
            "إظهار كمية المنتج":   0,
            "الوزن":               0.2,
            "وحدة الوزن":          "kg",
            "رابط المنافس":        mp.url,
            "المنافس":             mp.competitor,
            "_hash":               mp.key,
            "_confidence":         mp.confidence,
        })

    products_path = str(Path(output_dir) / f"new_products_{ts}.xlsx")
    brands_path_out = str(Path(output_dir) / f"new_brands_{ts}.csv")

    pd.DataFrame(products_rows).to_excel(products_path, index=False)
    if new_brands_rows:
        pd.DataFrame(new_brands_rows).to_csv(brands_path_out, index=False, encoding="utf-8-sig")

    logger.info("📤 تم التصدير: %d منتج | %d ماركة جديدة",
                len(products_rows), len(new_brands_rows))
    return {
        "products_file":    products_path,
        "new_brands_file":  brands_path_out if new_brands_rows else "",
        "products_count":   len(products_rows),
        "new_brands_count": len(new_brands_rows),
    }
