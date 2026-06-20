"""
engines/scraper_v30_advanced.py — محرك الكشط المتقدم v30.3
══════════════════════════════════════════════════════════════
v30.3 change:
  • limit=0  ⇒ بلا سقف (كشط كل المنتجات بدون سعر). الافتراضي صار 0.
  • SQL: عند limit<=0 لا نُضيف LIMIT — نسحب كل الصفوف المؤهّلة.

v30.2 fixes (retained):
  • Semaphore(8) prevents TCPConnector pool exhaustion
  • SAR-first currency extraction — USD/$  explicitly excluded
  • JSON-LD priceCurrency check
  • Per-request timeout=15s
  • Sync fallback runs in ThreadPoolExecutor to avoid event-loop blocking
  • AI fallback also cleans product names (removes Tester/Sample)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import random
import re
import json
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger("ScraperV30Adv")

# ── Anti-ban integration ─────────────────────────────────────────────────────
try:
    from scrapers.anti_ban import (
        get_browser_headers,
        get_rate_limiter,
        try_all_sync_fallbacks,
        looks_like_bot_challenge,
    )
    _HAS_ANTI_BAN = True
except ImportError:
    _HAS_ANTI_BAN = False

    def get_browser_headers(referer=""):
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/134.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        }

    def looks_like_bot_challenge(html):
        return False

# Thread pool for sync fallbacks — avoids blocking the event loop
_SYNC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="scrv30")
# إغلاق نظيف للمجمّع عند خروج العملية — لمنع تعليق الخيوط
import atexit as _atexit
_atexit.register(lambda: _SYNC_EXECUTOR.shutdown(wait=False))

# ── USD/non-SAR detection ────────────────────────────────────────────────────
_USD_MARKERS = re.compile(r"\$|USD|usd|دولار|euro|EUR|eur|يورو|£|GBP", re.I)


def _line_has_foreign_currency(text: str) -> bool:
    """Returns True if line contains USD/EUR/GBP markers — skip it."""
    return bool(_USD_MARKERS.search(text))


# ══════════════════════════════════════════════════════════════════════════════
#  استخراج الأسعار — SAR-first, USD-excluded
# ══════════════════════════════════════════════════════════════════════════════
class PriceExtractor:

    @staticmethod
    def extract_price(html: str, url: str = "") -> Optional[float]:
        try:
            soup = BeautifulSoup(html, "html.parser")

            # ── Strategy 1: SAR-specific selectors FIRST ─────────────────
            for selector in (
                "span[class*='sar']", "span[class*='ريال']",
                "div[class*='sar']", "div[class*='ريال']",
                "span.s-product-price", "div.s-product-price",
                ".s-price-wrapper span",
                ".s-product-card-sale-price", ".s-product-price-sale",
                ".s-product-card-price", "h4.s-product-card-price",
                "span.s-product-card-price", "[data-product-price]",
                ".product-formatted-price", ".product-price-amount",
                ".product-price .amount", "span.product-price",
                "[data-testid='product-price']", "[data-testid*='price']",
                "p.price ins .woocommerce-Price-amount",
                "p.price .woocommerce-Price-amount",
                "bdi",
            ):
                for elem in soup.select(selector):
                    text = elem.get_text(strip=True)
                    if not _line_has_foreign_currency(text):
                        p = PriceExtractor._parse_sar_text(text)
                        if p:
                            return p

            # ── Strategy 2: Generic price selectors ──────────────────────
            for selector in (
                "span.price", "span.product-price", "div.price", "p.price",
                "span[class*='price']", "div[class*='price']",
                "span.product__price", "span.money",
                ".product-price__value", ".product-price--sale",
            ):
                for elem in soup.select(selector):
                    text = elem.get_text(strip=True)
                    if _line_has_foreign_currency(text):
                        continue
                    p = PriceExtractor._parse_sar_text(text)
                    if p:
                        return p

            # ── Strategy 3: data-* attributes ────────────────────────────
            for attr in ("data-price", "data-product-price", "data-amount", "data-regular-price"):
                for el in soup.find_all(attrs={attr: True}):
                    try:
                        p = float(str(el[attr]).replace(",", ""))
                        if 0 < p < 100_000:
                            return p
                    except (ValueError, TypeError):
                        pass

            # ── Strategy 4: JSON-LD with priceCurrency check ─────────────
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    ld = json.loads(script.string or "")
                    if isinstance(ld, list):
                        for item in ld:
                            if isinstance(item, dict) and "offers" in item:
                                ld = item
                                break
                        else:
                            continue
                    offers = ld.get("offers", ld)
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    currency = str(offers.get("priceCurrency", offers.get("currency", ""))).upper()
                    if currency and currency not in ("SAR", ""):
                        continue
                    for pk in ("price", "lowPrice", "highPrice"):
                        p_str = str(offers.get(pk, ""))
                        if p_str:
                            p = float(p_str.replace(",", ""))
                            if 0 < p < 100_000:
                                return p
                except Exception as e:
                    logger.debug("JSON-LD parse error %s: %s", url, e)

            # ── Strategy 5: Inline JS patterns ───────────────────────────
            # Phase 2 Item 6: unify USD exclusion — if a script explicitly
            # declares a non-SAR priceCurrency (caught and skipped by
            # Strategy 4), we must not then harvest its "price":... via
            # regex below and treat it as SAR.
            for script in soup.find_all("script"):
                txt = script.string or ""
                if len(txt) > 200_000:
                    continue
                _cur_m = re.search(r'"priceCurrency"\s*:\s*"([^"]+)"', txt)
                if _cur_m and _cur_m.group(1).upper() not in ("SAR", ""):
                    continue  # foreign currency declared — skip entire script
                for pattern in (
                    r'"price"\s*:\s*["\']?(\d+(?:\.\d+)?)',
                    r'"sale_price"\s*:\s*["\']?(\d+(?:\.\d+)?)',
                    r'"amount"\s*:\s*["\']?(\d+(?:\.\d+)?)',
                ):
                    m = re.search(pattern, txt)
                    if m:
                        p = float(m.group(1))
                        if 0 < p < 100_000:
                            return p

            # ── Strategy 6: Arabic text lines (SAR keywords, USD excluded)
            text = soup.get_text()
            for line in text.split("\n"):
                line_s = line.strip()
                if not line_s or len(line_s) > 200:
                    continue
                if any(kw in line_s for kw in ("ريال", "رس", "ر.س", "SAR")):
                    if _line_has_foreign_currency(line_s):
                        continue
                    p = PriceExtractor._parse_sar_text(line_s)
                    if p:
                        return p

            return None
        except Exception as e:
            logger.debug(f"price extract error: {e}")
            return None

    @staticmethod
    def _parse_sar_text(text: str) -> Optional[float]:
        try:
            if _line_has_foreign_currency(text):
                return None
            trans = str.maketrans(
                "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
                "01234567890123456789",
            )
            cleaned = (
                text.translate(trans)
                .replace("ريال", "").replace("رس", "").replace("ر.س", "")
                .replace("ر.س.", "").replace("SR", "").replace("SAR", "")
                .replace(",", "").replace("،", "").strip()
            )
            numbers = re.findall(r"\d+(?:\.\d+)?", cleaned)
            if not numbers:
                return None
            for n in numbers:
                try:
                    p = float(n)
                except ValueError:
                    continue
                if 0 < p < 100_000:
                    return p
        except Exception:
            pass
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  AI Fallback — price extraction + name cleaning (cost-guarded)
# ══════════════════════════════════════════════════════════════════════════════
_AI_FALLBACK_BUDGET = 50
_ai_fallback_used = 0
_ai_fallback_lock = threading.Lock()


def _ai_extract_price(text_snippet: str) -> Optional[float]:
    global _ai_fallback_used
    with _ai_fallback_lock:
        if _ai_fallback_used >= _AI_FALLBACK_BUDGET:
            return None
    try:
        from engines.ai_engine import _call_gemini
    except ImportError:
        return None
    snippet = text_snippet[:2500]
    prompt = (
        "Extract the product price in SAR (Saudi Riyals) from this text. "
        "IGNORE any USD or dollar prices. "
        "Reply with ONLY the numeric SAR price (e.g. 299.00). "
        "If no SAR price found, reply with 0.\n\n"
        f"Text:\n{snippet}"
    )
    try:
        resp = _call_gemini(prompt, temperature=0.0, max_tokens=32)
        if resp:
            with _ai_fallback_lock:
                _ai_fallback_used += 1
            nums = re.findall(r"\d+\.?\d*", str(resp).strip())
            if nums:
                p = float(nums[0])
                if 0 < p < 100_000:
                    return p
    except Exception as e:
        logger.debug(f"AI price fallback error: {e}")
    return None


def _ai_clean_product_name(raw_name: str) -> str:
    if not raw_name or len(raw_name) < 3:
        return raw_name
    cleaned = raw_name
    for junk in (
        " - متجر", " | متجر", " – متجر", "| مهووس", "| Mahwous",
        " - خبير العطور", "| خبير", " | سعيد صلاح",
        " - فانيلا", "| فانيلا", "| Vanilla",
        " - Golden Scent", "| Golden Scent",
    ):
        cleaned = cleaned.replace(junk, "")
    cleaned = re.sub(r"\s*[|\-–—]\s*[^\|–—]{0,40}(متجر|store|shop|ستور)\s*$", "", cleaned, flags=re.I)
    return cleaned.strip()


# ══════════════════════════════════════════════════════════════════════════════
#  المحرك — Semaphore-guarded, no pool exhaustion
# ══════════════════════════════════════════════════════════════════════════════
class AdvancedScraper:

    def __init__(self, max_concurrent: int = 8):
        self.session: Optional[aiohttp.ClientSession] = None
        self.price_extractor = PriceExtractor()
        self._rate_limiter = get_rate_limiter() if _HAS_ANTI_BAN else None
        self._sem = asyncio.Semaphore(max_concurrent)

    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=50,
                limit_per_host=8,
                ttl_dns_cache=300,
                ssl=False,
                enable_cleanup_closed=True,
            )
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                connector=connector,
            )

    async def scrape_product_page(self, url: str, store_name: str) -> Dict[str, Any]:
        async with self._sem:
            return await self._scrape_inner(url, store_name)

    async def _scrape_inner(self, url: str, store_name: str) -> Dict[str, Any]:
        domain = urlparse(url).netloc
        html = None

        try:
            if self._rate_limiter:
                await self._rate_limiter.wait(domain)
            else:
                await asyncio.sleep(random.uniform(0.8, 2.5))

            await self._ensure_session()
            headers = get_browser_headers(referer=f"https://{domain}/")

            req_timeout = aiohttp.ClientTimeout(total=15, connect=8)
            async with self.session.get(
                url, headers=headers, ssl=False,
                allow_redirects=True, timeout=req_timeout,
            ) as response:
                if response.status == 200:
                    html = await response.text(errors="ignore")
                    if self._rate_limiter:
                        self._rate_limiter.record_success(domain)
                elif response.status in (404, 410):
                    return self._fail_result(url, store_name)
                else:
                    if self._rate_limiter:
                        self._rate_limiter.record_error(domain, response.status)
        except asyncio.TimeoutError:
            logger.debug(f"timeout: {url}")
        except (aiohttp.ClientError, OSError) as e:
            logger.debug(f"aiohttp error {url}: {type(e).__name__}")

        if not html or (_HAS_ANTI_BAN and looks_like_bot_challenge(html)):
            if _HAS_ANTI_BAN:
                try:
                    loop = asyncio.get_running_loop()
                    html_sync = await asyncio.wait_for(
                        loop.run_in_executor(_SYNC_EXECUTOR, try_all_sync_fallbacks, url, 15),
                        timeout=18.0,
                    )
                    if html_sync:
                        html = html_sync
                except (asyncio.TimeoutError, Exception) as e:
                    logger.debug(f"sync fallback timeout/error {url}: {e}")

        if not html:
            return self._fail_result(url, store_name)

        price = self.price_extractor.extract_price(html, url)

        if not price or price <= 0:
            try:
                text_for_ai = BeautifulSoup(html, "html.parser").get_text()[:3000]
                if text_for_ai.strip():
                    price = _ai_extract_price(text_for_ai)
            except Exception as e:
                logger.debug("AI phase error %s: %s", url, e)

        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return self._fail_result(url, store_name)

        title_tag = soup.find("title")
        raw_name = (
            title_tag.get_text(strip=True) if title_tag
            else url.split("/")[-1].replace("-", " ")
        )
        product_name = _ai_clean_product_name(raw_name)

        image_url = ""
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content", "").startswith("http"):
            image_url = og_img["content"]
        else:
            for img_sel in ("img.product-image", "img[class*='product']"):
                el = soup.select_one(img_sel)
                if el and el.get("src"):
                    image_url = urljoin(url, el["src"])
                    break

        # ── الوصف + SKU + الماركة — meta tags ثم JSON-LD (Schema.org) ──────
        description, sku, brand = self._extract_meta_fields(soup, html)

        return {
            "url": url,
            "store": store_name,
            "product_name": product_name[:200],
            "price": price or 0.0,
            "image_url": image_url,
            "description": description,
            "sku": sku,
            "brand": brand,
            "success": price is not None and price > 0,
        }

    @staticmethod
    def _extract_meta_fields(soup, html: str):
        """يستخرج (الوصف، SKU، الماركة) من meta tags و JSON-LD.

        الأولوية: meta tags (سريعة، أعلى الصفحة) ثم json_ld_extractor كاحتياط
        أكثر دقة (Schema.org Product). يُرجع نصوصاً نظيفة أو "".
        """
        description = sku = brand = ""

        # 1) og:description / meta description
        for finder in (
            lambda: soup.find("meta", property="og:description"),
            lambda: soup.find("meta", attrs={"name": "description"}),
            lambda: soup.find("meta", attrs={"name": "twitter:description"}),
        ):
            tag = finder()
            if tag and tag.get("content", "").strip():
                description = tag["content"].strip()[:1200]
                break

        # 2) SKU من meta tags الشائعة
        sku_tag = (
            soup.find("meta", property="product:retailer_item_id")
            or soup.find("meta", attrs={"name": "sku"})
            or soup.find("meta", property="og:sku")
        )
        if sku_tag and sku_tag.get("content", "").strip():
            sku = sku_tag["content"].strip()[:120]

        # 3) JSON-LD (Schema.org) — أدقّ مصدر للماركة الحقيقية، ويملأ أي نقص.
        #    ملاحظة: متاجر سلة تضع اسم المتجر في product:brand، لذا نُفضّل
        #    ماركة JSON-LD أولاً ثم نرجع لـ meta كاحتياط.
        ld_brand = ""
        try:
            from engines.json_ld_extractor import extract as _ld_extract
            ld = _ld_extract(html) or {}
            ld_brand = str(ld.get("brand", "")).strip()[:80]
            description = description or str(ld.get("description", ""))[:1200]
            sku = sku or str(ld.get("sku", ""))[:120]
        except Exception:
            pass

        if ld_brand:
            brand = ld_brand
        else:
            brand_tag = soup.find("meta", property="product:brand") or soup.find("meta", property="og:brand")
            if brand_tag and brand_tag.get("content", "").strip():
                brand = brand_tag["content"].strip()[:80]

        return description, sku, brand

    @staticmethod
    def _fail_result(url: str, store_name: str) -> Dict[str, Any]:
        return {
            "url": url, "store": store_name,
            "product_name": url.split("/")[-1].replace("-", " "),
            "price": 0.0, "image_url": "",
            "description": "", "sku": "", "brand": "",
            "success": False,
        }

    async def scrape_batch(
        self, urls: List[str], store_name: str,
        progress_cb=None,
    ) -> List[Dict]:
        tasks = [self.scrape_product_page(u, store_name) for u in urls]
        results = []
        total = len(tasks)

        for i, coro in enumerate(asyncio.as_completed(tasks)):
            try:
                r = await coro
                results.append(r)
            except Exception as e:
                logger.debug(f"task exception: {e}")
                results.append(self._fail_result("", store_name))
            if progress_cb and (i + 1) % 5 == 0:
                progress_cb(i + 1, total)

        if progress_cb:
            progress_cb(total, total)
        return results

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


# ══════════════════════════════════════════════════════════════════════════════
#  Main entry — app.py or CLI
# ══════════════════════════════════════════════════════════════════════════════
async def run_advanced_price_scraping(
    store_filter: str = "",
    limit: int = 0,                       # 0 = بلا سقف (كشط كل المنتجات)
    progress_cb=None,
    max_parallel_stores: int = 25,
    flush_every: int = 20,
) -> Dict[str, Any]:
    """
    Scrape products with price=0 across ALL competitor stores in PARALLEL.

    v30.3 — uncapped + parallel:
      • limit <= 0 ⇒ كشط جميع المنتجات المؤهّلة (بلا LIMIT في SQL).
      • All stores run simultaneously (asyncio.gather + Semaphore).
      • Each scraped product is streamed to DB every `flush_every`.
      • progress_cb receives live per-store + global counters.

    Args:
        store_filter         : scrape only this one store (empty = all stores).
        limit                : max products per batch; 0 أو سالب = بلا حد.
        progress_cb          : callable(dict) with live counters.
        max_parallel_stores  : max competitor stores scraped simultaneously.
        flush_every          : flush scraped products to DB every N rows.
    """
    global _ai_fallback_used
    _ai_fallback_used = 0

    from utils.db_manager import get_db, upsert_competitor_products

    conn = get_db()
    try:
        use_limit = bool(limit) and limit > 0
        if store_filter and use_limit:
            rows = conn.execute(
                """SELECT product_url, competitor FROM competitor_products_store
                   WHERE (price IS NULL OR price = 0) AND product_url != '' AND competitor = ?
                   LIMIT ?""",
                (store_filter, limit),
            ).fetchall()
        elif store_filter:
            rows = conn.execute(
                """SELECT product_url, competitor FROM competitor_products_store
                   WHERE (price IS NULL OR price = 0) AND product_url != '' AND competitor = ?""",
                (store_filter,),
            ).fetchall()
        elif use_limit:
            rows = conn.execute(
                """SELECT product_url, competitor FROM competitor_products_store
                   WHERE (price IS NULL OR price = 0) AND product_url != ''
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT product_url, competitor FROM competitor_products_store
                   WHERE (price IS NULL OR price = 0) AND product_url != ''"""
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"total_scraped": 0, "prices_found": 0, "updated_in_db": 0,
                "errors": 0, "ai_used": 0,
                "message": "✅ جميع المنتجات لديها أسعار بالفعل!"}

    urls_by_store: Dict[str, List[str]] = {}
    for url, store in rows:
        urls_by_store.setdefault(store, []).append(url)

    total_target = sum(len(u) for u in urls_by_store.values())
    logger.info("🚀 Parallel scrape across %d stores (target=%d products, max_parallel=%d, limit=%s)",
                len(urls_by_store), total_target, max_parallel_stores,
                "UNCAPPED" if not use_limit else limit)

    counters = {
        "total_done":    0,
        "total_target":  total_target,
        "prices_found":  0,
        "errors":        0,
        "updated_in_db": 0,
        "by_store":      {s: {"done": 0, "total": len(u), "prices": 0}
                          for s, u in urls_by_store.items()},
    }
    counters_lock = asyncio.Lock()

    async def _emit_progress():
        if progress_cb is None:
            return
        try:
            snapshot = {
                "total_done":    counters["total_done"],
                "total_target":  counters["total_target"],
                "prices_found":  counters["prices_found"],
                "errors":        counters["errors"],
                "updated_in_db": counters["updated_in_db"],
                "by_store":      {k: dict(v) for k, v in counters["by_store"].items()},
            }
            progress_cb(snapshot)
        except Exception as _cb_err:
            logger.debug("progress_cb error: %s", _cb_err)

    scraper = AdvancedScraper(max_concurrent=12)
    store_semaphore = asyncio.Semaphore(max_parallel_stores)

    async def _scrape_one_store(store: str, urls: List[str]) -> None:
        async with store_semaphore:
            buffer: List[Dict] = []
            tasks = [scraper.scrape_product_page(u, store) for u in urls]

            for coro in asyncio.as_completed(tasks):
                try:
                    r = await coro
                except Exception as _task_err:
                    logger.debug("store=%s task error: %s", store, _task_err)
                    async with counters_lock:
                        counters["errors"] += 1
                        counters["total_done"] += 1
                        counters["by_store"][store]["done"] += 1
                    await _emit_progress()
                    continue

                found_price = bool(r.get("success") and r.get("price", 0) > 0)
                async with counters_lock:
                    counters["total_done"] += 1
                    counters["by_store"][store]["done"] += 1
                    if found_price:
                        counters["prices_found"] += 1
                        counters["by_store"][store]["prices"] += 1
                    elif not r.get("success"):
                        counters["errors"] += 1

                if found_price:
                    buffer.append({
                        "name":        r["product_name"],
                        "price":       r["price"],
                        "product_url": r["url"],
                        "image_url":   r.get("image_url", ""),
                        "brand":       r.get("brand", ""),
                    })

                if len(buffer) >= flush_every:
                    try:
                        res = upsert_competitor_products(
                            store, buffer, name_key="name", price_key="price")
                        async with counters_lock:
                            counters["updated_in_db"] += (
                                res.get("updated", 0) + res.get("inserted", 0))
                    except Exception as _db_err:
                        logger.error("DB flush error (%s): %s", store, _db_err)
                    buffer.clear()

                await _emit_progress()

            if buffer:
                try:
                    res = upsert_competitor_products(
                        store, buffer, name_key="name", price_key="price")
                    async with counters_lock:
                        counters["updated_in_db"] += (
                            res.get("updated", 0) + res.get("inserted", 0))
                    await _emit_progress()
                except Exception as _db_err:
                    logger.error("DB final flush error (%s): %s", store, _db_err)
            logger.info("✅ %s: %d/%d scraped, %d prices found",
                        store,
                        counters["by_store"][store]["done"],
                        counters["by_store"][store]["total"],
                        counters["by_store"][store]["prices"])

    try:
        await asyncio.gather(
            *[_scrape_one_store(s, u) for s, u in urls_by_store.items()],
            return_exceptions=True,
        )
    finally:
        await scraper.close()

    try:
        from utils.db_manager import trigger_gcs_sync
        trigger_gcs_sync(force=True)
    except Exception:
        pass

    total_scraped = counters["total_done"]
    prices_found  = counters["prices_found"]
    pct = prices_found * 100 // max(total_scraped, 1)
    return {
        "total_scraped": total_scraped,
        "prices_found":  prices_found,
        "updated_in_db": counters["updated_in_db"],
        "errors":        counters["errors"],
        "ai_used":       _ai_fallback_used,
        "by_store":      counters["by_store"],
        "message": (
            f"✅ كشط {total_scraped} | أسعار: {prices_found} ({pct}%) | "
            f"DB: {counters['updated_in_db']} | AI: {_ai_fallback_used}"
        ),
    }


if __name__ == "__main__":
    import sys
    _store = sys.argv[1] if len(sys.argv) > 1 else ""
    _limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0   # 0 = بلا سقف
    print(f"🕷️ Advanced Scraper v30.3 — store={_store or 'ALL'}, limit={_limit or 'UNCAPPED'}")
    result = asyncio.run(run_advanced_price_scraping(_store, _limit))
    print(result.get("message", result))
