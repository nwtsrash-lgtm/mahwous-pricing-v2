"""
engines/selenium_scraper_v30.py — محرك الكشط المتقدم v30
══════════════════════════════════════════════════════════════════════════════
الهدف:
- تحميل صفحات المنتجات عبر Chromium Headless مع JavaScript كامل.
- إعادة استخدام أقوى طبقة استخراج موجودة في المشروع.
- توفير واجهة موحدة لاستخدام المحرك كحلّ أخير عند فشل HTTP/JSON التقليدي.
- دعم تدوير User-Agent وتهيئة Proxy اختيارية.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import random
import re
import shutil
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger("SeleniumScraper_v30")

_DEFAULT_WAIT_SELECTORS = [
    "[itemprop='price']",
    ".price",
    ".product-price",
    "script[type='application/ld+json']",
    "h1",
]

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

_DRIVER_LOCK = threading.Lock()
_DRIVER_PATH: Optional[str] = None


@dataclass
class RenderedPage:
    url: str
    final_url: str
    title: str
    html: str
    status: str
    user_agent: str
    used_proxy: str
    elapsed_sec: float


@dataclass
class ScrapeResult:
    success: bool
    name: str
    price: float
    url: str
    image: str
    brand: str
    sku: str
    description: str
    source: str
    rendered: bool
    used_proxy: str
    user_agent: str
    error: str
    elapsed_sec: float
    raw: Dict[str, Any]

    def to_row(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "price": self.price,
            "url": self.url,
            "image": self.image,
            "brand": self.brand,
            "sku": self.sku,
            "description": self.description,
            "source": self.source,
            "rendered": self.rendered,
            "used_proxy": self.used_proxy,
            "user_agent": self.user_agent,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "success": self.success,
            "error": self.error,
        }


def _pick_user_agent() -> str:
    return random.choice(_USER_AGENTS)


def _find_chromium_binary() -> str:
    for candidate in (os.environ.get("CHROMIUM_BINARY"), shutil.which("chromium"), shutil.which("chromium-browser")):
        if candidate:
            return candidate
    return "chromium"


def _resolve_driver_path() -> Optional[str]:
    global _DRIVER_PATH
    if _DRIVER_PATH:
        return _DRIVER_PATH
    with _DRIVER_LOCK:
        if _DRIVER_PATH:
            return _DRIVER_PATH
        existing = shutil.which("chromedriver")
        if existing:
            _DRIVER_PATH = existing
            return _DRIVER_PATH
        # نترك Selenium Manager يتولى التوافق مع نسخة Chromium الحالية.
        return None


def _build_options(user_agent: str, proxy: str = "") -> Options:
    options = Options()
    options.binary_location = _find_chromium_binary()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1440,2200")
    options.add_argument("--lang=ar-SA")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument(f"--user-agent={user_agent}")
    if proxy:
        options.add_argument(f"--proxy-server={proxy}")
    return options


def _create_driver(user_agent: str, proxy: str = "") -> webdriver.Chrome:
    driver_path = _resolve_driver_path()
    options = _build_options(user_agent=user_agent, proxy=proxy)
    if driver_path:
        service = Service(driver_path)
        driver = webdriver.Chrome(service=service, options=options)
    else:
        driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(35)
    driver.set_script_timeout(25)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'platform', {get: () => 'Linux x86_64'});
                    Object.defineProperty(navigator, 'languages', {get: () => ['ar-SA', 'ar', 'en-US', 'en']});
                """,
            },
        )
    except Exception:
        pass
    return driver


def _wait_for_product_signals(driver: webdriver.Chrome, timeout: int = 20) -> None:
    wait = WebDriverWait(driver, timeout)
    for selector in _DEFAULT_WAIT_SELECTORS:
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
            return
        except TimeoutException:
            continue
    try:
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:
        pass


def render_page(url: str, timeout: int = 25, proxy: str = "", user_agent: str = "") -> RenderedPage:
    ua = user_agent or _pick_user_agent()
    t0 = time.time()
    driver: Optional[webdriver.Chrome] = None
    try:
        driver = _create_driver(user_agent=ua, proxy=proxy)
        driver.get(url)
        _wait_for_product_signals(driver, timeout=min(timeout, 20))
        time.sleep(1.2)
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.45);")
            time.sleep(0.5)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.8)
        except Exception:
            pass
        html = driver.page_source or ""
        return RenderedPage(
            url=url,
            final_url=driver.current_url or url,
            title=driver.title or "",
            html=html,
            status="ok" if html else "empty_html",
            user_agent=ua,
            used_proxy=proxy,
            elapsed_sec=time.time() - t0,
        )
    except Exception as exc:
        return RenderedPage(
            url=url,
            final_url=url,
            title="",
            html="",
            status=f"render_error:{type(exc).__name__}",
            user_agent=ua,
            used_proxy=proxy,
            elapsed_sec=time.time() - t0,
        )
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def _extract_price_from_rendered_html(html: str) -> float:
    if not html:
        return 0.0
    patterns = [
        r'"price"\s*:\s*"?([\d.,]+)"?',
        r'product:price:amount["\']?\s*content=["\']([\d.,]+)',
        r'(?:SAR|ر\.\s?س|ريال)\s*([\d.,]+)',
        r'([\d.,]+)\s*(?:SAR|ر\.\s?س|ريال)',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if not m:
            continue
        raw = m.group(1).replace(",", "")
        try:
            value = float(raw)
            if 0 < value < 1_000_000:
                return value
        except Exception:
            continue
    return 0.0


def _default_ai_price_extractor(html: str, url: str, name: str = "") -> Dict[str, Any]:
    try:
        from engines.ai_engine import ai_fallback_scrape
        data = ai_fallback_scrape(html, url)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("AI fallback unavailable for %s: %s", url, exc)
        return {}


def scrape_product_v30(
    url: str,
    store_url: str = "",
    proxy: str = "",
    user_agent: str = "",
    ai_price_extractor=None,
) -> Dict[str, Any]:
    rendered = render_page(url=url, proxy=proxy, user_agent=user_agent)
    ai_price_extractor = ai_price_extractor or _default_ai_price_extractor
    if not rendered.html:
        return asdict(
            ScrapeResult(
                success=False,
                name="",
                price=0.0,
                url=url,
                image="",
                brand="",
                sku="",
                description="",
                source="v30_render_failed",
                rendered=False,
                used_proxy=rendered.used_proxy,
                user_agent=rendered.user_agent,
                error=rendered.status,
                elapsed_sec=rendered.elapsed_sec,
                raw={},
            )
        )

    extracted: Dict[str, Any] = {}
    source = "v30_rendered_html"
    try:
        from utils.competitor_product_scraper import extract_product_from_html
        extracted = extract_product_from_html(rendered.html, rendered.final_url or url) or {}
    except Exception as exc:
        logger.warning("فشل extractor المتقدم في v30: %s", exc)
        extracted = {}

    name = str(extracted.get("title") or extracted.get("name") or rendered.title or "").strip()
    image = ""
    images = extracted.get("images") or []
    if isinstance(images, list) and images:
        image = str(images[0]).strip()
    price = 0.0
    try:
        price = float(extracted.get("price") or 0)
    except Exception:
        price = 0.0

    if price <= 0:
        p2 = _extract_price_from_rendered_html(rendered.html)
        if p2 > 0:
            price = p2
            source = "v30_rendered_regex"

    if price <= 0 and callable(ai_price_extractor):
        try:
            ai_result = ai_price_extractor(rendered.html, rendered.final_url or url, name)
            if isinstance(ai_result, dict):
                ai_price = float(ai_result.get("price") or 0)
                if ai_price > 0:
                    price = ai_price
                    source = ai_result.get("source") or "v30_ai_price"
                if not name and ai_result.get("name"):
                    name = str(ai_result.get("name")).strip()
        except Exception as exc:
            logger.debug("AI extractor failed for %s: %s", url, exc)

    result = ScrapeResult(
        success=bool(name and price > 0),
        name=name,
        price=price,
        url=rendered.final_url or url,
        image=image,
        brand=str(extracted.get("brand") or "").strip(),
        sku=str(extracted.get("sku") or "").strip(),
        description=str(extracted.get("description") or "").strip()[:12000],
        source=source,
        rendered=True,
        used_proxy=rendered.used_proxy,
        user_agent=rendered.user_agent,
        error="" if (name or price) else "no_name_and_price_after_render",
        elapsed_sec=rendered.elapsed_sec,
        raw=extracted,
    )
    return asdict(result)


def scrape_many_products_v30(
    urls: Iterable[str],
    store_url: str = "",
    max_workers: int = 4,
    proxy_pool: Optional[List[str]] = None,
    ai_price_extractor=None,
) -> List[Dict[str, Any]]:
    url_list = [str(u).strip() for u in urls if str(u).strip()]
    if not url_list:
        return []

    proxy_pool = [p for p in (proxy_pool or []) if str(p).strip()]
    results: List[Dict[str, Any]] = []

    def _run(single_url: str) -> Dict[str, Any]:
        proxy = random.choice(proxy_pool) if proxy_pool else ""
        return scrape_product_v30(
            url=single_url,
            store_url=store_url,
            proxy=proxy,
            ai_price_extractor=ai_price_extractor,
        )

    workers = max(1, min(int(max_workers or 1), 10))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(_run, u): u for u in url_list}
        for future in concurrent.futures.as_completed(future_map):
            url = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({
                    "success": False,
                    "name": "",
                    "price": 0.0,
                    "url": url,
                    "image": "",
                    "brand": "",
                    "sku": "",
                    "description": "",
                    "source": "v30_batch_exception",
                    "rendered": False,
                    "used_proxy": "",
                    "user_agent": "",
                    "error": str(exc)[:300],
                    "elapsed_sec": 0.0,
                    "raw": {},
                })
    return results
