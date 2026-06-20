"""
استخراج بيانات منتج من صفحة HTML لمتاجر المنافسين.
يستخدم BeautifulSoup + JSON-LD + وسوم Open Graph، مع روابط مطلقة عبر urljoin.
"""
from __future__ import annotations

import html as html_module
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from engines.ai_engine import ai_fallback_scrape  # FIX: Deep Sitemap & AI Fallback Integrated

logger = logging.getLogger(__name__)

# علامات شائعة لصفحات التحدي (Cloudflare / حماية)
_CHALLENGE_SNIPPETS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "enable javascript",
    "ddos protection by",
    "attention required! | cloudflare",
)


def _abs_url(base: str, href: Optional[str]) -> Optional[str]:
    if not href or not str(href).strip():
        return None
    h = str(href).strip()
    if h.startswith("data:") or h.startswith("javascript:") or h.startswith("#"):
        return None
    if h.startswith("//"):
        return "https:" + h
    if h.startswith("http"):
        return h
    try:
        return urljoin(base, h)
    except Exception:
        return None


def _uniq_urls(urls: List[str], limit: int = 30) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for u in urls:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def _parse_price_from_text(text: str) -> Optional[float]:
    if not text:
        return None
    t = re.sub(r"\s+", " ", str(text))
    # 1 299,00 أو 1299.50 أو ٣٩٩
    patterns = [
        r"(?:ر\.?\s*س|SAR|ريال|ر\s*ي\s*ال)\s*[:\s]*([\d٬,\.]+)",
        r"([\d٬,\.]+)\s*(?:ر\.?\s*س|SAR|ريال)",
        r'"price"\s*:\s*"?([\d\.]+)"?',
        r"content=[\"']([\d\.]+)[\"'][^>]*itemprop=[\"']price",
    ]
    for pat in patterns:
        m = re.search(pat, t, re.I)
        if m:
            raw = m.group(1)
            raw = raw.replace("٬", "").replace(",", ".")
            raw = re.sub(r"[^\d.]", "", raw)
            try:
                v = float(raw)
                if 0 < v < 1_000_000:
                    return v
            except ValueError:
                continue
    return None


def _walk_json_ld(node: Any, out: Dict[str, Any]) -> None:
    """يجمع حقول Product من كائن JSON-LD (متداخل @graph أو قائمة)."""
    if node is None:
        return
    if isinstance(node, list):
        for x in node:
            _walk_json_ld(x, out)
        return
    if not isinstance(node, dict):
        return
    t = node.get("@type")
    types = t if isinstance(t, list) else ([t] if t else [])
    types_l = [str(x).lower() for x in types if x]
    if "product" in types_l or any("product" in str(x).lower() for x in types_l):
        if not out.get("name") and node.get("name"):
            out["name"] = str(node["name"]).strip()
        brand = node.get("brand")
        if isinstance(brand, dict) and brand.get("name"):
            out["brand"] = str(brand["name"]).strip()
        elif isinstance(brand, str):
            out["brand"] = brand.strip()
        desc = node.get("description")
        if desc and not out.get("description"):
            out["description"] = str(desc).strip()[:8000]
        sku = node.get("sku") or node.get("mpn")
        if sku and not out.get("sku"):
            out["sku"] = str(sku).strip()
        gtin = node.get("gtin") or node.get("gtin13") or node.get("gtin8")
        if gtin and not out.get("barcode"):
            out["barcode"] = str(gtin).strip()
        offers = node.get("offers")
        if offers:
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                p = offers.get("price") or offers.get("lowPrice")
                if p and not out.get("price"):
                    try:
                        out["price"] = float(str(p).replace(",", ""))
                    except ValueError:
                        pass
        img = node.get("image")
        if img:
            if isinstance(img, str):
                out.setdefault("images", []).append(img)
            elif isinstance(img, list):
                out.setdefault("images", []).extend(str(x) for x in img if x)
            elif isinstance(img, dict) and img.get("url"):
                out.setdefault("images", []).append(str(img["url"]))
    g = node.get("@graph")
    if g:
        _walk_json_ld(g, out)
    for k, v in node.items():
        if k != "@graph" and isinstance(v, (list, dict)):
            _walk_json_ld(v, out)


def extract_meta_bundle(html: str, base_url: str) -> Dict[str, Any]:
    """يستخرج حقولاً من وسوم meta و JSON-LD (يعمل بدون BeautifulSoup)."""
    out: Dict[str, Any] = {}
    if not html:
        return out

    def _meta(prop: str, attr: str = "property") -> Optional[str]:
        pat = rf'<meta[^>]+{attr}=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)["\']'
        m = re.search(pat, html, re.I)
        if m:
            return html_module.unescape(m.group(1).strip())
        pat2 = rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+{attr}=["\']{re.escape(prop)}["\']'
        m2 = re.search(pat2, html, re.I)
        return html_module.unescape(m2.group(1).strip()) if m2 else None

    og_title = _meta("og:title") or _meta("twitter:title", "name")
    og_desc = _meta("og:description") or _meta("description", "name")
    og_img = _meta("og:image") or _meta("twitter:image", "name")
    price = _meta("product:price:amount")
    brand_m = _meta("product:brand") or _meta("brand", "name")
    sku_m = _meta("product:retailer_item_id")

    if og_title:
        out["title"] = og_title
    if og_desc:
        out["description"] = og_desc
    if og_img:
        u = _abs_url(base_url, og_img)
        if u:
            out["images"] = [u]
    if price:
        try:
            out["price"] = float(str(price).replace(",", ""))
        except ValueError:
            pass
    if brand_m:
        out["brand"] = brand_m.strip()
    if sku_m:
        out["sku"] = sku_m.strip()

    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
        html,
        re.I,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
            _walk_json_ld(data, out)
        except json.JSONDecodeError:
            continue

    return out


def looks_like_bot_challenge(html: str) -> bool:
    if not html or len(html) < 400:
        return True
    head = html[:12000].lower()
    return any(s in head for s in _CHALLENGE_SNIPPETS)


def extract_product_from_html(html: str, page_url: str) -> Dict[str, Any]:
    """
    استخراج شامل: BeautifulSoup إن وُجد، مع دمج JSON-LD و meta.
    يُعيد: title, price, description, images[], brand, sku, barcode, raw_meta_summary
    """
    base = page_url
    merged: Dict[str, Any] = extract_meta_bundle(html, base)

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except ImportError:
        logger.warning("BeautifulSoup غير مثبت — الاعتماد على meta/regex فقط")
        _finalize_merged(merged, page_url)
        return merged

    # عنوان
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        t = h1.get_text(" ", strip=True)
        if t and (not merged.get("title") or len(t) > len(str(merged.get("title", "")))):
            merged["title"] = t

    # وصف طويل
    for sel in (
        "[itemprop=description]",
        ".product-description",
        ".product-details",
        "#product-description",
        ".description",
        "div.entry-content",
    ):
        el = soup.select_one(sel)
        if el:
            txt = el.get_text("\n", strip=True)
            if len(txt) > 80:
                merged["description"] = txt[:12000]
                break

    # سعر من عناصر شائعة
    if not merged.get("price"):
        for sel in ('[itemprop="price"]', ".price", ".product-price", "[data-price]"):
            el = soup.select_one(sel)
            if not el:
                continue
            val = el.get("content") or el.get("data-price") or el.get_text(" ", strip=True)
            p = _parse_price_from_text(str(val))
            if p:
                merged["price"] = p
                break

    # صور المعرض
    imgs: List[str] = []
    for im in soup.find_all("img"):
        src = im.get("src") or im.get("data-src") or im.get("data-lazy-src")
        u = _abs_url(base, src)
        if not u:
            continue
        low = u.lower()
        if any(x in low for x in (".svg", "pixel", "spacer", "blank", "1x1", "logo", "icon")):
            continue
        if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")) or "cdn" in low or "image" in low:
            imgs.append(u)
    if merged.get("images"):
        imgs = list(merged["images"]) + imgs
    merged["images"] = _uniq_urls(imgs, 25)

    # ماركة / SKU من microdata
    b_el = soup.find(attrs={"itemprop": "brand"})
    if b_el and not merged.get("brand"):
        name_el = b_el.find(attrs={"itemprop": "name"}) if hasattr(b_el, "find") else None
        merged["brand"] = (
            name_el.get_text(strip=True) if name_el else b_el.get_text(strip=True)
        )
    sku_el = soup.find(attrs={"itemprop": "sku"})
    if sku_el and sku_el.get_text(strip=True) and not merged.get("sku"):
        merged["sku"] = sku_el.get_text(strip=True)

    _finalize_merged(merged, page_url)

    # FIX: Deep Sitemap & AI Fallback Integrated
    # طبقة إنقاذ AI: تعمل فقط إذا فشل الاستخراج التقليدي في الاسم/السعر.
    needs_ai_fallback = (
        not str(merged.get("title") or "").strip()
        or merged.get("price") in (None, 0, 0.0, "0")
    )
    if needs_ai_fallback and html:
        ai_data = ai_fallback_scrape(html, page_url)
        if isinstance(ai_data, dict) and not ai_data.get("error"):
            ai_name = str(ai_data.get("name") or "").strip()
            if ai_name:
                merged["title"] = ai_name
            ai_price = ai_data.get("price")
            try:
                ai_price_float = float(ai_price)
            except (TypeError, ValueError):
                ai_price_float = 0.0
            if ai_price_float > 0:
                merged["price"] = ai_price_float
            if "is_available" in ai_data:
                merged["is_available"] = bool(ai_data.get("is_available"))
            ai_description = str(ai_data.get("description") or "").strip()
            if ai_description and not str(merged.get("description") or "").strip():
                merged["description"] = ai_description
            ai_fragrance_notes = str(ai_data.get("fragrance_notes") or "").strip()
            if ai_fragrance_notes:
                merged["fragrance_notes"] = ai_fragrance_notes  # FIX: Zero-Gap HTML & AI Fragrance Notes

    return merged


def _finalize_merged(merged: Dict[str, Any], page_url: str) -> None:
    if not merged.get("title") and merged.get("name"):
        merged["title"] = str(merged["name"]).strip()
    abs_list: List[str] = []
    for u in list(merged.get("images") or []):
        au = _abs_url(page_url, u)
        if au:
            abs_list.append(au)
    merged["images"] = _uniq_urls(abs_list, 25)
    if merged.get("price") is None:
        blob = " ".join(
            str(merged.get(k) or "")
            for k in ("title", "description")
        )
        p = _parse_price_from_text(blob)
        if p:
            merged["price"] = p
    merged["url"] = page_url
    merged["domain"] = urlparse(page_url).netloc
    # ملخص نصي للـ AI عند الاعتماد على meta فقط
    parts = [
        f"العنوان: {merged.get('title', '')}",
        f"الماركة: {merged.get('brand', '')}",
        f"السعر: {merged.get('price', '')}",
        f"SKU: {merged.get('sku', '')}",
        f"الباركود: {merged.get('barcode', '')}",
        f"الوصف (مختصر): {str(merged.get('description', ''))[:1500]}",
        f"عدد الصور: {len(merged.get('images') or [])}",
    ]
    merged["raw_meta_summary"] = "\n".join(parts)


def fetch_product_page_html(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    يجلب HTML عبر سلسلة anti-ban في scrapers.anti_ban.
    يُعيد (html, رسالة_خطأ). عند التحدي قد يُعاد HTML جزئي مع رسالة تحذير.
    """
    url = (url or "").strip()
    if not url.startswith("http"):
        return None, "الرابط يجب أن يبدأ بـ http أو https."

    try:
        from scrapers.anti_ban import try_all_sync_fallbacks
    except ImportError:
        return None, "تعذر استيراد scrapers.anti_ban — شغّل التطبيق من جذر المشروع."

    try:
        html_text = try_all_sync_fallbacks(url)
    except Exception as exc:
        logger.exception("fetch_product_page_html")
        return None, f"فشل جلب الصفحة: {exc}"

    if not html_text:
        return None, (
            "تعذر جلب المحتوى (حظر Cloudflare أو حماية قوية). "
            "جرّب رابطاً آخر أو انسخ عنوان الصفحة بعد فتحها في المتصفح."
        )

    if looks_like_bot_challenge(html_text):
        return html_text, "cloudflare_or_challenge"

    return html_text, None
