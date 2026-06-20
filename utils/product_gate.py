"""
utils/product_gate.py — بوابة التحقق الإلزامية قبل الإرسال أو التصدير
══════════════════════════════════════════════════════════════════════
كل منتج يجب أن يستوفي شرطين قبل الإرسال إلى Make.com أو التصدير لسلة:
  1) وصف غير فارغ بصيغة "مهووس" يحتوي مكونات حقيقية (لا حشو عام).
  2) رابط صورة حقيقي (http/https وليس placeholder).

السلوك الافتراضي: محاولة التوليد التلقائي للوصف إذا كان مفقوداً
بالاعتماد على المُنسّق الموجود (engines.mahwous_core.format_mahwous_description
أو utils.salla_shamel_export.generate_salla_html_description). إن تعذّر، يُرفض المنتج.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("ProductGate")


def fix_line_spacing(html: str) -> str:
    """
    تنسيق المسافات في HTML — إلزامي قبل أي تصدير أو إرسال.
    يُنظّف الوصف من الفراغات الزائدة ويضمن RTL + line-height صحيح.
    """
    if not html:
        return html or ""
    s = str(html)
    # Remove extra whitespace between HTML tags
    s = re.sub(r">\s+<", "><", s)
    # Normalize multiple spaces to single
    s = re.sub(r"\s{2,}", " ", s)
    # Ensure proper line-height style on first div
    if "line-height" not in s and "<div" in s:
        s = s.replace("<div", '<div style="line-height: 1.5;"', 1)
    # Remove empty paragraphs
    s = re.sub(r"<p[^>]*>\s*</p>", "", s)
    # Ensure RTL direction
    if "dir=" not in s and "<div" in s:
        s = s.replace("<div", '<div dir="rtl"', 1)
    # Clean up any double-style attributes from replacement
    s = re.sub(r'style="([^"]*)" style="', r'style="\1 ', s)
    return s.strip()

# علامات وصف "مهووس" المقبولة (HTML أو نص). يكفي وجود إحداها.
_MAHWOUS_MARKERS = (
    "مهووس",
    "الهرم العطري",
    "المكونات العطرية",
    "لمسة خبير",
    "لماذا تختار",
    # ── علامات إضافية من مولّد Gemini و MAGIC_FACTORY ──
    "رحلة العطر",
    "تفاصيل المنتج",
    "النفحات",
    "مقدمة العطر",
    "قلب العطر",
    "قاعدة العطر",
    "مميزات المنتج",
    "العائلة العطرية",
    "شخصية العطر",
)

# عبارات حشو/قوالب لا تُعد مكوّنات حقيقية
_PLACEHOLDER_DESC_TOKENS = (
    "lorem ipsum",
    "غير متوفر غير متوفر غير متوفر",
    "placeholder",
    "todo",
    "tbd",
)

# نطاقات/قيم رابط صورة غير مقبولة
_BAD_IMAGE_TOKENS = (
    "about:blank",
    "data:",
    "example.com",
    "no-image",
    "no_image",
    "noimage",
    "placeholder",
    "default.png",
    "default.jpg",
    "blank.gif",
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _HTML_TAG_RE.sub(" ", s or "")


def is_real_image_url(url: Any) -> bool:
    """يُعيد True إذا كان رابط الصورة حقيقياً (http/https وليس placeholder)."""
    if not url:
        return False
    s = str(url).strip().lower()
    if not s or s in ("nan", "none", "<na>"):
        return False
    if not (s.startswith("http://") or s.startswith("https://")):
        return False
    return not any(tok in s for tok in _BAD_IMAGE_TOKENS)


def is_mahwous_description(desc: Any, min_text_chars: int = 60) -> bool:
    """
    يتحقق أن الوصف:
      - غير فارغ
      - يحتوي على إحدى علامات صيغة "مهووس"
      - يحتوي نصاً فعلياً (بعد إزالة HTML) ≥ min_text_chars
      - لا يحتوي على عبارات حشو placeholder
    """
    if not desc:
        return False
    s = str(desc).strip()
    if not s:
        return False
    plain = _strip_html(s)
    plain_norm = re.sub(r"\s+", " ", plain).strip()
    if len(plain_norm) < min_text_chars:
        return False
    low = plain_norm.lower()
    if any(tok in low for tok in _PLACEHOLDER_DESC_TOKENS):
        return False
    return any(mk in s for mk in _MAHWOUS_MARKERS)


def _generate_mahwous_description(product: Dict) -> str:
    """يحاول توليد وصف مهووس باستخدام المنسّقات الموجودة."""
    # 1) القالب الأساسي في mahwous_core
    try:
        from engines.mahwous_core import format_mahwous_description  # type: ignore
        notes = product.get("notes") or {}
        if not notes:
            notes = {
                "top":   product.get("top_notes") or product.get("مقدمة_العطر") or product.get("الافتتاحية") or "",
                "heart": product.get("heart_notes") or product.get("قلب_العطر") or product.get("القلب") or "",
                "base":  product.get("base_notes") or product.get("قاعدة_العطر") or product.get("القاعدة") or "",
            }
        data = {
            "name":  product.get("name") or product.get("أسم المنتج") or product.get("المنتج") or "",
            "brand": product.get("brand") or product.get("الماركة") or "",
            "description": product.get("description") or product.get("الوصف") or "",
            "notes": notes,
        }
        if data["name"]:
            out = format_mahwous_description(data)
            if out:
                return out
    except Exception as e:
        logger.debug("mahwous_core تعذّر: %s", e)

    # 2) قالب Salla HTML الشامل
    try:
        from utils.salla_shamel_export import generate_salla_html_description  # type: ignore
        return generate_salla_html_description(
            product_name=str(product.get("name") or product.get("أسم المنتج") or "منتج"),
            brand_name=str(product.get("brand") or product.get("الماركة") or "غير متوفر"),
            top_notes=str(product.get("top_notes") or "غير متوفر"),
            heart_notes=str(product.get("heart_notes") or "غير متوفر"),
            base_notes=str(product.get("base_notes") or "غير متوفر"),
            description_text=str(product.get("description") or product.get("الوصف") or ""),
        )
    except Exception as e:
        logger.debug("generate_salla_html_description تعذّر: %s", e)
    return ""


def _get_desc(product: Dict) -> str:
    return str(product.get("الوصف") or product.get("description") or "").strip()


def _get_image(product: Dict) -> str:
    return str(
        product.get("image_url")
        or product.get("صورة المنتج")
        or product.get("صورة_المنافس")
        or product.get("الصورة")
        or ""
    ).strip()


def validate_and_enrich(
    products: List[Dict],
    *,
    auto_generate_desc: bool = True,
) -> Tuple[List[Dict], List[Dict]]:
    """
    يفحص كل منتج. إن غاب الوصف بصيغة مهووس، يحاول توليده.
    يُعيد (valid, rejected) — كل عنصر مرفوض dict فيه: product, reason.
    """
    valid: List[Dict] = []
    rejected: List[Dict] = []
    for p in products or []:
        if not isinstance(p, dict):
            rejected.append({"product": p, "reason": "صيغة منتج غير صالحة"})
            continue
        name = str(p.get("name") or p.get("أسم المنتج") or p.get("المنتج") or "").strip()
        if not name:
            rejected.append({"product": p, "reason": "اسم المنتج فارغ"})
            continue

        # ── الوصف ─────────────────────────────────────────────────
        desc = _get_desc(p)
        if not is_mahwous_description(desc):
            if auto_generate_desc:
                gen = _generate_mahwous_description(p)
                if gen and is_mahwous_description(gen):
                    p["الوصف"] = gen
                    p["description"] = gen
                    desc = gen
            if not is_mahwous_description(desc):
                logger.warning("⚠️ تخطي «%s» — وصف مهووس مفقود أو غير صالح", name[:50])
                rejected.append({"product": p, "reason": "وصف مهووس مفقود/غير صالح"})
                continue

        # ── رابط الصورة ───────────────────────────────────────────
        img = _get_image(p)
        if not is_real_image_url(img):
            logger.warning("⚠️ تخطي «%s» — رابط صورة غير حقيقي", name[:50])
            rejected.append({"product": p, "reason": "رابط صورة مفقود/غير حقيقي"})
            continue

        # ── fix_line_spacing إلزامي قبل الموافقة ──────────────────
        desc = _get_desc(p)
        p["الوصف"] = fix_line_spacing(desc)
        if "description" in p:
            p["description"] = p["الوصف"]

        valid.append(p)
    return valid, rejected


def validate_dataframe(df, *, auto_generate_desc: bool = True):
    """
    نسخة DataFrame من validate_and_enrich. تعيد (valid_df, rejected_records).
    تتعامل مع أعمدة سلة العربية أو الأعمدة الإنجليزية.
    """
    import pandas as pd
    if df is None or getattr(df, "empty", True):
        return df, []
    records = df.to_dict(orient="records")
    valid, rejected = validate_and_enrich(records, auto_generate_desc=auto_generate_desc)
    return pd.DataFrame(valid) if valid else pd.DataFrame(columns=df.columns), rejected
