"""
sitemap_cache.py
================
كاش روابط منتجات لكل متجر منافس مع تاريخ آخر تعديل (lastmod).
يُستخدم للتحديث التزايدي: في كل تشغيل نكتفي بكشط المنتجات الجديدة
أو التي تغيّر تاريخها فقط — مما يقلل وقت التحديث وضغط الكشط.

البنية على القرص: data/sitemap_cache/{store_slug}.json
{
  "store_url": "https://saeedsalah.com/",
  "fetched_at": 1776560000,
  "urls": {
    "https://saeedsalah.com/.../p123": "2026-04-15T12:00:00+03:00",
    ...
  }
}
"""
from __future__ import annotations
import os
import re
import json
import time
from typing import Dict, Iterable, List, Tuple
from urllib.parse import urlparse

DATA_DIR = os.environ.get("DATA_DIR", "data")
CACHE_DIR = os.path.join(DATA_DIR, "sitemap_cache")


def _slug(store_url: str) -> str:
    host = urlparse(store_url).hostname or store_url
    return re.sub(r"[^a-z0-9.-]+", "_", host.lower())


def _path(store_url: str) -> str:
    return os.path.join(CACHE_DIR, f"{_slug(store_url)}.json")


def load(store_url: str) -> Dict:
    p = _path(store_url)
    if not os.path.exists(p):
        return {"store_url": store_url, "fetched_at": 0, "urls": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"store_url": store_url, "fetched_at": 0, "urls": {}}


def save(
    store_url: str,
    urls: Dict[str, str],
    failures: Dict[str, int] | None = None,
) -> str:
    """
    Persist cache. `failures` (optional) tracks per-URL consecutive fetch
    failures so we can retry new+failed URLs instead of silently skipping them
    — and eventually abandon them after MAX_FAIL_COUNT attempts to avoid
    hammering broken pages forever. Backward-compatible: omitted/empty = {}.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = _path(store_url)
    payload = {
        "store_url": store_url,
        "fetched_at": int(time.time()),
        "urls": urls,
        "failures": dict(failures or {}),
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return p


def diff(old_urls: Dict[str, str], new_entries: Iterable) -> Tuple[List[str], List[str], List[str]]:
    """
    يُرجع (added, modified, unchanged).
    new_entries: iterable من SitemapEntry (.url, .lastmod) أو dict {url, lastmod}.
    """
    added: List[str] = []
    modified: List[str] = []
    unchanged: List[str] = []
    for e in new_entries:
        if hasattr(e, "url"):
            url = e.url
            lm = getattr(e, "lastmod", "") or ""
        else:
            url = e.get("url", "")
            lm = e.get("lastmod", "") or ""
        if not url:
            continue
        prev = old_urls.get(url)
        if prev is None:
            added.append(url)
        elif lm and prev != lm:
            modified.append(url)
        else:
            unchanged.append(url)
    return added, modified, unchanged


MAX_FAIL_COUNT = 3  # Phase 2: abandon after N consecutive failures


def merge_after_scrape(
    store_url: str,
    new_entries: Iterable,
    successfully_scraped_urls: Iterable[str] = (),
    attempted_but_failed_urls: Iterable[str] = (),
    max_fail_count: int = MAX_FAIL_COUNT,
) -> Dict[str, str]:
    """
    يدمج لقطة Sitemap الجديدة مع الكاش القديم.

    السلوك:
      - نجح الكشط            → حدّث lastmod، مسح عدّاد الفشل.
      - فُشل الكشط (attempted_but_failed):
          * عدّاد الفشل < N   → خزّن lastmod="" (يجبر تصنيفه "modified"
                                في الجولة التالية فيُعاد محاولته).
          * عدّاد الفشل ≥ N   → اقبل lastmod السايت-ماب واعتبره ميؤوساً منه
                                (لمنع حلقة لا نهائية على صفحة معطوبة).
      - لم يُكشط أصلاً (unchanged أو مفلتر): أبقِ lastmod القديم، أو سجّل
        الجديد بـ lastmod السايت-ماب إن كان غير موجود.

    حافة p/4 المغطّاة: رابط جديد فشل لأول مرة لم يعد يُحفظ بـ lastmod
    السايت-ماب (السلوك القديم كان يُصنّفه "unchanged" في الجولة التالية
    ويُتخطى للأبد).
    """
    data = load(store_url)
    old = data.get("urls", {}) or {}
    failures: Dict[str, int] = dict(data.get("failures", {}) or {})
    success = set(successfully_scraped_urls or [])
    failed = set(attempted_but_failed_urls or [])
    merged = dict(old)

    for e in new_entries:
        if hasattr(e, "url"):
            url = e.url
            lm = getattr(e, "lastmod", "") or ""
        else:
            url = e.get("url", "")
            lm = e.get("lastmod", "") or ""
        if not url:
            continue

        if url in success:
            merged[url] = lm
            failures.pop(url, None)
        elif url in failed:
            cnt = failures.get(url, 0) + 1
            if cnt >= max_fail_count:
                # ميؤوس منه: اقبل lastmod السايت-ماب ليُصنَّف unchanged
                # فلا نُعيد محاولته إلا إذا تغيّر lastmod.
                merged[url] = lm
                failures.pop(url, None)
            else:
                merged[url] = ""  # يُصنَّف "modified" في الجولة التالية
                failures[url] = cnt
        elif url not in merged:
            merged[url] = lm

    save(store_url, merged, failures=failures)
    return merged


def status_all() -> List[Dict]:
    """يُرجع ملخص لكل ملفات الكاش الموجودة."""
    out: List[Dict] = []
    if not os.path.isdir(CACHE_DIR):
        return out
    for fn in sorted(os.listdir(CACHE_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(CACHE_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            out.append({
                "store_url": d.get("store_url", fn),
                "urls_count": len(d.get("urls", {})),
                "fetched_at": d.get("fetched_at", 0),
                "file": fn,
            })
        except Exception:
            continue
    return out
