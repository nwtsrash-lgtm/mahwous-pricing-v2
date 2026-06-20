"""
engines/sitemap_resolve.py — حل روابط Sitemap v2.1 (2026)  *** MASTER ***
═══════════════════════════════════════════════════════════════════════════
هذا الملف هو المصدر الوحيد للحقيقة (Single Source of Truth).
scrapers/sitemap_resolve.py و make/sitemap_resolve.py مجرد Shims تستورد منه.

يحدّد مسار Sitemap لأي متجر إلكتروني بأولوية:
  1. robots.txt → سطور Sitemap: (المصدر الأكثر شرعية)
  2. مسارات سلة / زد / Shopify / WooCommerce
  3. /sitemap.xml   /sitemap_index.xml  (المعيار العام)

التحسينات في v2.1:
  - Semaphore لتقييد التزامن عند معالجة sitemapindex الضخمة
  - batch processing للـ sub-sitemaps لمنع انفجار coroutines

يُعيد قائمة URLs لمنتجات المتجر جاهزة للكشط مع تاريخ آخر تعديل.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp

# Dedicated executor for sync sitemap fallbacks (cloudscraper / curl_cffi).
# Keeps the shared asyncio default pool free for other awaitable work.
_SITEMAP_SYNC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="sitemap-sync",
)
# إغلاق نظيف للمجمّع عند خروج العملية — لمنع تعليق الخيوط
import atexit as _atexit
_atexit.register(lambda: _SITEMAP_SYNC_EXECUTOR.shutdown(wait=False))

from scrapers.anti_ban import (
    fetch_with_retry,
    get_browser_headers,
    get_xml_headers,
    looks_like_bot_challenge,
    try_all_sync_fallbacks,
)

logger = logging.getLogger(__name__)


class SitemapDiscoveryError(RuntimeError):
    """Sitemap / robots discovery failed after anti-ban retries (often Cloudflare 403/429)."""


def _looks_like_xml(text: str) -> bool:
    t = (text or "").lstrip()[:8000].lower()
    return bool(
        t.startswith("<?xml")
        or "<urlset" in t
        or "<sitemapindex" in t
    )


async def _fetch_sitemap_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    referer: str,
) -> Optional[str]:
    """
    Sitemap / robots body as text: aiohttp + fetch_with_retry first, then
    curl_cffi / cloudscraper sync chain if the response is a bot challenge.
    """
    ref = referer or f"https://{urlparse(url).netloc}/"
    resp = await fetch_with_retry(session, url, max_retries=4, referer=ref)
    if resp is not None:
        try:
            text = await resp.text(errors="ignore")
        finally:
            resp.close()
        if text and (
            _looks_like_xml(text)
            or url.rstrip("/").lower().endswith("/robots.txt")
        ):
            return text
        if text and looks_like_bot_challenge(text):
            logger.warning(
                "[Sitemap] HTTP 200 but bot challenge HTML for %s — trying TLS bypass",
                url,
            )
        elif text and not _looks_like_xml(text) and _loc_xml_path_endswith_xml(url):
            logger.warning(
                "[Sitemap] HTTP 200 but non-XML body for %s — trying TLS bypass",
                url,
            )

    loop = asyncio.get_running_loop()
    # Use dedicated executor and shorter timeout to avoid blocking the shared pool.
    sync_text = await loop.run_in_executor(
        _SITEMAP_SYNC_EXECUTOR,
        lambda u=url: try_all_sync_fallbacks(u, timeout=15),
    )
    if sync_text and (
        _looks_like_xml(sync_text)
        or url.rstrip("/").lower().endswith("/robots.txt")
    ):
        return sync_text
    if sync_text and looks_like_bot_challenge(sync_text):
        logger.error(
            "[Sitemap] BLOCKED after retries and sync fallback (Cloudflare/challenge): %s",
            url,
        )
        return None
    if sync_text and _loc_xml_path_endswith_xml(url) and not _looks_like_xml(sync_text):
        logger.error(
            "[Sitemap] Expected XML from %s but got non-XML after bypass attempts",
            url,
        )
        return None
    return sync_text


# ══════════════════════════════════════════════════════════════════════════
#  ثوابت ومسارات Sitemap
# ══════════════════════════════════════════════════════════════════════════
_SITEMAP_CANDIDATES = [
    "/sitemap_index.xml",
    "/sitemap.xml",
    "/sitemap-products.xml",
    "/products-sitemap.xml",
    "/sitemap_products.xml",
    "/page-sitemap.xml",
    "/product-sitemap.xml",
    "/sitemap1.xml",
]

_SALLA_EXTRA_PATHS = [
    "/sitemap.xml",
    "/sitemap_products.xml",
    "/sitemap-products.xml",
]

_ZID_EXTRA_PATHS = [
    "/sitemap.xml",
    "/sitemap_products.xml",
]

_SALLA_DOMAINS = re.compile(
    r"(salla\.sa|salla\.store|\.salla\.|s\.salla\.sa)", re.I
)
_ZID_DOMAINS = re.compile(
    r"(zid\.store|\.zid\.sa|zid\.sa)", re.I
)

_EXCLUDE_URL_RE = re.compile(
    r"(/blog/|/category/|/categories/|/tag/"
    r"|/cart(?:/|$)|/checkout(?:/|$)"
    r"|/account(?:/|$)|/contact(?:/|$)|/about(?:/|$)"
    r"|/faq(?:/|$)|/privacy(?:/|$)|/terms(?:/|$)"
    r"|/(?:ar|en)/(?:blog|cart|category|categories|tag|account"
    r"|collections?|pages?)(?:/|$)"
    r"|/cdn\."
    r"|/feed(?:/|$)|/rss(?:/|$)|/amp/)",
    re.I,
)

# الحد الأقصى لطلبات sitemap المتزامنة في نفس الوقت
_SITEMAP_CONCURRENCY = 10


@dataclass
class SitemapEntry:
    """رابط منتج مع تاريخ آخر تعديل (اختياري)."""
    url: str
    lastmod: str = ""
    discovered_from: str = ""


@dataclass
class SitemapDiag:
    """تشخيص عملية حل الـ Sitemap — يُعرض في الواجهة."""
    store_url: str = ""
    robots_sitemaps: List[str] = field(default_factory=list)
    sitemap_found: str = ""
    urls_total: int = 0
    urls_product: int = 0
    errors: List[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════
#  دوال مساعدة
# ══════════════════════════════════════════════════════════════════════════
def _base_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _loc_xml_path_endswith_xml(loc_text: str) -> bool:
    """Treat ?query on sitemap URLs as non-part of the file extension (e.g. …/x.xml?from=1)."""
    pth = (urlparse(loc_text).path or "").lower().rstrip("/")
    return pth.endswith(".xml")


def _is_salla(url: str) -> bool:
    return bool(_SALLA_DOMAINS.search(url))


def _is_zid(url: str) -> bool:
    return bool(_ZID_DOMAINS.search(url))


async def _fetch_xml(
    session: aiohttp.ClientSession, url: str
) -> Optional[str]:
    """GET عبر fetch_with_retry + تخطي التحدي عند الحاجة."""
    ref = f"https://{urlparse(url).netloc}/"
    try:
        text = await _fetch_sitemap_text(session, url, referer=ref)
        if not text:
            logger.debug("_fetch_xml empty body %s", url)
            return None
        return text
    except Exception as exc:
        logger.debug("_fetch_xml %s → %s", url, exc)
    return None


def _parse_sitemap_xml(xml_text: str) -> Tuple[List[SitemapEntry], List[str]]:
    """
    يحلل XML ويعيد:
      - entries: قائمة SitemapEntry (url + lastmod)
      - sub_sitemaps: روابط sitemapindex فرعية (تحتاج جلب إضافي)
    """
    entries: List[SitemapEntry] = []
    sub_sitemaps: List[str] = []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return entries, sub_sitemaps

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    if root_tag == "sitemapindex":
        for sitemap_el in root.findall(".//sm:sitemap", ns):
            loc = sitemap_el.find("sm:loc", ns)
            if loc is not None and loc.text:
                sub_sitemaps.append(loc.text.strip())
        if not sub_sitemaps:
            # Fallback: accept ANY <loc> inside sitemapindex (not just .xml paths)
            for el in root.iter():
                tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
                if tag == "loc" and el.text and el.text.strip().startswith("http"):
                    sub_sitemaps.append(el.text.strip())
        return entries, sub_sitemaps

    if root_tag == "urlset":
        for url_el in root.findall(".//sm:url", ns):
            loc = url_el.find("sm:loc", ns)
            lastmod_el = url_el.find("sm:lastmod", ns)
            if loc is not None and loc.text:
                entries.append(SitemapEntry(
                    url=loc.text.strip(),
                    lastmod=(lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else ""),
                ))

    if not entries and not sub_sitemaps:
        for el in root.iter():
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag == "loc" and el.text:
                u = el.text.strip()
                if _loc_xml_path_endswith_xml(u):
                    sub_sitemaps.append(u)
                elif u.startswith("http"):
                    entries.append(SitemapEntry(url=u))

    return entries, sub_sitemaps


async def resolve_sitemap_recursively(
    session: aiohttp.ClientSession,
    sitemap_url: str,
    max_depth: int = 3,
    current_depth: int = 0,
) -> Dict[str, str]:
    """
    يعيد dict: url → رابط ملف الـ urlset الذي احتوى <loc> (لتلميحات الفلترة الاختيارية).
    """
    if current_depth > max_depth:
        return {}
    try:
        referer = f"https://{urlparse(sitemap_url).netloc}/"
        xml_text = await _fetch_sitemap_text(
            session, sitemap_url, referer=referer
        )
        if not xml_text:
            logger.error(
                "[Sitemap] Failed after anti-ban retries (likely 403/429): %s",
                sitemap_url,
            )
            return {}
        root = ET.fromstring(xml_text)

        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]

        merged: Dict[str, str] = {}
        if root.tag == "sitemapindex":
            child_urls: List[str] = []
            for loc in root.findall(".//loc"):
                if not loc.text:
                    continue
                cu = loc.text.strip()
                # BLACKLIST FIX: accept ALL <loc> entries from a sitemapindex —
                # NOT just those ending in .xml. Many platforms (Salla, Zid, WooCommerce)
                # serve sitemaps at URLs like /sitemap/products or /sitemap?type=products.
                # The old `.xml`-only filter was silently dropping these entire sub-sitemaps.
                if cu.startswith("http"):
                    child_urls.append(cu)
            if not child_urls:
                logger.warning(
                    "[Sitemap] sitemapindex has no <loc> child URLs: %s",
                    sitemap_url,
                )

            # Semaphore-controlled concurrency — prevents spawning 200+ tasks at once
            # for large sitemapindex files, which overloads aiohttp and triggers WAF.
            # Phase 1 (2026-04-19): added per-task stagger so the N workers that clear
            # the semaphore on depth-N don't all hit the target origin within the same
            # millisecond window — that burst was slipping past the rate limiter's
            # per-domain delay and triggering Cloudflare 429 on large sitemapindex.
            _sem = asyncio.Semaphore(_SITEMAP_CONCURRENCY)
            _STAGGER_STEP = 0.3  # seconds between child starts

            async def _fetch_child(cu: str, idx: int) -> Dict[str, str]:
                if idx:
                    await asyncio.sleep(_STAGGER_STEP * min(idx, _SITEMAP_CONCURRENCY))
                async with _sem:
                    return await resolve_sitemap_recursively(
                        session, cu, max_depth, current_depth + 1
                    )

            tasks = [
                asyncio.create_task(_fetch_child(cu, i))
                for i, cu in enumerate(child_urls)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for cu, res in zip(child_urls, results):
                if isinstance(res, BaseException):
                    logger.error(
                        "[Sitemap] Child sitemap fetch/parse raised for %s: %r",
                        cu,
                        res,
                    )
                    continue
                if not res:
                    logger.warning(
                        "[Sitemap] Child sitemap returned 0 URLs (fetch empty, parse error, "
                        "or all filtered): %s",
                        cu,
                    )
                    continue
                for u, src in res.items():
                    merged.setdefault(u, src)
        elif root.tag == "urlset":
            for loc in root.findall(".//loc"):
                if not loc.text:
                    continue
                loc_text = loc.text.strip()
                if not loc_text.startswith("http"):
                    continue
                if _loc_xml_path_endswith_xml(loc_text) and "sitemap" in loc_text.lower():
                    nested = await resolve_sitemap_recursively(
                        session,
                        loc_text,
                        max_depth,
                        current_depth + 1,
                    )
                    for u, src in nested.items():
                        merged.setdefault(u, src)
                    if not nested:
                        logger.warning(
                            "[Sitemap] urlset <loc> looked like nested sitemap but yielded "
                            "0 URLs: %s",
                            loc_text,
                        )
                    continue
                merged.setdefault(loc_text, sitemap_url)
        return merged
    except Exception as exc:
        logger.debug("resolve_sitemap_recursively failed for %s: %s", sitemap_url, exc)
        return {}


async def _fetch_and_parse_sitemap(
    session: aiohttp.ClientSession,
    url: str,
    depth: int = 0,
    max_depth: int = 3,
    sem: Optional[asyncio.Semaphore] = None,
) -> List[SitemapEntry]:
    """
    يجلب ويحلل sitemap (يتتبع sitemapindex بشكل متكرر حتى max_depth).
    يستخدم Semaphore لتقييد التزامن ومنع انفجار coroutines عند sitemapindex ضخم.
    """
    # FIX: Deep Sitemap & AI Fallback Integrated
    urls = await resolve_sitemap_recursively(
        session=session,
        sitemap_url=url,
        max_depth=max_depth,
        current_depth=depth,
    )
    if not urls:
        return []
    return [
        SitemapEntry(url=u, discovered_from=src)
        for u, src in urls.items()
    ]


async def _sitemaps_from_robots(
    session: aiohttp.ClientSession, base: str
) -> List[str]:
    """يستخرج روابط Sitemap من robots.txt."""
    text = await _fetch_xml(session, f"{base}/robots.txt")
    if not text:
        return []
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("sitemap:"):
            url = stripped.split(":", 1)[1].strip()
            if url.startswith("http"):
                found.append(url)
    return found


def _filter_product_entries(entries: List[SitemapEntry], base: str) -> List[SitemapEntry]:
    """
    سياسة القائمة السوداء: كل <loc> من الـ sitemap يُقبل ما لم يطابق _EXCLUDE_URL_RE
    أو يكن على استضافة CDN.

    إن وُجد ``discovered_from`` (رابط ملف الـ urlset) وفيه ``product`` أو ``item``،
    فتُقبل كل الروابط بنفس القاعدة: استثناءات صريحة فقط (لا أنماط مسار/منصة).
    """
    _ = base
    product_entries: List[SitemapEntry] = []
    for e in entries:
        try:
            p = urlparse(e.url)
        except Exception:
            continue
        host = (p.netloc or "").lower()
        if "cdn." in host:
            continue
        if _EXCLUDE_URL_RE.search(e.url):
            continue
        product_entries.append(e)

    return product_entries


# ══════════════════════════════════════════════════════════════════════════
#  الدوال الرئيسية العامة
# ══════════════════════════════════════════════════════════════════════════
async def resolve_product_urls(
    store_url: str,
    session: aiohttp.ClientSession,
    *,
    max_products: int = 0,
) -> List[str]:
    """
    تُرجع قائمة URLs لصفحات المنتجات الجاهزة للكشط.

    max_products=0 → كل المنتجات بلا سقف.
    """
    entries = await resolve_product_entries(store_url, session, max_products=max_products)
    return [e.url for e in entries]


async def resolve_store_product_urls(
    session: aiohttp.ClientSession,
    store_url: str,
    *,
    max_products: int = 0,
) -> List[SitemapEntry]:
    """
    غلاف توافق رجعي للاسم/التوقيع القديمين اللذين يستخدمهما engines.async_scraper.

    يعيد قائمة SitemapEntry نفسها، مع ترتيب المعاملات المتوقع:
    (session, store_url) بدلاً من (store_url, session).
    """
    return await resolve_product_entries(store_url, session, max_products=max_products)


async def resolve_product_entries(
    store_url: str,
    session: aiohttp.ClientSession,
    *,
    max_products: int = 0,
) -> List[SitemapEntry]:
    """
    مثل resolve_product_urls لكن يُعيد SitemapEntry (url + lastmod) للكشط التزايدي.

    الخوارزمية:
    1. robots.txt → سطور Sitemap
    2. مسارات خاصة بسلة / زد / Shopify
    3. مسارات Sitemap المعيارية
    4. Fallback: Shopify /products.json
    5. Fallback: HTML crawl لصفحة /products
    """
    base = _base_url(store_url)
    all_entries: List[SitemapEntry] = []

    # 1) robots.txt (الأشرع — هذا ما تعلنه المتاجر رسمياً)
    robots_urls = await _sitemaps_from_robots(session, base)
    for surl in robots_urls:
        entries = await _fetch_and_parse_sitemap(session, surl)
        all_entries.extend(entries)

    # 2) مسارات خاصة بالمنصة
    if not all_entries:
        extra_paths = []
        if _is_salla(base):
            extra_paths = _SALLA_EXTRA_PATHS
        elif _is_zid(base):
            extra_paths = _ZID_EXTRA_PATHS

        for path in extra_paths:
            entries = await _fetch_and_parse_sitemap(session, f"{base}{path}")
            if entries:
                all_entries.extend(entries)
                break

    # 3) مسارات معيارية
    if not all_entries:
        for path in _SITEMAP_CANDIDATES:
            entries = await _fetch_and_parse_sitemap(session, f"{base}{path}")
            if entries:
                all_entries.extend(entries)
                break

    # 4) Fallback: Shopify /products.json API
    if not all_entries:
        all_entries.extend(await _fallback_shopify_api(session, base, max_products))

    # 5) Fallback: HTML crawl of /products page
    if not all_entries:
        all_entries.extend(await _fallback_html_product_page(session, base))

    # إزالة التكرار مع الحفاظ على الترتيب
    seen: set = set()
    unique: List[SitemapEntry] = []
    for e in all_entries:
        if e.url not in seen:
            seen.add(e.url)
            unique.append(e)

    # فلترة → منتجات فقط
    product_entries = _filter_product_entries(unique, base)

    if not product_entries and unique:
        logger.info(
            "لا صفحات منتجات بعد الفلترة (%d رابط كلي) — يُرجع الكل", len(unique)
        )
        product_entries = unique

    # تطبيق السقف
    if max_products > 0:
        product_entries = product_entries[:max_products]

    if not unique:
        loop = asyncio.get_running_loop()
        probe_url = f"{base}/sitemap.xml"
        probe = await loop.run_in_executor(
            None,
            lambda u=probe_url: try_all_sync_fallbacks(u, timeout=28),
        )
        blocked = (
            probe is None
            or looks_like_bot_challenge(probe)
            or (
                probe
                and "<html" in probe[:8000].lower()
                and not _looks_like_xml(probe)
            )
        )
        if blocked:
            msg = (
                f"Sitemap discovery for {base} returned 0 URLs; the store likely "
                f"blocks scrapers (Cloudflare 403/429) or returned a challenge page. "
                f"Try SCRAPER_PROXIES or open {probe_url} in a browser to verify."
            )
            logger.error(msg)
            raise SitemapDiscoveryError(msg)

    logger.info(
        "resolve_product_entries %s → %d منتج (من %d رابط كلي)",
        base, len(product_entries), len(unique),
    )
    return product_entries


# ══════════════════════════════════════════════════════════════════════════
#  Fallbacks
# ══════════════════════════════════════════════════════════════════════════
async def _fallback_shopify_api(
    session: aiohttp.ClientSession,
    base: str,
    max_products: int = 0,
) -> List[SitemapEntry]:
    """Shopify /products.json — صفحات متعددة حتى max_products."""
    entries: List[SitemapEntry] = []
    page = 1
    limit = 250
    try:
        while True:
            url = f"{base}/products.json?limit={limit}&page={page}"
            try:
                r = await fetch_with_retry(
                    session, url, max_retries=3, referer=f"{base}/"
                )
                if r is None:
                    break
                try:
                    data = await r.json(content_type=None)
                finally:
                    r.close()
            except Exception:
                break
            products = data.get("products") or []
            if not products:
                break
            for p in products:
                handle = p.get("handle", "")
                if handle:
                    entries.append(SitemapEntry(url=f"{base}/products/{handle}"))
            if len(products) < limit:
                break
            page += 1
            if max_products > 0 and len(entries) >= max_products:
                break
    except Exception:
        pass
    return entries


async def _fallback_html_product_page(
    session: aiohttp.ClientSession,
    base: str,
) -> List[SitemapEntry]:
    """
    يجلب صفحة /products أو الصفحة الرئيسية ويستخرج روابط المنتجات من <a href>.
    مناسب للمتاجر التي تعجز عن تقديم Sitemap.
    """
    entries: List[SitemapEntry] = []
    candidates_pages = ["/products", "/shop", "/store", "/"]
    _product_href_re = re.compile(
        r'href=["\']([^"\']*(?:/p\d{5,}|/products?/[^"\'/?#]{4,}|/item/[^"\'/?#]{4,}))["\']',
        re.I,
    )
    for path in candidates_pages:
        try:
            r = await fetch_with_retry(
                session,
                f"{base}{path}",
                max_retries=3,
                referer=f"{base}/",
            )
            if r is None:
                continue
            try:
                html = await r.text(errors="ignore")
            finally:
                r.close()
        except Exception:
            continue
        found = _product_href_re.findall(html)
        if not found:
            continue
        seen_local: set = set()
        for href in found:
            full = href if href.startswith("http") else f"{base}{href}"
            full = full.split("?")[0].rstrip("/")
            if full not in seen_local:
                seen_local.add(full)
                entries.append(SitemapEntry(url=full))
        if entries:
            logger.info(
                "_fallback_html_product_page %s → %d روابط من %s",
                base, len(entries), path,
            )
            break
    return entries


# ══════════════════════════════════════════════════════════════════════════
#  دالة مزامنة لتحليل رابط → Sitemap (تُستخدم في واجهة app.py)
# ══════════════════════════════════════════════════════════════════════════
def resolve_store_to_sitemap_url(user_input: str) -> Tuple[Optional[str], str]:
    """
    يعيد (رابط Sitemap الجاهز للكشط، رسالة توضيحية).
    إذا فشل يعيد (None, سبب).
    """
    import requests as _req

    raw = (user_input or "").strip()
    if not raw:
        return None, "الرجاء إدخال رابط."

    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    p = urlparse(raw)
    if not p.netloc:
        return None, "تعذر قراءة نطاق الرابط."

    base = f"{p.scheme}://{p.netloc}"

    def _probe(url: str) -> bool:
        try:
            r = _req.get(url, headers=get_xml_headers(), timeout=20, allow_redirects=True)
            if r.status_code != 200:
                return False
            t = r.text.lstrip()[:2000]
            return bool(re.search(r"<(?:urlset|sitemapindex)\b", t, re.I))
        except Exception:
            return False

    # رابط مباشر لـ XML
    if p.path.lower().endswith(".xml"):
        if _probe(raw):
            return raw, f"تم اعتماد Sitemap مباشرة: `{raw}`"

    # robots.txt
    try:
        r = _req.get(
            f"{base}/robots.txt",
            headers=get_xml_headers(),
            timeout=15,
            allow_redirects=True,
        )
        if r.status_code == 200:
            for line in r.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    u = line.split(":", 1)[1].strip()
                    if u.startswith("http") and _probe(u):
                        return u, f"تم الاستنتاج من robots.txt: `{u}`"
    except Exception:
        pass

    # مسارات شائعة
    for path in _SITEMAP_CANDIDATES:
        candidate = f"{base}{path}"
        if _probe(candidate):
            return candidate, f"تم الاستنتاج تلقائياً: `{candidate}`"

    return (
        None,
        "لم يُعثر على Sitemap يعمل (HTTP 200 وXML). "
        "جرّب فتح الرابط في المتصفح أو أضف رابط sitemap يدوياً.",
    )
