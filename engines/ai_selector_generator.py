"""
engines/ai_selector_generator.py — AI-driven CSS selector generation (v1.0)
═══════════════════════════════════════════════════════════════════════════
الفكرة:
  بدل كتابة selectors يدوية لكل متجر، يتلقى الـ AI عينة HTML مرة واحدة
  لكل دومين ويولّد selectors لـ (price, name, image, currency). النتائج
  تُخزَّن في ملف JSON — استدعاءات لاحقة لنفس الدومين مجانية تماماً.

يستخدم _call_gemini الموجود في engines/ai_engine.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger("AISelectorGen")

_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "ai_selectors_cache.json"
_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Dict[str, Any]] = {}
_LOADED = False


def _load_cache() -> None:
    global _LOADED, _CACHE
    if _LOADED:
        return
    with _CACHE_LOCK:
        if _LOADED:
            return
        try:
            if _CACHE_PATH.exists():
                _CACHE = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug("selector cache load failed: %s", e)
            _CACHE = {}
        _LOADED = True


def _save_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps(_CACHE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug("selector cache save failed: %s", e)


def _reduce_html(html: str, max_chars: int = 6000) -> str:
    """Strip script/style/svg and truncate — keeps the skeleton the AI needs."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "svg", "noscript", "iframe"]):
            tag.decompose()
        # Focus on main/product-likely containers
        main = soup.find(attrs={"class": re.compile(r"product|item|main", re.I)}) or soup.body or soup
        text = str(main)[:max_chars]
        return text
    except Exception:
        return html[:max_chars]


def _generate_selectors_via_ai(domain: str, html_sample: str) -> Optional[Dict[str, Any]]:
    """Ask Gemini to produce selectors for this domain."""
    try:
        from engines.ai_engine import _call_gemini
    except ImportError:
        return None

    reduced = _reduce_html(html_sample)
    prompt = (
        "You are a web scraping expert. Analyze the HTML snippet from a "
        f"Saudi e-commerce site (domain={domain}) and return ONLY a JSON "
        "object with CSS selectors for the product page. Required keys:\n"
        '  "price"    : CSS selector for the SAR price element\n'
        '  "name"     : CSS selector for product name/title\n'
        '  "image"    : CSS selector for main product image\n'
        '  "currency" : "SAR" | "USD" | other (currency of price selector)\n'
        "Reply with JSON only, no prose, no markdown fences.\n\n"
        f"HTML:\n{reduced}"
    )
    try:
        resp = _call_gemini(prompt, temperature=0.0, max_tokens=300)
        if not resp:
            return None
        text = str(resp).strip()
        # Strip markdown fences if present
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.M)
        # Grab the first JSON object
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        data = json.loads(match.group(0))
        if isinstance(data, dict) and "price" in data:
            return {
                "price":    str(data.get("price", "")).strip(),
                "name":     str(data.get("name", "")).strip(),
                "image":    str(data.get("image", "")).strip(),
                "currency": str(data.get("currency", "SAR")).strip().upper(),
            }
    except Exception as e:
        logger.debug("AI selector gen error for %s: %s", domain, e)
    return None


def get_selectors(domain: str, html_sample: str = "") -> Optional[Dict[str, Any]]:
    """
    Returns cached selectors for domain, or generates fresh ones via AI if
    html_sample is provided. Returns None when AI is unavailable or failed.
    """
    _load_cache()
    cached = _CACHE.get(domain)
    if cached and cached.get("price"):
        return cached

    if not html_sample:
        return None

    selectors = _generate_selectors_via_ai(domain, html_sample)
    if not selectors:
        return None

    with _CACHE_LOCK:
        _CACHE[domain] = selectors
        _save_cache()
    logger.info("🤖 AI selectors generated for %s: %s", domain, selectors)
    return selectors


def extract_with_ai_selectors(html: str, domain: str) -> Dict[str, Any]:
    """
    Try to extract (price, name, image) using AI-generated selectors.
    On first call per domain, AI is invoked; later calls hit the cache.
    """
    result: Dict[str, Any] = {"price": None, "name": "", "image": "", "currency": ""}
    selectors = get_selectors(domain, html_sample=html)
    if not selectors:
        return result

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return result

    # Skip if the site is primarily USD (we target SAR)
    if selectors.get("currency") and selectors["currency"] not in ("SAR", "", "ر.س", "RIYAL"):
        result["currency"] = selectors["currency"]
        # Still try — operator may want USD later; but don't pollute SAR field
        return result

    price_sel = selectors.get("price", "")
    if price_sel:
        try:
            el = soup.select_one(price_sel)
            if el:
                txt = el.get_text(" ", strip=True)
                nums = re.findall(r"[\d,]+\.?\d*", txt.replace(",", ""))
                if nums:
                    try:
                        p = float(nums[0])
                        if 0 < p < 1_000_000:
                            result["price"] = p
                    except ValueError:
                        pass
        except Exception as e:
            logger.debug("price selector %s failed on %s: %s", price_sel, domain, e)
            # Invalidate cache entry so next call regenerates
            invalidate(domain)

    if selectors.get("name"):
        try:
            el = soup.select_one(selectors["name"])
            if el:
                result["name"] = el.get_text(" ", strip=True)[:250]
        except Exception:
            pass

    if selectors.get("image"):
        try:
            el = soup.select_one(selectors["image"])
            if el:
                result["image"] = el.get("src") or el.get("data-src") or ""
        except Exception:
            pass

    result["currency"] = selectors.get("currency", "")
    return result


def invalidate(domain: str) -> None:
    """Drop cached selectors for a domain (e.g. when site HTML changed)."""
    _load_cache()
    with _CACHE_LOCK:
        if domain in _CACHE:
            _CACHE.pop(domain, None)
            _save_cache()
            logger.info("🗑 selector cache invalidated for %s", domain)
