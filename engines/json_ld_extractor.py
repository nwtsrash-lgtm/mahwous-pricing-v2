"""
engines/json_ld_extractor.py — High-precision structured-data extractor (v1.0)
═══════════════════════════════════════════════════════════════════════════════
يستخرج اسم المنتج + السعر + الصورة من:
  1. <script type="application/ld+json">  (Schema.org Product)
  2. OG/Twitter meta tags  (product:price:amount, og:image, og:title …)

هذا المصدر **أكثر دقة** من selectors و regex لأن المتاجر تنشره لـ Google/Facebook.
يعمل تلقائياً مع كل متاجر Salla و Shopify و Magento.

تفضيل العملة: SAR. تفضيل السعر: sale_price > price.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger("JsonLdExtractor")

_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_META_RE = re.compile(
    r'<meta\b[^>]*?(?:property|name)=["\']([^"\']+)["\'][^>]*?content=["\']([^"\']*)["\']',
    re.I,
)
_META_RE_REV = re.compile(
    r'<meta\b[^>]*?content=["\']([^"\']*)["\'][^>]*?(?:property|name)=["\']([^"\']+)["\']',
    re.I,
)

_NON_SAR = {"USD", "EUR", "GBP", "AED", "KWD", "BHD", "QAR", "OMR"}


def _walk(node: Any):
    """Yield every dict inside a possibly-nested JSON-LD structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _first_offer(product: Dict[str, Any]) -> Dict[str, Any]:
    offers = product.get("offers")
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if isinstance(offers, dict):
        return offers
    return {}


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        s = str(value).strip().replace(",", "")
        nums = re.findall(r"\d+\.?\d*", s)
        if not nums:
            return None
        p = float(nums[0])
        return p if 0 < p < 1_000_000 else None
    except Exception:
        return None


def _parse_meta(html: str) -> Dict[str, str]:
    """Parse OG/Twitter/product:* meta tags into a dict."""
    out: Dict[str, str] = {}
    head = html[:80_000]  # meta tags are always near the top
    for m in _META_RE.finditer(head):
        out.setdefault(m.group(1).lower(), m.group(2))
    for m in _META_RE_REV.finditer(head):
        out.setdefault(m.group(2).lower(), m.group(1))
    return out


def _extract_from_jsonld(html: str) -> Dict[str, Any]:
    """Walk every JSON-LD block, return the first Product found."""
    result: Dict[str, Any] = {}
    for raw in _LD_RE.findall(html):
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        for node in _walk(data):
            t = str(node.get("@type", ""))
            if "Product" not in t:
                continue
            offer = _first_offer(node)
            cur = (offer.get("priceCurrency") or "").upper()
            price = _to_float(offer.get("price")) or _to_float(offer.get("lowPrice"))
            if price is None:
                continue
            # Skip non-SAR offers; allow blank currency (Salla often omits)
            if cur and cur in _NON_SAR:
                continue
            img = node.get("image")
            if isinstance(img, list):
                img = img[0] if img else ""
            if isinstance(img, dict):
                img = img.get("url", "")
            # SKU: حقول Schema.org الشائعة بالترتيب
            sku = (
                node.get("sku") or node.get("mpn")
                or node.get("productID") or node.get("gtin13")
                or node.get("gtin") or ""
            )
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name", "")
            return {
                "price":    price,
                "name":     str(node.get("name", "")).strip()[:250],
                "image":    str(img or "").strip(),
                "description": str(node.get("description", "") or "").strip()[:1200],
                "sku":      str(sku or "").strip()[:120],
                "brand":    str(brand or "").strip()[:80],
                "currency": cur or "SAR",
                "source":   "json-ld",
            }
    return result


def _extract_from_meta(html: str) -> Dict[str, Any]:
    """Fallback: OG / product meta tags. Prefers sale_price over price."""
    meta = _parse_meta(html)
    cur = (
        meta.get("product:sale_price:currency")
        or meta.get("product:price:currency")
        or meta.get("og:price:currency")
        or ""
    ).upper()
    if cur and cur in _NON_SAR:
        return {}
    price = (
        _to_float(meta.get("product:sale_price:amount"))
        or _to_float(meta.get("product:price:amount"))
        or _to_float(meta.get("og:price:amount"))
    )
    if price is None:
        return {}
    return {
        "price":    price,
        "name":     (meta.get("og:title") or meta.get("twitter:title") or "").strip()[:250],
        "image":    (meta.get("og:image") or meta.get("twitter:image") or "").strip(),
        "description": (
            meta.get("og:description") or meta.get("twitter:description")
            or meta.get("description") or ""
        ).strip()[:1200],
        "sku":      (
            meta.get("product:retailer_item_id")
            or meta.get("product:sku") or meta.get("og:sku") or ""
        ).strip()[:120],
        "brand":    (meta.get("product:brand") or meta.get("og:brand") or "").strip()[:80],
        "currency": cur or "SAR",
        "source":   "og-meta",
    }


def extract(html: str) -> Dict[str, Any]:
    """
    Returns dict with: price, name, image, currency, source.
    Empty dict {} when no structured data found.
    Try JSON-LD first (richest), then OG/meta.
    """
    if not html or len(html) < 50:
        return {}
    data = _extract_from_jsonld(html)
    if data.get("price"):
        return data
    return _extract_from_meta(html)
