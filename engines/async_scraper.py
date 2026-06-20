"""
engines/async_scraper.py — محرك الكشط الرئيسي v2.0 (MASTER)
═══════════════════════════════════════════════════════════════
✅ توافق تام مع StealthManager و SitemapResolver
✅ نقاط استئناف ذكية (Checkpointing) لكل منافس على حدة
✅ حماية الذاكرة وتسريع الـ Regex
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
import traceback
import random
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import urlparse
from contextvars import ContextVar

import concurrent.futures

import aiohttp
import pandas as pd

# Dedicated thread pool for sync fallback calls (cloudscraper / curl_cffi / Selenium).
# The default asyncio executor has only ~5 threads on Cloud Run (1 CPU + 4).
# Without a dedicated pool, 50 concurrent URL fetches exhaust the shared pool,
# new run_in_executor calls block waiting for a free thread, and the event loop
# itself stalls — producing the indefinite "hang" after alkhabeershop.com.
_SYNC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=50,
    thread_name_prefix="scraper-sync",
)
import atexit as _atexit_sync
_atexit_sync.register(lambda: _SYNC_EXECUTOR.shutdown(wait=False))

if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ─── قفل مزامنة مشترك لحماية ملفات الحالة من Race Conditions ────────────────
_STATE_WRITE_LOCK    = threading.Lock()
_PROGRESS_WRITE_LOCK = threading.Lock()
_LIVE_WRITE_LOCK     = threading.Lock()
_CSV_WRITE_LOCK      = threading.Lock()

# ─── إعداد السجل ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("AsyncScraper")

# ─── ثوابت مُترجمة مسبقاً (Precompiled Regex) لحماية الذاكرة ────────────────
import re as _re

_RE_OG_TITLE    = _re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', _re.I)
_RE_OG_IMAGE    = _re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', _re.I)
_RE_OG_URL      = _re.compile(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',   _re.I)
_RE_OG_PRICE    = _re.compile(r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([^"\']+)["\']', _re.I)
_RE_PRICE_SPAN  = _re.compile(r'class="[^"]*price[^"]*"[^>]*>\s*(?:<[^>]+>)?([\d,. ]+)', _re.I)
_RE_H1_PRODUCT  = _re.compile(r'<h1[^>]*>\s*([^<]{3,120}?)\s*</h1>', _re.S | _re.I)

# ─── Anti-ban imports مُسبقة على مستوى الـ Module ─────────────────────────
try:
    from scrapers.anti_ban import stealth_manager, fetch_with_retry
    _ANTI_BAN_AVAILABLE = True
except ImportError:
    _ANTI_BAN_AVAILABLE = False
    logger.warning("⚠️ scrapers.anti_ban غير متاح — سيتم استخدام headers افتراضية")

# ─── مسارات البيانات ──────────────────────────────────────────────────────────
_DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(_DATA_DIR, exist_ok=True)

COMPETITORS_FILE = os.path.join(_DATA_DIR, "competitors_list.json")
OUTPUT_CSV       = os.path.join(_DATA_DIR, "competitors_latest.csv")
PROGRESS_FILE    = os.path.join(_DATA_DIR, "scraper_progress.json")
LASTMOD_FILE     = os.path.join(_DATA_DIR, "scraper_lastmod.json")
STATE_FILE       = os.path.join(_DATA_DIR, "scraper_state.json")   # نقاط الاستئناف
PID_FILE         = os.path.join(_DATA_DIR, "scraper.pid")

CSV_COLS = [
    "store", "name", "price", "original_price",
    "sku", "url", "image", "brand", "category",
    "availability", "scraped_at",
]


def _has_valid_price(row: dict | None) -> bool:
    if not row:
        return False
    try:
        return float(row.get("price") or 0) > 0
    except Exception:
        return False


def _proxy_pool_from_env() -> List[str]:
    raw = os.environ.get("SCRAPER_PROXIES", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _v30_row_from_result(result: dict | None, store_url: str) -> dict | None:
    if not isinstance(result, dict):
        return None
    try:
        price = float(result.get("price") or 0)
    except Exception:
        price = 0.0
    name = str(result.get("name") or "").strip()
    if not name and price <= 0:
        return None
    return extract_product(
        {
            "name": name,
            "price": price,
            "sku": result.get("sku") or "",
            "image": result.get("image") or "",
            "url": result.get("url") or "",
            "brand": result.get("brand") or "",
        },
        store_url,
    )


def _run_v30_sync(url: str, store_url: str) -> dict | None:
    try:
        from engines.selenium_scraper_v30 import scrape_product_v30
        proxies = _proxy_pool_from_env()
        return scrape_product_v30(
            url=url,
            store_url=store_url,
            proxy=random.choice(proxies) if proxies else "",
        )
    except Exception as exc:
        logger.debug("v30 sync fallback failed for %s: %s", url, exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  هياكل البيانات
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Progress:
    """تقدم الكشط الكلي — يُكتب دورياً إلى PROGRESS_FILE"""
    running: bool = False
    started_at: str = ""
    finished_at: str = ""
    last_updated: str = ""
    phase: str = "discovering"
    pid: int = 0
    stores_total: int = 0
    stores_done: int = 0
    urls_total: int = 0
    urls_processed: int = 0
    rows_in_csv: int = 0
    rows_saved_run: int = 0  # run-scoped: rows saved in THIS run only (not historical CSV)
    fetch_exceptions: int = 0
    success_rate_pct: float = 0.0
    current_store: str = ""
    store_urls_done: int = 0
    store_urls_total: int = 0
    last_error: str = ""
    stores_results: Dict[str, int] = field(default_factory=dict)
    stores_http_errors: Dict[str, dict] = field(default_factory=dict)
    # ── Evidence-backed failure-class counters (truthful diagnostics) ──────
    # urls_discovered : roots returned by sitemap/products.json resolution
    # urls_enqueued   : roots passed the prioritisation filter and entered queue
    # urls_attempted  : fetch_product was actually invoked (reached HTTP stage)
    # urls_skipped_reason : histogram of why URLs were skipped before attempt
    #   e.g. {"resume_done": N, "max_reached": N, "circuit_broken": N,
    #         "not_product_url": N, "empty_sitemap": N, "sitemap_timeout": N,
    #         "sitemap_blocked": N, "import_error": N}
    urls_discovered: int = 0
    urls_enqueued: int = 0
    urls_attempted: int = 0
    urls_skipped_reason: Dict[str, int] = field(default_factory=dict)

    def save(self, path: str = PROGRESS_FILE) -> None:
        try:
            self.last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.pid = os.getpid()
            with _PROGRESS_WRITE_LOCK:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        except Exception:
            logger.warning(f"تعذّر حفظ التقدم: {traceback.format_exc()}")

    @classmethod
    def load(cls, path: str = PROGRESS_FILE) -> "Progress":
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f) or {}
            # Backward/forward compatible load: drop unknown keys so older
            # JSON files (missing rows_saved_run) and newer files (with extra
            # keys) both deserialise safely.
            import dataclasses as _dc
            _known = {f.name for f in _dc.fields(cls)}
            filtered = {k: v for k, v in data.items() if k in _known}
            return cls(**filtered)
        except Exception:
            return cls()


def _write_pid_file() -> None:
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        logger.warning(f"تعذّر حفظ PID: {traceback.format_exc()}")


def _cleanup_pid_file() -> None:
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        logger.warning(f"تعذّر حذف PID file: {traceback.format_exc()}")


def _derive_terminal_phase(rows: int, errors: int, urls_processed: int) -> str:
    """
    يحسم الحالة النهائية للكشط بشكل حتمي:
      - failed   : 0 منتج محفوظ + أخطاء مرصودة (أو لا معالجة أصلاً)
      - partial  : منتجات محفوظة لكن نسبة الأخطاء ≥ 40% من المعالج
      - completed: نجاح طبيعي
    تُستخدم من جميع نقاط الإنهاء لضمان اتساق UI.
    """
    try:
        rows    = int(rows or 0)
        errors  = int(errors or 0)
        urls_processed = int(urls_processed or 0)
    except Exception:
        return "completed"
    if rows <= 0 and (errors > 0 or urls_processed == 0):
        return "failed"
    denom = max(urls_processed, rows + errors, 1)
    err_ratio = errors / denom
    if rows > 0 and err_ratio >= 0.40:
        return "partial"
    return "completed"


def _finalize_progress_phase(progress) -> str:
    """
    يضبط progress.phase للحالة النهائية المشتقة ويعيدها.

    يعتمد على rows_saved_run (منتجات هذا التشغيل فقط) وليس rows_in_csv
    (الذي يمثّل المجموع التاريخي التراكمي في CSV). هذا يمنع اعتبار تشغيل
    فاشل ناجحاً لمجرّد وجود بيانات تاريخية.
    """
    # Prefer explicit run-scoped counter; fall back to sum(stores_results)
    # (also run-scoped because Progress is instantiated fresh per run); only
    # as last resort use rows_in_csv for maximum backward-compat.
    run_rows = int(getattr(progress, "rows_saved_run", 0) or 0)
    if run_rows <= 0:
        try:
            sr = getattr(progress, "stores_results", {}) or {}
            run_rows = sum(int(v or 0) for v in sr.values())
        except Exception:
            run_rows = 0
    phase = _derive_terminal_phase(
        run_rows,
        getattr(progress, "fetch_exceptions", 0),
        getattr(progress, "urls_processed", 0),
    )
    progress.phase = phase
    return phase


def _bump_skip(progress: "Progress", reason: str, n: int = 1) -> None:
    """Increment urls_skipped_reason[reason] safely."""
    try:
        d = progress.urls_skipped_reason
        if not isinstance(d, dict):
            d = {}
            progress.urls_skipped_reason = d
        d[reason] = int(d.get(reason, 0) or 0) + int(n)
    except Exception:
        pass


def _mark_progress_failed(message: str) -> None:
    try:
        progress = Progress.load()
        progress.running = False
        progress.phase = "failed"
        progress.finished_at = datetime.now().isoformat()
        progress.last_error = (message or "")[:300]
        progress.save()
    except Exception:
        logger.warning(f"تعذّر تحديث حالة الفشل: {traceback.format_exc()}")


@dataclass
class StoreCheckpoint:
    """نقطة استئناف خاصة بمتجر واحد"""
    store_url: str
    domain: str
    status: str = "pending"       # pending | running | done | error
    last_page: int = 0            # رقم الصفحة الأخيرة (لـ /products.json)
    last_url_index: int = 0       # فهرس آخر URL في قائمة sitemap
    urls_done: int = 0
    urls_total: int = 0
    rows_saved: int = 0
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    last_checkpoint_at: str = ""


class ScraperState:
    """نظام نقاط الاستئناف الكامل."""
    def __init__(self, path: str = STATE_FILE):
        self._path = path
        self._data: Dict[str, StoreCheckpoint] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            for domain, d in raw.items():
                try:
                    self._data[domain] = StoreCheckpoint(**d)
                except Exception:
                    pass
        except Exception:
            self._data = {}

    def save(self) -> None:
        try:
            out = {k: asdict(v) for k, v in self._data.items()}
            with _STATE_WRITE_LOCK:
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.warning(f"تعذّر حفظ الحالة: {traceback.format_exc()}")

    def get(self, domain: str, store_url: str) -> StoreCheckpoint:
        if domain not in self._data:
            self._data[domain] = StoreCheckpoint(store_url=store_url, domain=domain)
        return self._data[domain]

    def update(self, domain: str, **kwargs) -> None:
        if domain in self._data:
            cp = self._data[domain]
            for k, v in kwargs.items():
                if hasattr(cp, k):
                    setattr(cp, k, v)
            cp.last_checkpoint_at = datetime.now().isoformat()
            self.save()

    def mark_done(self, domain: str, rows: int) -> None:
        self.update(
            domain,
            status="done",
            rows_saved=rows,
            finished_at=datetime.now().isoformat(),
        )

    def mark_error(self, domain: str, error: str) -> None:
        self.update(domain, status="error", error=error[:200])

    def is_done(self, domain: str) -> bool:
        return self._data.get(domain, StoreCheckpoint("", "")).status == "done"

    def reset(self, domain: str | None = None) -> None:
        if domain:
            if domain in self._data:
                cp = self._data[domain]
                cp.status = "pending"
                cp.last_page = 0
                cp.last_url_index = 0
                cp.urls_done = 0
                cp.error = ""
                self.save()
        else:
            self._data = {}
            self.save()

    def get_summary(self) -> dict:
        total = len(self._data)
        done  = sum(1 for c in self._data.values() if c.status == "done")
        err   = sum(1 for c in self._data.values() if c.status == "error")
        return {"total": total, "done": done, "errors": err, "pending": total - done - err}

    def all_checkpoints(self) -> Dict[str, StoreCheckpoint]:
        return self._data


# ══════════════════════════════════════════════════════════════════════════════
#  استخراج المنتجات من JSON / HTML
# ══════════════════════════════════════════════════════════════════════════════

def _domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")

def _write_live_progress(domain: str, data: dict) -> None:
    try:
        with _LIVE_WRITE_LOCK:
            with open(os.path.join(_DATA_DIR, f"_sc_live_{domain}.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass

def extract_product(data: dict, store_url: str) -> dict | None:
    name = (
        data.get("name") or data.get("title") or
        data.get("product_name") or data.get("الاسم") or ""
    ).strip()
    if not name:
        return None

    def _price(raw):
        try:
            return float(str(raw).replace(",", "").replace("ر.س", "").strip())
        except Exception:
            return 0.0

    price = _price(
        data.get("price") or data.get("Price") or
        data.get("regular_price") or data.get("السعر") or 0
    )
    orig  = _price(
        data.get("compare_at_price") or data.get("original_price") or
        data.get("السعر_الأصلي") or price
    )
    sku   = str(data.get("sku") or data.get("id") or data.get("SKU") or "")
    url   = (data.get("url") or data.get("link") or data.get("handle") or "").strip()
    if url and not url.startswith("http"):
        base = store_url.rstrip("/")
        url  = f"{base}/{url.lstrip('/')}"
    image = (
        data.get("image") or data.get("featured_image") or
        data.get("thumbnail") or ""
    )
    if isinstance(image, dict):
        image = image.get("src", "")
    brand = str(data.get("vendor") or data.get("brand") or data.get("الماركة") or "")
    cat   = str(data.get("product_type") or data.get("category") or "")
    avail = str(data.get("available") or data.get("in_stock") or "true")

    return {
        "store":          _domain(store_url),
        "name":           name,
        "price":          price,
        "original_price": orig,
        "sku":            sku,
        "url":            url,
        "image":          image if isinstance(image, str) else "",
        "brand":          brand,
        "category":       cat,
        "availability":   avail,
        "scraped_at":     datetime.now().isoformat()[:19],
    }


def _url_looks_like_product_page(url: str) -> bool:
    p = (urlparse(url).path or "").lower()
    return bool(
        _re.search(r"/p\d{5,}(?:/|\?|$)", p)
        or "/products/" in p
        or "/product/" in p
    )


def _product_fields_from_all_json_ld(html: str) -> dict:
    try:
        from utils.competitor_product_scraper import _walk_json_ld
    except Exception:
        return {}
    acc: dict = {}
    for m in _re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
        html,
        _re.I,
    ):
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        _walk_json_ld(data, acc)
    return acc


# ══════════════════════════════════════════════════════════════════════════════
#  جلب منتج واحد من URL مع حماية Stealth
# ══════════════════════════════════════════════════════════════════════════════

# Per-domain strategy: after N consecutive aiohttp 403/429, skip aiohttp entirely
# for this domain and go straight to curl_cffi sync fallback. This prevents
# concurrent 403 storms from tripping the circuit breaker.
_DOMAIN_AIOHTTP_403: Dict[str, int] = {}
_DOMAIN_SKIP_THRESHOLD = 0  # Skip aiohttp entirely — curl_cffi chrome104 bypasses Cloudflare

# Per-task browser_like_http fetcher — ContextVar provides per-asyncio-task
# isolation so concurrent store scrapes don't overwrite each other's fetcher.
_blh_fetcher_var: ContextVar[Any] = ContextVar('_blh_fetcher', default=None)


async def fetch_product(
    session: aiohttp.ClientSession,
    url: str,
    store_url: str,
    semaphore: asyncio.Semaphore,
    http_status_counters: Dict[str, int] | None = None,
) -> dict | None:
    async with semaphore:
        # Phase 3: robots.txt احترام (fail-open عند الأخطاء)
        try:
            from utils.robots_cache import can_fetch as _robots_can_fetch
            if not await _robots_can_fetch(session, url):
                if http_status_counters is not None:
                    http_status_counters["robots_blocked"] = (
                        http_status_counters.get("robots_blocked", 0) + 1
                    )
                logger.debug("robots.txt disallowed: %s", url)
                return None
        except Exception as _rob_err:
            logger.debug("robots check error %s: %s", url, _rob_err)

        json_url = url if url.endswith(".json") else url.rstrip("/") + ".json"

        # Sticky-per-store proxy selection (None if pool empty → direct).
        _domain_for_proxy = _domain(store_url)
        _skip_aiohttp = _DOMAIN_AIOHTTP_403.get(_domain_for_proxy, 0) >= _DOMAIN_SKIP_THRESHOLD
        _proxy_url: Optional[str] = None
        try:
            if _ANTI_BAN_AVAILABLE:
                from scrapers.anti_ban import proxy_rotator as _pr
                _proxy_url = _pr.get_proxy_for_domain(_domain_for_proxy)
        except Exception:
            _proxy_url = None

        # تطبيق تأخير بسيط جداً داخل كل سレッド لمنع الـ Spike Requests
        if _ANTI_BAN_AVAILABLE:
            await stealth_manager.apply_smart_delay(0.5, 1.5)

        try:
            if _ANTI_BAN_AVAILABLE and not _skip_aiohttp:
                resp = await fetch_with_retry(session, json_url, max_retries=2,
                                               referer=store_url, proxy=_proxy_url)
                if resp is not None:
                    try:
                        if resp.status == 200 and "json" in resp.headers.get("Content-Type", ""):
                            data = await resp.json(content_type=None)
                            prod = data.get("product", data)
                            row  = extract_product(prod, store_url)
                            if _has_valid_price(row):
                                return row
                        elif resp.status in (403, 429):
                            _DOMAIN_AIOHTTP_403[_domain_for_proxy] = _DOMAIN_AIOHTTP_403.get(_domain_for_proxy, 0) + 1
                            if http_status_counters is not None:
                                http_status_counters[str(resp.status)] = (
                                    http_status_counters.get(str(resp.status), 0) + 1
                                )
                    finally:
                        resp.close()
            elif _ANTI_BAN_AVAILABLE:
                pass  # skip aiohttp JSON phase for domains that consistently block
            else:
                async with session.get(
                    json_url, timeout=aiohttp.ClientTimeout(total=12), ssl=False
                ) as resp:
                    if resp.status == 200 and "json" in resp.headers.get("Content-Type", ""):
                        data = await resp.json(content_type=None)
                        prod = data.get("product", data)
                        row  = extract_product(prod, store_url)
                        if row:
                            return row
                    elif resp.status in (403, 429) and http_status_counters is not None:
                        http_status_counters[str(resp.status)] = (
                            http_status_counters.get(str(resp.status), 0) + 1
                        )
        except Exception as e:
            logger.debug("JSON fetch error %s: %s", url, e)

        # ── HTML Fetch بأسلوب التخفي ──────────────────────────────────────
        if _ANTI_BAN_AVAILABLE:
            hdrs = stealth_manager.get_secure_headers(referer=store_url)
        else:
            hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        html: str | None = None

        try:
            if _ANTI_BAN_AVAILABLE and not _skip_aiohttp:
                resp = await fetch_with_retry(session, url, max_retries=3,
                                               referer=store_url, proxy=_proxy_url)
                if resp is not None:
                    try:
                        if resp.status == 200:
                            html = await resp.text(errors="replace")
                            is_banned, ban_msg = stealth_manager.is_shadow_banned(html, resp.status)
                            if is_banned:
                                logger.error(f"[Anti-Ban] Shadow ban during fetch on {url}: {ban_msg}")
                                _DOMAIN_AIOHTTP_403[_domain_for_proxy] = _DOMAIN_AIOHTTP_403.get(_domain_for_proxy, 0) + 1
                                html = None
                        elif resp.status in (403, 429, 503):
                            _DOMAIN_AIOHTTP_403[_domain_for_proxy] = _DOMAIN_AIOHTTP_403.get(_domain_for_proxy, 0) + 1
                            if http_status_counters is not None:
                                http_status_counters[str(resp.status)] = (
                                    http_status_counters.get(str(resp.status), 0) + 1
                                )
                            logger.debug(f"HTTP {resp.status} Blocked: {url}")
                    finally:
                        resp.close()
            elif _ANTI_BAN_AVAILABLE:
                pass  # skip aiohttp HTML phase — goes straight to curl_cffi below
            else:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=20),
                    headers=hdrs, ssl=False, allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors="replace")
                    elif resp.status in (403, 429, 503):
                        if http_status_counters is not None:
                            http_status_counters[str(resp.status)] = (
                                http_status_counters.get(str(resp.status), 0) + 1
                            )
                        logger.debug(f"HTTP {resp.status} Blocked: {url}")
        except Exception:
            logger.debug(f"Fetch failed for {url}: {traceback.format_exc()}")
            html = None

        # في حال الحظر/الفشل، جرّب async curl_cffi مباشرة (أسرع من sync fallback)
        if not html:
            try:
                from browser_like_http import async_scraper_http_stack
                # Use module-level shared fetcher if available, else quick one-off
                _blh_fetcher = _blh_fetcher_var.get(None)
                if _blh_fetcher is not None:
                    code, text = await _blh_fetcher.get_text_once(url, timeout=15.0)
                    if code == 200 and text and len(text) > 500:
                        html = text
                else:
                    # Fallback: sync curl_cffi via thread pool
                    if _ANTI_BAN_AVAILABLE:
                        from scrapers.anti_ban import try_all_sync_fallbacks
                        loop = asyncio.get_running_loop()
                        html = await asyncio.wait_for(
                            loop.run_in_executor(
                                _SYNC_EXECUTOR,
                                lambda: try_all_sync_fallbacks(url, timeout=10, proxy=_proxy_url),
                            ),
                            timeout=15.0,
                        )
            except (asyncio.TimeoutError, Exception):
                html = None

        # Googlebot UA fallback — lightweight Cloudflare bypass for stores
        # that whitelist Google's crawler (Matjrah-based like niche.sa).
        # Runs only if all prior methods failed; avoids the heavier Selenium
        # path that try_all_sync_fallbacks would eventually reach.
        if not html and _ANTI_BAN_AVAILABLE:
            try:
                from scrapers.anti_ban import _try_googlebot_ua
                loop = asyncio.get_running_loop()
                html = await asyncio.wait_for(
                    loop.run_in_executor(
                        _SYNC_EXECUTOR,
                        lambda: _try_googlebot_ua(url, timeout=15, proxy=_proxy_url),
                    ),
                    timeout=18.0,
                )
            except (asyncio.TimeoutError, Exception):
                html = None

        if not html:
            return None

        # ── Phase 2 Item 4: canonical JSON-LD extractor FIRST ────────────
        # engines.json_ld_extractor.extract() enforces an explicit non-SAR
        # currency blocklist (USD/EUR/AED/KWD/…) and prefers sale_price over
        # list price — a signal the existing extract_meta_bundle / _walk
        # paths don't apply consistently. When it yields a valid price we
        # trust it and return immediately. On miss, the existing two
        # JSON-LD/meta paths below still run as fallback (zero regression).
        try:
            from engines.json_ld_extractor import extract as _ld_first
            _ld_data = _ld_first(html)
            if (
                _ld_data
                and _ld_data.get("price")
                and (_ld_data.get("name") or _url_looks_like_product_page(url))
            ):
                row = extract_product(
                    {
                        "name":  _ld_data.get("name", "") or "",
                        "price": _ld_data["price"],
                        "image": _ld_data.get("image", "") or "",
                        "url":   url,
                        "brand": "",
                    },
                    store_url,
                )
                if _has_valid_price(row):
                    return row
        except Exception as _ld_exc:
            logger.debug("json-ld first-extractor error %s: %s", url, _ld_exc)

        # ── JSON-LD + meta (legacy bundle, kept as fallback) ─────────────
        try:
            from utils.competitor_product_scraper import extract_meta_bundle

            bundle = extract_meta_bundle(html, url)
            schema_name = (bundle.get("name") or "").strip()
            og_title = (bundle.get("title") or "").strip()
            label = schema_name or og_title
            if label and (schema_name or _url_looks_like_product_page(url)):
                imgs = bundle.get("images") or []
                img0 = str(imgs[0]).strip() if imgs else ""
                p_raw = bundle.get("price")
                try:
                    p_val = float(p_raw) if p_raw not in (None, "") else 0.0
                except Exception:
                    p_val = 0.0
                row = extract_product(
                    {
                        "name": label,
                        "price": p_val,
                        "sku": bundle.get("sku") or "",
                        "image": img0,
                        "url": url,
                        "brand": bundle.get("brand") or "",
                    },
                    store_url,
                )
                if _has_valid_price(row):
                    return row
        except Exception as exc:
            pass

        ld_acc = _product_fields_from_all_json_ld(html)
        if ld_acc.get("name"):
            imgs = ld_acc.get("images") or []
            im0 = str(imgs[0]).strip() if imgs else ""
            try:
                p_ld = float(ld_acc.get("price") or 0)
            except Exception:
                p_ld = 0.0
            row = extract_product(
                {
                    "name": str(ld_acc.get("name", "")).strip(),
                    "price": p_ld,
                    "sku": ld_acc.get("sku") or "",
                    "image": im0,
                    "url": url,
                    "brand": ld_acc.get("brand") or "",
                },
                store_url,
            )
            if _has_valid_price(row):
                return row

        # ── og:meta + h1 ───────────────────────────────────────────────────
        def _meta(pattern: _re.Pattern) -> str:
            m = pattern.search(html)
            return m.group(1).strip() if m else ""

        pname = _meta(_RE_OG_TITLE)
        pimg = _meta(_RE_OG_IMAGE)
        purl = _meta(_RE_OG_URL) or url
        pprice_raw = _meta(_RE_OG_PRICE)
        try:
            pprice = float(pprice_raw.replace(",", "").strip()) if pprice_raw else 0.0
        except Exception:
            pprice = 0.0

        if pprice == 0.0:
            price_match = _RE_PRICE_SPAN.search(html)
            if price_match:
                try:
                    pprice = float(price_match.group(1).replace(",", "").replace(" ", ""))
                except Exception:
                    pprice = 0.0

        if not pname:
            h1_match = _RE_H1_PRODUCT.search(html)
            if h1_match:
                pname = h1_match.group(1).strip()

        if pname and _url_looks_like_product_page(url):
            row = extract_product(
                {"name": pname, "image": pimg, "url": purl, "price": pprice},
                store_url,
            )
            if _has_valid_price(row):
                return row

        # AI fallback: محاولة استخراج ذكي عندما تفشل كل الطرق التقليدية
        try:
            from engines.ai_engine import ai_fallback_scrape
            ai_data = ai_fallback_scrape(html, url) if html else {}
            if isinstance(ai_data, dict) and not ai_data.get("error"):
                row = extract_product(
                    {
                        "name": ai_data.get("name", ""),
                        "price": ai_data.get("price", 0),
                        "url": url,
                        "image": "",
                        "brand": "",
                    },
                    store_url,
                )
                if _has_valid_price(row):
                    return row
        except Exception:
            pass

        try:
            loop = asyncio.get_running_loop()
            # Use dedicated executor — same reason as sync fallback above.
            # Hard absolute timeout so v30 Selenium/sync fallbacks can never
            # hang a worker thread forever on blocked targets.
            v30_result = await asyncio.wait_for(
                loop.run_in_executor(
                    _SYNC_EXECUTOR,
                    lambda: _run_v30_sync(url, store_url),
                ),
                timeout=25.0,
            )
            v30_row = _v30_row_from_result(v30_result, store_url)
            if _has_valid_price(v30_row):
                return v30_row
        except Exception:
            logger.debug("v30 async fallback failed for %s: %s", url, traceback.format_exc())

        # ── Last-resort: name-only row ────────────────────────────────────
        # If we have a product name from JSON-LD/OG/H1 but no extractable
        # price, still return the row with price=0 so it is persisted. The
        # advanced scraper (v30.2 — "كشط الأسعار المفقودة") is designed to
        # backfill prices for rows with missing price. Without this, sites
        # that obscure prices produce 0 rows + high errors — misclassified.
        try:
            _fallback_name = ""
            _fallback_img = ""
            _fallback_brand = ""
            if 'ld_acc' in locals() and isinstance(ld_acc, dict) and ld_acc.get("name"):
                _fallback_name = str(ld_acc.get("name", "")).strip()
                _imgs = ld_acc.get("images") or []
                _fallback_img = str(_imgs[0]).strip() if _imgs else ""
                _fallback_brand = str(ld_acc.get("brand") or "")
            elif 'pname' in locals() and pname:
                _fallback_name = str(pname).strip()
                _fallback_img = str(pimg or "") if 'pimg' in locals() else ""
            if _fallback_name and _url_looks_like_product_page(url):
                return extract_product(
                    {
                        "name":  _fallback_name,
                        "price": 0.0,
                        "image": _fallback_img,
                        "url":   url,
                        "brand": _fallback_brand,
                    },
                    store_url,
                )
        except Exception:
            pass

        return None


# ══════════════════════════════════════════════════════════════════════════════
#  كاشط متجر واحد مع نقاط استئناف
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_one_store(
    store_url: str,
    progress: Progress,
    state: ScraperState,
    concurrency: int = 3,
    max_products: int = 0,
    resume: bool = True,
    single_mode: bool = False,
    output_queue: "Optional[asyncio.Queue[Any]]" = None,
    external_session: Optional[aiohttp.ClientSession] = None,
) -> List[dict]:
    # output_queue: when provided, each scraped row is also forwarded to the queue
    # immediately (streaming mode). The batch list (rows) is still built for
    # internal checkpointing and the return value — callers in streaming mode
    # can safely ignore the return value.
    domain = _domain(store_url)
    cp     = state.get(domain, store_url)

    # Architectural Fix: NEVER skip if max_products is explicitly requested or in single UI mode
    _force_run = (max_products > 0) or single_mode
    
    if resume and cp.status == "done" and not _force_run:
        logger.info(f"⏭️ {domain} — مكتمل ({cp.rows_saved} منتج)")
        return []

    cp.status     = "running"
    cp.started_at = cp.started_at or datetime.now().isoformat()
    state.save()

    try:
        from engines.sitemap_resolve import (
            SitemapDiscoveryError,
            resolve_product_entries,
        )
    except ImportError:
        logger.error("تعذّر تحميل engines.sitemap_resolve")
        state.mark_error(domain, "import_error")
        _bump_skip(progress, "import_error")
        progress.save()
        return []

    # Phase 1 (2026-04-19): smart lastmod-based incremental scraping.
    # utils.sitemap_cache persists {url -> lastmod} per store; we skip URLs
    # whose lastmod is unchanged since the last successful scrape.
    try:
        from utils import sitemap_cache as _sitemap_cache
        _SITEMAP_CACHE_AVAILABLE = True
    except ImportError:
        _sitemap_cache = None  # type: ignore
        _SITEMAP_CACHE_AVAILABLE = False
        logger.warning("⚠️ utils.sitemap_cache غير متاح — كشط كامل بدون تحديث ذكي")

    own_session = external_session is None
    connector: aiohttp.TCPConnector | None = None
    session: aiohttp.ClientSession | None = None
    # Always defined — code after `finally` and error paths must never hit NameError
    rows: List[dict] = []
    store_http_status: Dict[str, int] = {"403": 0, "429": 0}

    # ── Initialize async curl_cffi fetcher (browser_like_http) ──────────
    _blh = None
    try:
        from browser_like_http import AsyncScraperHTTP
        _blh = AsyncScraperHTTP()
        await _blh.__aenter__()
        _blh_fetcher_var.set(_blh)
        logger.info(f"🛡️ {domain} — AsyncScraperHTTP (chrome104) مفعّل")
    except Exception as _blh_err:
        logger.debug("browser_like_http unavailable: %s", _blh_err)
        _blh_fetcher_var.set(None)

    try:
        if external_session is not None:
            session = external_session
        else:
            connector = aiohttp.TCPConnector(ssl=False, limit=max(100, concurrency * 5))
            session = aiohttp.ClientSession(
                connector=connector,
                connector_owner=True,
                timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=15),
            )

        progress.current_store    = domain
        progress.store_urls_done  = 0
        progress.store_urls_total = 0
        progress.save()

        logger.info(f"🗺️ {domain} — يحلل Sitemap عبر anti-ban على نفس الجلسة…")
        # Phase 1: always return entries (url + lastmod) so we can diff vs cache.
        all_entries: list = []
        try:
            all_entries = await asyncio.wait_for(
                resolve_product_entries(store_url, session),
                timeout=400,
            )
        except asyncio.TimeoutError:
            state.mark_error(domain, "sitemap_timeout")
            _bump_skip(progress, "sitemap_timeout")
            progress.save()
            return []
        except SitemapDiscoveryError as exc:
            logger.error("🛑 %s — Sitemap محجوب أو غير متاح: %s", domain, exc)
            state.mark_error(domain, str(exc)[:200])
            _bump_skip(progress, "sitemap_blocked")
            progress.save()
            return []
        except Exception:
            state.mark_error(domain, traceback.format_exc()[:150])
            _bump_skip(progress, "sitemap_error")
            progress.save()
            return []

        all_urls: List[str] = [e.url for e in all_entries]

        # Record discovered count BEFORE the empty-check — evidence that
        # sitemap resolution did (or did not) yield URLs for this domain.
        progress.urls_discovered += int(len(all_urls or []))

        # ── Phase 1: smart incremental skip via lastmod cache ────────────
        # Safety rails:
        #   - Only skip if cache is non-empty (first run → full scrape).
        #   - Require ≥60% lastmod coverage from the sitemap; below that,
        #     we can't trust the skip decision (would miss real price changes).
        #   - Skip is disabled for forced runs (UI single-store / max_products).
        if (
            _SITEMAP_CACHE_AVAILABLE
            and not _force_run
            and all_entries
        ):
            try:
                _old_cache = _sitemap_cache.load(store_url).get("urls", {}) or {}
                _with_lastmod = sum(
                    1 for _e in all_entries if (getattr(_e, "lastmod", "") or "").strip()
                )
                _coverage = _with_lastmod / max(len(all_entries), 1)
                _cache_populated = len(_old_cache) > 0
                _MIN_COVERAGE = float(os.environ.get("SITEMAP_LASTMOD_MIN_COVERAGE", "0.6"))

                if _cache_populated and _coverage >= _MIN_COVERAGE:
                    added, modified, unchanged = _sitemap_cache.diff(_old_cache, all_entries)
                    if unchanged:
                        # dict.fromkeys preserves first-seen order and dedups.
                        _target = list(dict.fromkeys(list(added) + list(modified)))
                        logger.info(
                            "🧠 %s — تحديث ذكي: %d جديد + %d معدّل، تخطي %d بدون تغيير "
                            "(تغطية lastmod %.0f%%)",
                            domain, len(added), len(modified), len(unchanged),
                            _coverage * 100,
                        )
                        _bump_skip(progress, "unchanged_lastmod", len(unchanged))
                        all_urls = _target
                elif _cache_populated:
                    logger.info(
                        "⚠️ %s — تغطية lastmod ضعيفة (%.0f%% < %.0f%%) — كشط كامل احتياطاً",
                        domain, _coverage * 100, _MIN_COVERAGE * 100,
                    )
            except Exception as _cache_exc:
                logger.debug("sitemap_cache diff failed for %s: %s", domain, _cache_exc)

        # Phase 1: if smart-skip filtered everything out, nothing to do —
        # don't fall into the empty-sitemap Shopify fallback path.
        if not all_urls and all_entries:
            logger.info(
                "✅ %s — لا تغييرات منذ آخر كشط (كل الروابط بـ lastmod مطابق)",
                domain,
            )
            # Refresh cache timestamp so status reflects the check.
            if _SITEMAP_CACHE_AVAILABLE:
                try:
                    _sitemap_cache.merge_after_scrape(store_url, all_entries, set())
                except Exception:
                    pass
            state.mark_done(domain, 0)
            progress.save()
            return []

        if not all_urls:
            logger.warning(f"⚠️ {domain} — لا روابط في Sitemap، يحاول /products.json (Shopify API)...")
            # Shopify fallback: many Saudi stores run Shopify but block sitemaps.
            # /products.json?limit=250&page=X is usually unrestricted.
            shopify_rows: List[dict] = []
            try:
                base = store_url.rstrip("/")
                page = 1
                while True:
                    pj_url = f"{base}/products.json?limit=250&page={page}"
                    try:
                        _pj_resp = await asyncio.wait_for(
                            session.get(
                                pj_url,
                                timeout=aiohttp.ClientTimeout(total=20),
                                ssl=False,
                                headers={"User-Agent": "Mozilla/5.0"},
                                allow_redirects=True,
                            ),
                            timeout=25,
                        )
                    except Exception:
                        break
                    async with _pj_resp:
                        if _pj_resp.status != 200:
                            break
                        try:
                            pj_data = await _pj_resp.json(content_type=None)
                        except Exception:
                            break
                    prods = pj_data.get("products", [])
                    if not prods:
                        break
                    for prod in prods:
                        variants = prod.get("variants", [{}])
                        best_variant = next(
                            (v for v in variants if v.get("available", True)), variants[0] if variants else {}
                        )
                        try:
                            price = float(best_variant.get("price") or 0)
                        except Exception:
                            price = 0.0
                        name = str(prod.get("title") or "").strip()
                        if not name:
                            continue
                        handle = prod.get("handle", "")
                        prod_url = f"{base}/products/{handle}" if handle else ""
                        images = prod.get("images", [])
                        img = str(images[0].get("src", "")) if images else ""
                        row = extract_product(
                            {"name": name, "price": price, "url": prod_url,
                             "image": img, "brand": prod.get("vendor", ""),
                             "sku": str(best_variant.get("sku", ""))},
                            store_url,
                        )
                        if row:
                            shopify_rows.append(row)
                    if len(prods) < 250:
                        break
                    page += 1
            except Exception as _shopify_exc:
                logger.debug("Shopify products.json fallback failed for %s: %s", domain, _shopify_exc)

            if shopify_rows:
                logger.info(f"✅ {domain} — Shopify fallback: {len(shopify_rows)} منتج من products.json")
                state.mark_done(domain, len(shopify_rows))
                return shopify_rows

            logger.warning(f"⚠️ {domain} — Sitemap فارغ وفشل Shopify fallback")
            state.mark_error(domain, "empty_sitemap")
            _bump_skip(progress, "empty_sitemap")
            progress.save()
            return []

        total = len(all_urls)

        # ── Resumption from DB (URL-level) ───────────────────────────────
        # Independent of scraper_state.json / sitemap_cache: if the caller
        # re-triggers a scrape for the same store, skip URLs already saved
        # today with price>0 in competitor_products_store. This survives
        # container restarts and lost/corrupt state files.
        if resume and not _force_run:
            try:
                from utils.db_manager import get_scraped_urls_today
                _done_urls = get_scraped_urls_today(domain)
            except Exception:
                _done_urls = set()
            if _done_urls:
                before = len(all_urls)
                all_urls = [u for u in all_urls if u not in _done_urls]
                skipped_db = before - len(all_urls)
                if skipped_db:
                    logger.info(
                        f"♻️ {domain} — DB-resume: تخطّي {skipped_db} رابط "
                        f"كُشط بنجاح اليوم. المتبقي: {len(all_urls)}"
                    )
                    _bump_skip(progress, "db_resume_done", skipped_db)
                total = len(all_urls)  # recompute after filter
                if not all_urls:
                    logger.info(
                        f"✅ {domain} — كل الروابط مكتملة في DB اليوم."
                    )
                    state.mark_done(domain, 0)
                    progress.save()
                    return []

        # Reset checkpoint if forced run
        resume_idx = 0 if _force_run else (cp.last_url_index if (resume and cp.last_url_index > 0) else 0)
        
        if resume_idx > 0:
            logger.info(f"🔄 {domain} — استئناف من الرابط {resume_idx}/{total}")
        pending_urls = all_urls[resume_idx:]

        # Evidence counters: URLs skipped by resume checkpoint vs enqueued now
        if resume_idx > 0:
            _bump_skip(progress, "resume_done", resume_idx)
        progress.urls_enqueued += len(pending_urls)

        state.update(domain, urls_total=total, urls_done=resume_idx)
        progress.urls_total        += total
        progress.store_urls_total  = total

        semaphore         = asyncio.Semaphore(concurrency)
        done_count        = resume_idx
        checkpoint_every  = max(50, min(200, total // 10 + 1))

        _TASK_TIMEOUT = 60.0  # Phase 2: per-URL timeout (was 45)

        # ── Phase 2: Circuit Breaker state ─────────────────────────
        _consecutive_failures = 0
        _CIRCUIT_BREAKER_LIMIT = 20  # break after 20 consecutive failed URLs
        _circuit_broken = False

        # ── Real-time commit buffer (configurable; default: every 5 rows) ──
        # Decouples commit cadence from the parallel batch size so we persist
        # to SQLite as rows stream in, not only at end-of-batch.
        _commit_batch_size = max(1, int(os.environ.get("ASYNC_SCRAPER_COMMIT_BATCH", "5")))
        _pending_db_rows: list[dict] = []
        _db_flush_lock = asyncio.Lock()

        async def _flush_pending_rows(force: bool = False) -> None:
            """Commit the streaming buffer to SQLite (skips phantom rows)."""
            async with _db_flush_lock:
                if not _pending_db_rows:
                    return
                if not force and len(_pending_db_rows) < _commit_batch_size:
                    return
                batch = list(_pending_db_rows)
                _pending_db_rows.clear()
            try:
                from utils.db_manager import (
                    upsert_competitor_products,
                    normalize_scraped_row_for_db,
                )
                _db_rows = [
                    nr for r in batch
                    if (nr := normalize_scraped_row_for_db(r, domain)) is not None
                ]
                if not _db_rows:
                    return
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda rows=_db_rows: upsert_competitor_products(domain, rows),
                )
            except Exception as _db_exc:
                logger.debug("SQLite real-time write error: %s", _db_exc)

        async def _fetch_one(url: str) -> None:
            nonlocal done_count, _consecutive_failures, _circuit_broken
            progress.urls_attempted += 1
            try:
                row = await asyncio.wait_for(
                    fetch_product(
                        session,
                        url,
                        store_url,
                        semaphore,
                        http_status_counters=store_http_status,
                    ),
                    timeout=_TASK_TIMEOUT,
                )
                if row:
                    rows.append(row)
                    progress.rows_saved_run = int(getattr(progress, "rows_saved_run", 0) or 0) + 1
                    # Stream into DB buffer — flush at _commit_batch_size
                    async with _db_flush_lock:
                        _pending_db_rows.append(row)
                    await _flush_pending_rows(force=False)
                    if output_queue is not None:
                        # Streaming mode: forward immediately to caller.
                        # asyncio.Queue(maxsize=200) provides natural backpressure —
                        # if the consumer is slow we slow down gracefully.
                        await output_queue.put(row)
                    _consecutive_failures = 0  # Phase 2: reset on success
                else:
                    _consecutive_failures += 1
            except asyncio.TimeoutError:
                logger.debug("URL timeout (%ss): %s", _TASK_TIMEOUT, url)
                progress.fetch_exceptions += 1
                _consecutive_failures += 1
            except Exception:
                progress.fetch_exceptions += 1
                progress.last_error = traceback.format_exc()[:500]
                _consecutive_failures += 1
            finally:
                done_count += 1
                progress.urls_processed  += 1
                progress.store_urls_done  = done_count

                if done_count % 10 == 0 or done_count >= total:
                    safe = progress.urls_processed
                    progress.success_rate_pct = (
                        (safe - progress.fetch_exceptions) / safe * 100 if safe else 0
                    )
                    progress.save()
                    _write_live_progress(domain, {
                        "urls_done":  done_count,
                        "urls_total": total,
                        "rows_saved": len(rows),
                        "pct":        min(100, int(done_count / max(total, 1) * 100)),
                        "updated_at": datetime.now().isoformat()[:19],
                    })

                if done_count % checkpoint_every == 0 and not _force_run:
                    state.update(
                        domain,
                        last_url_index=done_count,
                        urls_done=done_count,
                    )
                    logger.info(
                        f"💾 {domain} — نقطة @ {done_count}/{total} | {len(rows)} منتج"
                    )

                # Phase 2: trip circuit breaker (checked after batch)
                if _consecutive_failures >= _CIRCUIT_BREAKER_LIMIT:
                    _circuit_broken = True

        # ── Wall-clock timeout — ONLY when a product cap is set ──────────────
        # max_products == 0 means "scrape everything" — no ceiling, no timeout.
        # Imposing a timeout on a 50k-product store would abort it mid-run and drop
        # tens of thousands of products. Only cap if the caller explicitly asked for N.
        if max_products > 0:
            _STORE_WALL_TIMEOUT: Optional[float] = max(600, min(2700, len(pending_urls) * 3))
        else:
            _STORE_WALL_TIMEOUT = None   # TRUE INFINITY — no wall-clock cap

        async def _run_batches():
            nonlocal _circuit_broken
            BATCH = 50
            for start in range(0, len(pending_urls), BATCH):
                if max_products > 0 and len(rows) >= max_products:
                    logger.info(f"🛑 {domain} — تم الوصول للحد الأقصى ({max_products}). جاري إيقاف السحب.")
                    _skipped = max(0, len(pending_urls) - start)
                    if _skipped:
                        _bump_skip(progress, "max_reached", _skipped)
                    rows[:] = rows[:max_products]
                    break

                # Circuit Breaker — save partial results and exit
                if _circuit_broken:
                    logger.warning(
                        f"🔌 {domain} — Circuit Breaker: {_CIRCUIT_BREAKER_LIMIT} فشل متتالي. "
                        f"تم إنقاذ {len(rows)} منتج ناجح."
                    )
                    _skipped = max(0, len(pending_urls) - start)
                    if _skipped:
                        _bump_skip(progress, "circuit_broken", _skipped)
                    break

                batch = pending_urls[start: start + BATCH]

                await asyncio.gather(*[_fetch_one(u) for u in batch], return_exceptions=True)

                # Real-time commits already happen inside _fetch_one every
                # _commit_batch_size rows. Flush any stragglers from this batch.
                await _flush_pending_rows(force=True)

                recent_blocks = int(store_http_status.get("403", 0)) + int(store_http_status.get("429", 0))
                recent_processed = start + len(batch)
                block_rate = recent_blocks / max(recent_processed, 1)

                if block_rate > 0.3:
                    adaptive_delay = 5.0
                    logger.warning(f"[Anti-Ban] حظر عالي ({block_rate:.2f}). تبريد {adaptive_delay} ثوانِ")
                elif block_rate > 0.1:
                    adaptive_delay = 2.0
                else:
                    adaptive_delay = 0.5

                if start + BATCH < len(pending_urls) and (max_products == 0 or len(rows) < max_products):
                    await asyncio.sleep(adaptive_delay)

        # Run batches — with timeout only when a product cap was requested
        if _STORE_WALL_TIMEOUT is not None:
            try:
                await asyncio.wait_for(_run_batches(), timeout=_STORE_WALL_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    f"⏰ {domain} — Store wall-clock timeout ({_STORE_WALL_TIMEOUT}s). "
                    f"Partial save: {len(rows)} products rescued."
                )
                # Partial results in `rows` are preserved — fall through to save
        else:
            # Truly infinite — runs until all URLs are done or circuit breaker trips
            await _run_batches()

    finally:
        # Guaranteed final flush of any rows still in the streaming buffer —
        # protects against data loss on crash, timeout, or circuit-breaker exit.
        try:
            await _flush_pending_rows(force=True)
        except Exception:
            logger.debug("final flush failed", exc_info=True)
        if own_session and session is not None and isinstance(session, aiohttp.ClientSession):
            if not session.closed:
                await session.close()
        # Close AsyncScraperHTTP (browser_like_http)
        if _blh is not None:
            try:
                await _blh.__aexit__(None, None, None)
            except Exception:
                pass
            _blh_fetcher_var.set(None)
        await asyncio.sleep(0.25)

    if not _force_run:
        state.mark_done(domain, len(rows))

    # Phase 1 + 2 Bug-0: persist lastmod cache for next run's smart-skip.
    # Successful URLs → refresh lastmod. URLs we *attempted* but failed are
    # passed separately so merge_after_scrape can force a retry next round
    # (up to MAX_FAIL_COUNT) instead of the old silent-skip behavior.
    if _SITEMAP_CACHE_AVAILABLE and all_entries:
        try:
            _successful_urls = {r.get("url") for r in rows if r and r.get("url")}
            # Attempted = URLs in all_urls (post smart-skip filter); failed = attempted - success.
            _attempted_urls = set(all_urls or [])
            _failed_urls = _attempted_urls - _successful_urls
            _sitemap_cache.merge_after_scrape(
                store_url,
                all_entries,
                _successful_urls,
                attempted_but_failed_urls=_failed_urls,
            )
        except Exception as _cache_exc:
            logger.debug("sitemap_cache merge failed for %s: %s", domain, _cache_exc)

    # Merge counts from the rate-limiter (which also sees 403/429 responses
    # handled internally by fetch_with_retry) so the UI diagnostic reflects
    # total HTTP blocks, not only those observed in the outer response.
    _rl_blocks = {"403": 0, "429": 0, "5xx": 0}
    try:
        if _ANTI_BAN_AVAILABLE:
            from scrapers.anti_ban import get_rate_limiter as _grl
            _rl_blocks = _grl().get_block_counts(domain) or _rl_blocks
    except Exception:
        pass
    progress.stores_http_errors[domain] = {
        "403": max(int(store_http_status.get("403", 0)), int(_rl_blocks.get("403", 0))),
        "429": max(int(store_http_status.get("429", 0)), int(_rl_blocks.get("429", 0))),
        "5xx": int(_rl_blocks.get("5xx", 0)),
    }
    progress.save()
    logger.info(f"✅ {domain} — {len(rows)} منتج")
    return rows


# ══════════════════════════════════════════════════════════════════════════════
#  Real-Time Streaming Generator (Phase 2 — Task 2.3)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_one_store_streaming(
    store_url: str,
    concurrency: int = 10,
    max_products: int = 0,
    client_session: Optional[aiohttp.ClientSession] = None,
) -> AsyncGenerator[dict, None]:
    """
    Async generator: yields each scraped product dict immediately upon discovery.

    Zero duplication: wraps scrape_one_store() with an asyncio.Queue bridge
    (maxsize=200 rows for backpressure).  The batch system is preserved —
    scrape_one_store() still builds its internal rows[] for checkpointing;
    this generator simply also forwards each row to the caller in real-time.
    Optional ``client_session`` reuses a shared aiohttp session (multi-store
    realtime pipeline) to avoid per-store TCP/TLS setup overhead.

    Usage:
        async for row in scrape_one_store_streaming(url):
            process(row)          # row arrives within seconds of being scraped

    The generator completes when scrape_one_store() finishes (all URLs done,
    circuit-breaker tripped, wall-clock timeout, or an unhandled error).
    """
    _SENTINEL = object()  # signals producer completion — never yielded to caller
    # Unbounded queue: backpressure comes from the Semaphore inside scrape_one_store,
    # NOT from the queue. A maxsize=200 cap was blocking _fetch_one tasks inside
    # asyncio.gather, serialising the scraping and destroying concurrency.
    queue: asyncio.Queue = asyncio.Queue()

    # Fresh Progress + ScraperState for this streaming session
    domain   = _domain(store_url)
    state    = ScraperState()
    state.reset(domain)
    progress = Progress(
        running=True,
        started_at=datetime.now().isoformat(),
        stores_total=1,
        current_store=domain,
        phase="streaming",
    )
    progress.save()

    async def _producer() -> None:
        """Runs scrape_one_store() and always puts the sentinel when done."""
        try:
            await scrape_one_store(
                store_url,
                progress,
                state,
                concurrency=concurrency,
                max_products=max_products,
                resume=False,        # streaming = always fresh
                single_mode=True,
                output_queue=queue,
                external_session=client_session,
            )
        except Exception:
            logger.error(
                "scrape_one_store_streaming producer error for %s: %s",
                domain, traceback.format_exc()[:200],
            )
        finally:
            await queue.put(_SENTINEL)  # always signal completion

    producer_task = asyncio.create_task(_producer())

    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            yield item  # deliver row to caller immediately
    finally:
        # Cleanup: cancel producer if consumer exits early (e.g., max reached)
        if not producer_task.done():
            producer_task.cancel()
            try:
                await producer_task
            except asyncio.CancelledError:
                pass
        progress.running     = False
        _finalize_progress_phase(progress)
        progress.finished_at = datetime.now().isoformat()
        progress.save()


# ══════════════════════════════════════════════════════════════════════════════
#  كشط متجر مفرد (تُستدعى من زر الواجهة)
# ══════════════════════════════════════════════════════════════════════════════

def run_single_store(
    store_url: str,
    concurrency: int = 10,
    max_products: int = 0,
    force: bool = False,
) -> dict:
    domain = _domain(store_url)
    state  = ScraperState()
    
    # Always reset state for single store runs to avoid stale 'done' skips
    state.reset(domain)

    progress = Progress(
        running=True,
        started_at=datetime.now().isoformat(),
        stores_total=1,
        current_store=domain,
        phase="discovering",
    )
    progress.save()

    try:
        progress.phase = "scraping"
        progress.save()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Hard ceiling per store: 2 hours. Prevents the daemon thread from
        # hanging indefinitely when the event loop stalls (e.g. after thread-pool
        # exhaustion caused by many concurrent sync fallback calls).
        _SINGLE_STORE_TIMEOUT = 7200

        async def _scrape_with_timeout():
            return await asyncio.wait_for(
                scrape_one_store(
                    store_url, progress, state,
                    concurrency=concurrency,
                    max_products=max_products,
                    resume=False,
                    single_mode=True,
                ),
                timeout=_SINGLE_STORE_TIMEOUT,
            )

        rows = loop.run_until_complete(_scrape_with_timeout())
    except asyncio.TimeoutError:
        msg = f"⏰ {domain} — Store timeout after {_SINGLE_STORE_TIMEOUT}s"
        logger.warning(msg)
        progress.running = False
        progress.phase = "timeout"
        progress.finished_at = datetime.now().isoformat()
        progress.last_error = msg
        progress.save()
        state.mark_error(domain, "store_timeout")
        return {"success": False, "rows": 0, "message": msg, "domain": domain}
    except Exception:
        progress.running = False
        progress.phase = "failed"
        progress.finished_at = datetime.now().isoformat()
        progress.last_error = traceback.format_exc()[:300]
        progress.save()
        state.mark_error(domain, traceback.format_exc())
        return {"success": False, "rows": 0, "message": traceback.format_exc(), "domain": domain}
    finally:
        try:
            loop.close()
        except Exception:
            pass

    n = _merge_rows_to_csv(rows, domain)
    progress.running      = False
    # Run-scoped authoritative count for single-store mode
    progress.rows_saved_run = len(rows)
    _finalize_progress_phase(progress)
    progress.finished_at  = datetime.now().isoformat()
    progress.stores_done  = 1
    progress.stores_results[domain] = len(rows)
    progress.rows_in_csv  = n
    progress.save()

    # Classify success from the derived terminal phase (never True when 0 rows).
    _ok = progress.phase == "completed" or (progress.phase == "partial" and len(rows) > 0)
    _msg_icon = "✅" if _ok else ("⚠️" if progress.phase == "partial" else "❌")
    return {
        "success": bool(_ok),
        "rows":    len(rows),
        "phase":   progress.phase,
        "message": f"{_msg_icon} {len(rows)} منتج من {domain} — حالة: {progress.phase}",
        "domain":  domain,
    }


def _merge_rows_to_csv(new_rows: List[dict], domain: str) -> int:
    if not new_rows:
        return _count_csv_rows()

    new_df = pd.DataFrame(new_rows)
    for col in CSV_COLS:
        if col not in new_df.columns:
            new_df[col] = ""

    with _CSV_WRITE_LOCK:
        try:
            old_df = pd.read_csv(OUTPUT_CSV, encoding="utf-8-sig", low_memory=False)
            old_df = old_df[old_df["store"].astype(str) != domain]
            combined = pd.concat([old_df, new_df[CSV_COLS]], ignore_index=True)
        except Exception:
            combined = new_df[CSV_COLS]

        combined.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    
    # v26.0 — Persistent Store Sync
    # Phase 2 Item 5: route through canonical normalizer for consistency
    # with the per-batch upsert path (drops name-less / zero-price rows).
    try:
        from utils.db_manager import (
            upsert_competitor_products,
            normalize_scraped_row_for_db,
        )
        _normalized = [
            nr for r in new_rows
            if (nr := normalize_scraped_row_for_db(r, domain)) is not None
        ]
        if _normalized:
            upsert_competitor_products(domain, _normalized)
    except Exception as e:
        logger.warning(f"⚠️ فشل مزامنة قاعدة البيانات لـ {domain}: {e}")
        
    return len(combined)


def _count_csv_rows() -> int:
    try:
        with open(OUTPUT_CSV, encoding="utf-8-sig") as f:
            return sum(1 for _ in f) - 1
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════════════
#  حلقة الكشط الرئيسية (كل المتاجر)
# ══════════════════════════════════════════════════════════════════════════════

async def run_scraper(
    concurrency: int = 10,
    max_products: int = 0,
    resume: bool = True,
    parallel_stores: int = 5,
) -> None:
    """
    Main scraper loop.

    Args:
        concurrency:     max simultaneous URL fetches *per store*.
        max_products:    cap per store (0 = unlimited).
        resume:          honour checkpoints from previous runs.
        parallel_stores: 1 = sequential (one store after another).  Values > 1
                         enable War Machine mode: every store runs at once on a
                         single shared aiohttp.ClientSession (TCPConnector limit 100).
                         CLI default is 5 so full runs start parallel unless set to 1.
    """
    try:
        with open(COMPETITORS_FILE, encoding="utf-8") as f:
            stores: List[str] = json.load(f)
    except Exception:
        stores = []

    if not stores:
        logger.error("لا توجد متاجر في competitors_list.json")
        return

    state = ScraperState()

    # Architectural Fix: If max_products is set, we must force a fresh scrape
    _effective_resume = resume and (max_products == 0)

    if not _effective_resume:
        logger.info("🗑️ تم تعطيل الاستئناف لإجبار جلب البيانات الجديدة (تحديث محدود/مُجبر)")
        state.reset()

    progress = Progress(
        running=True,
        started_at=datetime.now().isoformat(),
        stores_total=len(stores),
        phase="discovering",
    )
    progress.save()

    # ── Sequential mode (original behaviour, parallel_stores == 1) ──────────
    if parallel_stores <= 1:
        for i, store_url in enumerate(stores, 1):
            domain = _domain(store_url)
            logger.info(f"\n{'═'*60}\n🏪 [{i}/{len(stores)}] {domain}\n{'═'*60}")
            progress.stores_done   = i - 1
            progress.current_store = domain
            progress.phase         = "scraping"
            progress.save()

            # Store-level exception isolation — one store crashing
            # must never kill the entire scraper run
            try:
                rows = await scrape_one_store(
                    store_url, progress, state,
                    concurrency=concurrency,
                    max_products=max_products,
                    resume=_effective_resume,
                )
            except Exception as _store_exc:
                logger.error(
                    f"💥 {domain} — Unhandled exception (isolated): {_store_exc}\n"
                    f"{traceback.format_exc()[:300]}"
                )
                state.mark_error(domain, f"unhandled: {str(_store_exc)[:150]}")
                progress.last_error = f"{domain}: {str(_store_exc)[:100]}"
                rows = []

            progress.stores_done = i
            progress.stores_results[domain] = len(rows)
            progress.rows_in_csv = _merge_rows_to_csv(rows, domain)
            progress.save()

    # ── Parallel mode (parallel_stores > 1) ─────────────────────────────────
    else:
        _stores_done_count = 0
        # High-concurrency connector: 25 stores × 20 concurrent each = 500 total.
        # limit_per_host=50 caps per-domain to avoid WAF rate-limit triggers.
        # The old limit=100 caused connection starvation for >5 simultaneous stores.
        _parallel_connector = aiohttp.TCPConnector(
            ssl=False,
            limit=500,              # total concurrent connections
            limit_per_host=50,      # per-domain cap (anti-ban)
            enable_cleanup_closed=True,
            keepalive_timeout=30,
        )
        _parallel_session = aiohttp.ClientSession(
            connector=_parallel_connector,
            connector_owner=True,
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
        )

        async def _scrape_store_guarded(idx: int, store_url: str) -> None:
            nonlocal _stores_done_count
            domain = _domain(store_url)

            logger.info(
                f"\n{'═'*60}\n"
                f"🏪 [{idx+1}/{len(stores)}] {domain} [parallel / shared session]\n"
                f"{'═'*60}"
            )
            progress.current_store = domain
            progress.phase = "scraping"
            progress.save()

            try:
                rows = await scrape_one_store(
                    store_url, progress, state,
                    concurrency=concurrency,
                    max_products=max_products,
                    resume=_effective_resume,
                    external_session=_parallel_session,
                )
            except Exception as _store_exc:
                logger.error(
                    f"💥 {domain} — Unhandled exception (isolated, parallel): "
                    f"{_store_exc}\n{traceback.format_exc()[:300]}"
                )
                state.mark_error(domain, f"unhandled: {str(_store_exc)[:150]}")
                progress.last_error = f"{domain}: {str(_store_exc)[:100]}"
                rows = []

            _stores_done_count += 1
            progress.stores_done = _stores_done_count
            progress.stores_results[domain] = len(rows)
            progress.rows_in_csv = _merge_rows_to_csv(rows, domain)
            progress.save()

        try:
            store_tasks = [
                _scrape_store_guarded(i, s_url)
                for i, s_url in enumerate(stores)
            ]
            await asyncio.gather(*store_tasks, return_exceptions=True)
        finally:
            if not _parallel_session.closed:
                await _parallel_session.close()

    # ── Finalise ─────────────────────────────────────────────────────────────
    progress.running     = False
    # Reconcile run-scoped count from per-store results (authoritative for
    # sequential+parallel multi-store runs).
    try:
        progress.rows_saved_run = sum(
            int(v or 0) for v in (progress.stores_results or {}).values()
        )
    except Exception:
        pass
    _finalize_progress_phase(progress)
    progress.finished_at = datetime.now().isoformat()
    progress.save()

    summary = state.get_summary()
    logger.info(
        f"\n✅ اكتمل | متاجر: {summary['done']}/{summary['total']} "
        f"| أخطاء: {summary['errors']} "
        f"| منتجات: {progress.rows_in_csv:,}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="محرك كشط مهووس v2.0")
    parser.add_argument("--store", default="",
                        help="رابط متجر واحد (فارغ = كل المتاجر)")
    parser.add_argument("--max-products", type=int, default=0,
                        help="أقصى عدد منتجات لكل متجر (0 = بلا حد)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="عدد الطلبات المتزامنة")
    parser.add_argument("--no-resume", action="store_true",
                        help="إعادة الكشط من الصفر (تجاهل نقاط الاستئناف)")
    parser.add_argument("--reset-state", action="store_true",
                        help="مسح كل نقاط الاستئناف قبل البدء")
    # New: parallel store mode (1 = original sequential, 2-3 = parallel)
    parser.add_argument("--parallel-stores", type=int, default=5,
                        help="عتبة الوضع المتوازي (1=تسلسلي، >1=كل المتاجر دفعة واحدة مع جلسة مشتركة)")
    args = parser.parse_args()

    resume = not args.no_resume
    _write_pid_file()

    try:
        if args.reset_state:
            ScraperState().reset()
            logger.info("🗑️ تم مسح نقاط الاستئناف")

        if args.store:
            result = run_single_store(
                args.store,
                concurrency=args.concurrency,
                max_products=args.max_products,
                force=not resume,
            )
            logger.info(result["message"])
            if not result.get("success", False):
                _mark_progress_failed(result.get("message", "فشل تشغيل الكاشط"))
        else:
            asyncio.run(
                run_scraper(
                    concurrency=args.concurrency,
                    max_products=args.max_products,
                    resume=resume,
                    parallel_stores=args.parallel_stores,
                )
            )
    except Exception:
        _mark_progress_failed(traceback.format_exc())
        raise
    finally:
        _cleanup_pid_file()


if __name__ == "__main__":
    main()
