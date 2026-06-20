"""
mahwous_links.py
================
يبني خريطة موثوقة بين أسماء الماركات/التصنيفات والروابط الحقيقية الكاملة
على متجر mahwous.com (متضمنة معرّفات Salla الداخلية مثل brand-XXXX و cYYYY).

المصادر:
- https://mahwous.com/sitemap-1.xml  → روابط التصنيفات (c-IDs)
- https://mahwous.com/brands         → روابط الماركات (brand-IDs)

النتيجة تُحفظ في data/mahwous_links.json وتُحمّل في الذاكرة عند الاستخدام.
"""
from __future__ import annotations
import os
import re
import json
import threading
import time
import unicodedata
from typing import Optional, Dict, List, Tuple
from urllib.parse import unquote

try:
    import requests
except Exception:
    requests = None  # noqa

try:
    from rapidfuzz import fuzz, process as rf_process
    _HAS_RF = True
except Exception:
    _HAS_RF = False

DATA_DIR = os.environ.get("DATA_DIR", "data")
CACHE_PATH = os.path.join(DATA_DIR, "mahwous_links.json")
SITEMAP_INDEX = "https://mahwous.com/sitemap.xml"
BRANDS_PAGE = "https://mahwous.com/brands"
BASE = "https://mahwous.com"

_AR_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_BRAND_HREF_RE = re.compile(r'href="(https://mahwous\.com/[^"]*?/brand-\d+)"', re.I)
_CAT_LOC_RE = re.compile(r"<loc>(https://mahwous\.com/[^<]+/c\d+)</loc>", re.I)
_SITEMAP_RE = re.compile(r"<loc>(https://mahwous\.com/[^<]+\.xml)</loc>", re.I)


def _norm(s: str) -> str:
    """تطبيع نص عربي: إزالة التشكيل، توحيد الهمزات، lowercase، إزالة الرموز."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = _AR_DIACRITICS.sub("", s)
    s = (s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
           .replace("ة", "ه").replace("ى", "ي").replace("ـ", ""))
    s = s.replace("-", " ").replace("_", " ").replace("|", " ")
    s = _NON_WORD.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _slug_from_url(url: str) -> str:
    """يستخرج الـslug العربي من رابط Salla قبل الـ/brand-... أو /c..."""
    try:
        path = url.replace(BASE, "").strip("/")
        # احذف آخر segment (brand-XXX أو cYYY)
        parts = path.split("/")
        if parts and re.match(r"^(brand-\d+|c\d+)$", parts[-1]):
            slug = "/".join(parts[:-1])
        else:
            slug = path
        return unquote(slug)
    except Exception:
        return url


def _http_get(url: str, timeout: int = 30) -> str:
    if requests is None:
        raise RuntimeError("requests not installed")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MahwousLinksBot/1.0)",
        "Accept-Language": "ar,en;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def fetch_categories() -> List[Dict[str, str]]:
    """يجلب جميع روابط التصنيفات من sitemap (يدعم sitemap index)."""
    out: List[Dict[str, str]] = []
    try:
        idx = _http_get(SITEMAP_INDEX)
    except Exception:
        return out
    sitemap_urls = _SITEMAP_RE.findall(idx) or [SITEMAP_INDEX]
    for sm in sitemap_urls:
        try:
            xml = _http_get(sm)
        except Exception:
            continue
        for url in _CAT_LOC_RE.findall(xml):
            slug = _slug_from_url(url)
            out.append({
                "url": url,
                "slug": slug,
                "name_norm": _norm(slug),
            })
    # إزالة التكرارات
    seen = set()
    uniq = []
    for it in out:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq


def fetch_brands() -> List[Dict[str, str]]:
    """يجلب جميع روابط الماركات من صفحة /brands."""
    out: List[Dict[str, str]] = []
    try:
        html = _http_get(BRANDS_PAGE)
    except Exception:
        return out
    seen = set()
    for url in _BRAND_HREF_RE.findall(html):
        if url in seen:
            continue
        seen.add(url)
        slug = _slug_from_url(url)
        out.append({
            "url": url,
            "slug": slug,
            "name_norm": _norm(slug),
        })
    return out


def refresh_cache() -> Dict[str, any]:
    """يجلب الروابط ويكتب الكاش. يُرجع ملخصاً."""
    os.makedirs(DATA_DIR, exist_ok=True)
    cats = fetch_categories()
    brands = fetch_brands()
    payload = {
        "fetched_at": int(time.time()),
        "categories": cats,
        "brands": brands,
    }
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {
        "categories_count": len(cats),
        "brands_count": len(brands),
        "fetched_at": payload["fetched_at"],
        "cache_path": CACHE_PATH,
    }


_MEM_CACHE: Optional[Dict] = None
_MEM_LOCK = threading.Lock()


def _load_cache() -> Dict:
    global _MEM_CACHE
    with _MEM_LOCK:
        if _MEM_CACHE is not None:
            return _MEM_CACHE
        if not os.path.exists(CACHE_PATH):
            _MEM_CACHE = {"categories": [], "brands": [], "fetched_at": 0}
            return _MEM_CACHE
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                _MEM_CACHE = json.load(f)
        except Exception:
            _MEM_CACHE = {"categories": [], "brands": [], "fetched_at": 0}
        return _MEM_CACHE


def reload_cache() -> None:
    global _MEM_CACHE
    with _MEM_LOCK:
        _MEM_CACHE = None
    _load_cache()


def cache_status() -> Dict[str, any]:
    c = _load_cache()
    return {
        "categories_count": len(c.get("categories", [])),
        "brands_count": len(c.get("brands", [])),
        "fetched_at": c.get("fetched_at", 0),
        "cache_path": CACHE_PATH,
        "exists": os.path.exists(CACHE_PATH),
    }


def _best_match(query: str, items: List[Dict[str, str]],
                threshold: int = 75) -> Optional[Dict[str, str]]:
    """يطابق نص ضد قائمة slugs بـrapidfuzz، يُرجع أفضل عنصر إن تجاوز العتبة."""
    qn = _norm(query)
    if not qn or not items:
        return None
    # محاولة 1: تطابق دقيق
    for it in items:
        if it["name_norm"] == qn:
            return it
    # محاولة 2: تطابق احتواء
    contained = [it for it in items if qn in it["name_norm"] or it["name_norm"] in qn]
    if len(contained) == 1:
        return contained[0]
    if not _HAS_RF:
        return contained[0] if contained else None
    # محاولة 3: rapidfuzz
    candidates = contained or items
    choices = {i: it["name_norm"] for i, it in enumerate(candidates)}
    best = rf_process.extractOne(qn, choices, scorer=fuzz.token_set_ratio)
    if best and best[1] >= threshold:
        return candidates[best[2]]
    return None


def lookup_brand_url(name: str) -> Optional[str]:
    """يُرجع رابط الماركة الكامل (مع brand-ID) أو None."""
    if not name:
        return None
    c = _load_cache()
    item = _best_match(name, c.get("brands", []))
    return item["url"] if item else None


def lookup_category_url(name: str) -> Optional[str]:
    """يُرجع رابط التصنيف الكامل (مع c-ID) أو None."""
    if not name:
        return None
    c = _load_cache()
    item = _best_match(name, c.get("categories", []))
    return item["url"] if item else None


def lookup_category_url_for_perfume(gender: str = "", kind: str = "") -> Optional[str]:
    """
    اختصار: يعطيك رابط تصنيف عطر مناسب حسب الجنس والنوع.
    gender: 'رجالي' / 'نسائي' / 'للجنسين'
    kind:   'نيش' / 'تستر' / 'بدائل' / 'فرموني' / '' (افتراضي عام)
    """
    g = (gender or "").strip()
    k = (kind or "").strip()
    queries = []
    if k and g:
        queries.append(f"عطور {k} {g}")
    if k:
        queries.append(f"عطور {k}")
    if g:
        queries.append(f"عطور {g}")
    queries.append("العطور")
    for q in queries:
        u = lookup_category_url(q)
        if u:
            return u
    return None


if __name__ == "__main__":
    print(json.dumps(refresh_cache(), ensure_ascii=False, indent=2))
