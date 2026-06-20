"""
utils/make_helper.py v25.0 — إرسال صحيح لـ Make.com + عمود NO
══════════════════════════════════════════════════════════════
v25.0 additions:
  + حقل NO (رقم المنتج من كتالوج سلة/زد — Primary Key "No.") يُمرَّر صراحةً
    في كل payload. Make.com يستخدمه لتحديث المنتج الصحيح بدقة مطلقة.
  + دالة _extract_no() تقرأ "No." / "NO" / "رقم المنتج" من dict أو Series.
  + في حال غياب product_id الصريح، نستخدم NO كبديل أساسي.

سيناريو تحديث الأسعار (Integration Webhooks, Salla):
  Webhook → BasicFeeder يقرأ {{2.products}} → UpdateProduct
  Payload المطلوب: {"products": [{"NO":"...","product_id":"...","name":"...","price":...}]}

سيناريو المنتجات الجديدة:
  Webhook → BasicFeeder يقرأ {{1.data}} → CreateProduct
  Payload المطلوب: {"data": [{"NO":"...","أسم المنتج":"...","سعر المنتج":...,"الوصف":"..."}]}
"""

import requests
import json
import logging
import os
import time
from typing import List, Dict, Any, Optional

logger = logging.getLogger("MakeHelper")


# ── Webhook URLs ───────────────────────────────────────────────────────────
def _get_webhook_url(key: str, default: str) -> str:
    return os.environ.get(key, "") or default

WEBHOOK_UPDATE_PRICES = _get_webhook_url(
    "WEBHOOK_UPDATE_PRICES",
    "https://hook.eu2.make.com/YOUR_WEBHOOK_URL_HERE"
)
WEBHOOK_NEW_PRODUCTS = _get_webhook_url(
    "WEBHOOK_NEW_PRODUCTS",
    "https://hook.eu2.make.com/YOUR_WEBHOOK_URL_HERE"
)

TIMEOUT = 15  # ثانية


# ── الإرسال الأساسي ────────────────────────────────────────────────────────
def _post_to_webhook(url: str, payload: Any) -> Dict:
    if not url:
        return {"success": False, "message": "❌ Webhook URL غير محدد", "status_code": 0}
    try:
        headers = {"Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        if resp.status_code in (200, 201, 202, 204):
            return {
                "success": True,
                "message": f"✅ تم الإرسال بنجاح ({resp.status_code})",
                "status_code": resp.status_code,
            }
        return {
            "success": False,
            "message": f"❌ HTTP {resp.status_code}: {resp.text[:200]}",
            "status_code": resp.status_code,
        }
    except requests.exceptions.Timeout:
        return {"success": False, "message": "❌ انتهت مهلة الاتصال (Timeout)", "status_code": 0}
    except requests.exceptions.ConnectionError:
        return {"success": False, "message": "❌ فشل الاتصال بـ Make — تحقق من الإنترنت", "status_code": 0}
    except Exception as e:
        return {"success": False, "message": f"❌ خطأ غير متوقع: {str(e)}", "status_code": 0}


# ── تحويل float آمن ───────────────────────────────────────────────────────
def _safe_float(val, default: float = 0.0) -> float:
    try:
        if val is None or str(val).strip() in ("", "nan", "None", "NaN"):
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


# ── تنظيف product_id ──────────────────────────────────────────────────────
def _clean_pid(raw) -> str:
    """product_id دائماً كـ str(int(float(value))). مثال: '100.0' → '100'."""
    if raw is None: return ""
    s = str(raw).strip()
    if s in ("", "nan", "None", "NaN", "0", "0.0"): return ""
    try:
        return str(int(float(s)))
    except (ValueError, TypeError):
        return s


def _pid_as_int(raw) -> Optional[int]:
    """product_id كرقم صحيح لموديول Salla UpdateProduct (select field)."""
    s = _clean_pid(raw)
    if not s:
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


# ── استخراج رقم المنتج No. من الكتالوج (Primary Key في سلة/زد) ───────────
def _extract_no(row_or_dict) -> str:
    """
    يستخرج قيمة عمود "No." (رقم المنتج في كتالوجنا) من صف DataFrame أو dict.
    هذا هو المعرّف الرسمي الذي يستخدمه Make.com لتحديث المنتج في سلة/زد.
    يتحقق من عدة أسماء محتملة ويُنظّف القيمة نهائياً عبر _clean_pid().
    """
    if row_or_dict is None:
        return ""
    getter = row_or_dict.get if hasattr(row_or_dict, "get") else lambda k, d=None: d
    raw = (
        getter("No.")          or getter("NO")             or
        getter("no")           or getter("No")             or
        getter("رقم_المنتج")   or getter("رقم المنتج")    or
        getter("catalog_no")   or getter("product_no")     or ""
    )
    return _clean_pid(raw)


# ── ربط اسم التصنيف → category_id (جدول الربط في missing_queue_manager) ────
def _resolve_category_id(p: Dict) -> Optional[int]:
    """يحوّل اسم التصنيف → category_id رقمي عبر جدول الربط الموجود.
    يقبل category_id رقمياً جاهزاً أولاً؛ وإلا يبحث باسم التصنيف (عدة مفاتيح محتملة).
    يعيد None إن تعذّر (فيُحذف لاحقاً من الـ payload — لا يُرسَل صفر/فارغ)."""
    raw_id = _safe_float(p.get("category_id", 0))
    if raw_id:
        return int(raw_id)
    name = str(
        p.get("category_name") or p.get("التصنيف") or
        p.get("تصنيف_المنتج")   or p.get("category") or ""
    ).strip()
    if not name:
        return None
    try:
        from utils.missing_queue_manager import load_category_catalog
        cid = _safe_float(load_category_catalog().get(name, 0))
        return int(cid) if cid else None
    except Exception as e:
        logger.warning("تعذّر ربط التصنيف «%s» بـ category_id: %s", name[:40], e)
        return None


# ── ربط اسم الماركة → brand_id (جدول الربط في missing_queue_manager) ───────
def _resolve_brand_id(p: Dict) -> Optional[int]:
    """يحوّل اسم الماركة → brand_id رقمي عبر load_brand_id_map.
    يقبل brand_id رقمياً جاهزاً أولاً؛ وإلا يطبّع اسم الماركة بنفس مفتاح
    missing_queue_manager (_brand_key) ثم يبحث. يعيد None إن تعذّر (يُحذف من الـ payload)."""
    raw_id = _safe_float(p.get("brand_id", 0))
    if raw_id:
        return int(raw_id)
    name = str(p.get("brand") or p.get("الماركة") or p.get("brand_name") or "").strip()
    if not name:
        return None
    try:
        from utils.missing_queue_manager import load_brand_id_map, _brand_key
        bid = _safe_float(load_brand_id_map().get(_brand_key(name), 0))
        return int(bid) if bid else None
    except Exception as e:
        logger.warning("تعذّر ربط الماركة «%s» بـ brand_id: %s", name[:40], e)
        return None


# ══════════════════════════════════════════════════════════════════════════
#  تحويل DataFrame → قائمة منتجات مع حساب السعر الصحيح لكل قسم
# ══════════════════════════════════════════════════════════════════════════
def export_to_make_format(df, section_type: str = "update") -> List[Dict]:
    """
    تحويل DataFrame إلى قائمة منتجات جاهزة لـ Make.
    section_type: raise | lower | approved | update | missing | new
    كل منتج يحتوي على: NO, product_id, name, price, section, + حقول سياقية
    """
    if df is None or (hasattr(df, "empty") and df.empty):
        return []

    products = []
    for _, row in df.iterrows():

        # ── رقم المنتج (NO = Primary Key في سلة/زد) ───────────────────────
        product_no = _extract_no(row)
        product_id = product_no or _clean_pid(
            row.get("معرف_المنتج")  or row.get("product_id")     or
            row.get("معرف المنتج")  or row.get("sku")            or
            row.get("SKU")          or ""
        )

        # ── اسم المنتج ────────────────────────────────────────────────────
        name = (
            str(row.get("المنتج",         "")) or
            str(row.get("منتج_المنافس",   "")) or
            str(row.get("أسم المنتج",     "")) or
            str(row.get("اسم المنتج",     "")) or
            str(row.get("name",           "")) or ""
        ).strip()
        if name in ("", "nan", "None"): name = ""

        # ── السعر حسب القسم ───────────────────────────────────────────────
        comp_price = _safe_float(row.get("سعر_المنافس", 0))
        our_price  = _safe_float(
            row.get("السعر", 0) or row.get("سعر المنتج", 0) or
            row.get("price",  0) or 0
        )

        if section_type == "raise":
            # سعرنا أقل من المنافس → نرفع سعرنا ليكون أقل بـ 1 ريال من المنافس
            price = round(comp_price - 1, 2) if comp_price > 0 else our_price
        elif section_type == "lower":
            # سعرنا أعلى من المنافس → نخفض سعرنا ليكون أقل بـ 1 ريال من المنافس
            price = round(comp_price - 1, 2) if comp_price > 0 else our_price
        elif section_type in ("approved", "update"):
            price = our_price
        else:
            price = comp_price if comp_price > 0 else our_price

        if not name: continue

        comp_name  = str(row.get("منتج_المنافس", ""))
        comp_src   = str(row.get("المنافس", ""))
        diff       = _safe_float(row.get("الفرق", 0))
        match_pct  = _safe_float(row.get("نسبة_التطابق", 0))
        decision   = str(row.get("القرار", ""))
        brand      = str(row.get("الماركة", ""))

        product = {
            "NO":         product_no,          # ← Primary Key في سلة/زد
            "product_id": product_id,
            "name":       name,
            "price":      float(price),
            "section":    section_type,
        }

        if comp_name and comp_name not in ("nan", "None", "—"):
            product["comp_name"] = comp_name
        if comp_src and comp_src not in ("nan", "None"):
            product["competitor"] = comp_src
        if diff:
            product["price_diff"] = diff
        if match_pct:
            product["match_score"] = match_pct
        if decision and decision not in ("nan", "None"):
            product["decision"] = decision
        if brand and brand not in ("nan", "None"):
            product["brand"] = brand

        products.append(product)

    return products


# ══════════════════════════════════════════════════════════════════════════
#  إرسال منتج واحد — تحديث السعر
#  Payload: {"products": [{"NO":"...","product_id":"...","name":"...","price":...}]}
# ══════════════════════════════════════════════════════════════════════════
def send_single_product(product: Dict) -> Dict:
    """
    إرسال منتج واحد لتحديث سعره في سلة عبر Make.
    Make يقرأ: {{2.products}} → NO | product_id | name | price
    Payload: {"products": [{...}]}
    """
    if not product:
        return {"success": False, "message": "❌ لا توجد بيانات للإرسال"}

    name       = str(product.get("name", "")).strip()
    price      = _safe_float(product.get("price", 0))
    product_no = _extract_no(product) or _clean_pid(product.get("NO", ""))
    product_id = product_no or _clean_pid(product.get("product_id", ""))

    if not name:
        return {"success": False, "message": "❌ اسم المنتج مطلوب"}
    if price <= 0:
        return {"success": False, "message": f"❌ السعر غير صحيح: {price}"}

    pid_int = _pid_as_int(product_no or product_id)
    if pid_int is None:
        logger.warning("⚠️ NO/product_id غير رقمي للمنتج «%s» — سيُرسل كنص", name[:50])
        pid_int = product_no or product_id or ""
    _prod = {
        "NO":          product_no or product_id,     # ← Primary Key Make (fallback صلب)
        "product_id":  pid_int,                       # ← integer لموديول Salla
        "name":        name,
        "price":       float(price),
        "section":     product.get("section", "update"),
        "comp_name":   product.get("comp_name", ""),
        "competitor":  product.get("competitor", ""),
        "price_diff":  product.get("price_diff", product.get("diff", 0)),
        "match_score": product.get("match_score", 0),
        "decision":    product.get("decision", ""),
        "brand":       product.get("brand", ""),
    }
    _cu = str(product.get("comp_url", product.get("رابط_المنافس", "")) or "").strip()
    if _cu:
        _prod["comp_url"] = _cu

    payload = {"products": [_prod]}

    result = _post_to_webhook(WEBHOOK_UPDATE_PRICES, payload)
    if result["success"]:
        id_info = f" [NO: {product_no}]" if product_no else (f" [ID: {product_id}]" if product_id else "")
        result["message"] = f"✅ تم تحديث «{name}»{id_info} ← {price:,.0f} ر.س"
    return result


def trigger_price_update(
    sku: str,
    target_price: float,
    comp_url: str = "",
    *,
    name: str = "",
    comp_name: str = "",
    comp_price: float = 0.0,
    diff: float = 0.0,
    decision: str = "",
    competitor: str = "",
    no: str = "",
) -> bool:
    """
    غلاف تفاعلي لإرسال تحديث سعر واحد إلى Make.com.
    يعيد True عند نجاح HTTP. الحقل `no` هو رقم المنتج في كتالوج سلة/زد.
    """
    res = send_single_product({
        "NO":         no or sku,
        "product_id": sku,
        "name": name,
        "price": float(target_price),
        "comp_name": comp_name,
        "comp_price": comp_price,
        "diff": diff,
        "decision": decision,
        "competitor": competitor,
        "comp_url": comp_url or "",
    })
    return bool(res.get("success"))


# ══════════════════════════════════════════════════════════════════════════
#  إرسال عدة منتجات — تحديث الأسعار
#  Payload: {"products": [{NO, product_id, name, price, ...}]}
# ══════════════════════════════════════════════════════════════════════════
def send_price_updates(products: List[Dict]) -> Dict:
    """
    إرسال قائمة منتجات لتحديث أسعارها في سلة عبر Make.
    كل عنصر يحتوي على `NO` (رقم منتج سلة/زد) لضمان التحديث الدقيق.
    """
    if not products:
        return {"success": False, "message": "❌ لا توجد منتجات للإرسال"}

    valid_products = []
    skipped = 0

    for p in products:
        name       = str(p.get("name", "")).strip()
        price      = _safe_float(p.get("price", 0))
        product_no = _extract_no(p) or _clean_pid(p.get("NO", ""))
        product_id = product_no or _clean_pid(p.get("product_id", ""))

        if not name or price <= 0:
            skipped += 1
            continue

        pid_int = _pid_as_int(product_no or product_id)
        if pid_int is None:
            logger.warning("⚠️ تخطي «%s» — product_id غير رقمي", name[:50])
            skipped += 1
            continue

        if not product_no:
            logger.warning("⚠️ NO فارغ في الدفعة عند «%s»", name[:50])
        valid_products.append({
            "NO":          product_no or product_id,      # ← Primary Key Make (fallback صلب)
            "product_id":  pid_int,                        # ← integer لموديول Salla
            "name":        name,
            "price":       float(price),
            "section":     p.get("section", "update"),
            "comp_name":   p.get("comp_name", ""),
            "competitor":  p.get("competitor", ""),
            "price_diff":  p.get("price_diff", p.get("diff", 0)),
            "match_score": p.get("match_score", 0),
            "decision":    p.get("decision", ""),
            "brand":       p.get("brand", ""),
        })

    if not valid_products:
        return {
            "success": False,
            "message": f"❌ لا توجد منتجات صالحة (تم تخطي {skipped} منتج)"
        }

    payload = {"products": valid_products}
    _no_count = sum(1 for p in valid_products if p.get("NO"))
    logger.info("📤 إرسال %d منتج إلى Make — مع NO: %d/%d",
                len(valid_products), _no_count, len(valid_products))
    result = _post_to_webhook(WEBHOOK_UPDATE_PRICES, payload)

    if result["success"]:
        skip_msg = f" (تم تخطي {skipped})" if skipped else ""
        with_no = sum(1 for p in valid_products if p.get("NO"))
        no_msg = f" | مع NO: {with_no}/{len(valid_products)}"
        result["message"] = f"✅ تم إرسال {len(valid_products)} منتج لتحديث الأسعار{no_msg}{skip_msg}"
    return result


# ══════════════════════════════════════════════════════════════════════════
#  إرسال منتجات جديدة — Webhook منفصل
#  Payload: {"data": [{NO, أسم المنتج, سعر المنتج, ...}]}
# ══════════════════════════════════════════════════════════════════════════
def send_new_products(products: List[Dict]) -> Dict:
    if not products:
        return {"success": False, "message": "❌ لا توجد منتجات للإرسال"}

    # ── بوابة إلزامية: وصف مهووس + رابط صورة حقيقي ────────────────────
    try:
        from utils.product_gate import validate_and_enrich
        products, _gate_rejected = validate_and_enrich(products, auto_generate_desc=True)
        gate_skipped = len(_gate_rejected)
        if gate_skipped:
            logger.warning("🚫 بوابة الجودة استبعدت %d منتج (وصف/صورة مفقود)", gate_skipped)
    except Exception as _e:
        logger.error("فشل تطبيق بوابة الجودة: %s", _e)
        gate_skipped = 0
    if not products:
        return {"success": False,
                "message": f"❌ لا توجد منتجات صالحة — تم رفض {gate_skipped} لغياب وصف مهووس أو صورة حقيقية"}

    sent, skipped, errors = 0, gate_skipped, []

    for p in products:
        name  = str(p.get("name", p.get("أسم المنتج", ""))).strip()
        price = _safe_float(
            p.get("price", 0) or p.get("سعر المنتج", 0) or p.get("السعر", 0)
        )
        product_no = _extract_no(p) or _clean_pid(p.get("NO", ""))
        pid = product_no or _clean_pid(p.get("product_id", p.get("معرف_المنتج", "")))

        if not name:
            skipped += 1
            continue

        item = {
            "NO":              product_no,                # ← Primary Key Make
            "product_id":      pid,
            "أسم المنتج":      name,
            "سعر المنتج":      float(price),
            "رمز المنتج sku":  str(p.get("sku", p.get("رمز المنتج sku", ""))).strip(),
            "الوزن":           int(_safe_float(p.get("weight", p.get("الوزن", 1))) or 1),
            "سعر التكلفة":     float(_safe_float(p.get("cost_price", p.get("سعر التكلفة", 0)))),
            "السعر المخفض":    float(_safe_float(p.get("sale_price",  p.get("السعر المخفض", 0)))),
            "الوصف":           str(p.get("الوصف", p.get("description", ""))).strip(),
        }
        if p.get("image_url"):
            item["صورة المنتج"] = str(p["image_url"])

        result = _post_to_webhook(WEBHOOK_NEW_PRODUCTS, {"data": [item]})
        if result["success"]:
            sent += 1
        else:
            errors.append(name)

        if len(products) > 1:
            time.sleep(0.3)

    failed = len(errors)
    if sent == 0:
        return {
            "success": False,
            "message": f"❌ فشل إرسال جميع المنتجات. تم تخطي {skipped}",
            "sent": sent,
            "failed": failed,
            "total": len(products),
            "status_code": 0,
        }

    skip_msg = f" (تم تخطي {skipped})" if skipped else ""
    err_msg  = f" (فشل {len(errors)})" if errors else ""
    return {
        "success": sent > 0,
        "message": f"✅ تم إرسال {sent} منتج جديد إلى Make{skip_msg}{err_msg}",
        "sent": sent,
        "failed": failed,
        "total": len(products),
        "status_code": 200 if failed == 0 else 207,
    }


# ══════════════════════════════════════════════════════════════════════════
#  إرسال المنتجات المفقودة — نفس سيناريو المنتجات الجديدة
# ══════════════════════════════════════════════════════════════════════════
def send_missing_products(products: List[Dict]) -> Dict:
    if not products:
        return {"success": False, "message": "❌ لا توجد منتجات مفقودة للإرسال"}

    # ── بوابة إلزامية: وصف مهووس + رابط صورة حقيقي ────────────────────
    try:
        from utils.product_gate import validate_and_enrich
        products, _gate_rejected = validate_and_enrich(products, auto_generate_desc=True)
        gate_skipped = len(_gate_rejected)
        if gate_skipped:
            logger.warning("🚫 بوابة الجودة استبعدت %d منتج مفقود (وصف/صورة مفقود)", gate_skipped)
    except Exception as _e:
        logger.error("فشل تطبيق بوابة الجودة: %s", _e)
        gate_skipped = 0
    if not products:
        return {"success": False,
                "message": f"❌ لا توجد منتجات مفقودة صالحة — رفض {gate_skipped} لغياب وصف مهووس أو صورة حقيقية"}

    sent, skipped, errors = 0, gate_skipped, []

    for p in products:
        name  = str(p.get("name", p.get("المنتج", p.get("منتج_المنافس", "")))).strip()
        comp_price = _safe_float(
            p.get("سعر_المنافس", 0) or p.get("comp_price", 0) or p.get("competitor_price", 0)
        )
        # قاعدة التسعير للمفقودات: سعر المنافس − 1
        if comp_price > 0:
            price = max(int(round(comp_price - 1)), 1)
        else:
            price = int(round(_safe_float(p.get("price", 0) or p.get("السعر", 0))))
        product_no = _extract_no(p) or _clean_pid(p.get("NO", ""))
        pid = product_no or _clean_pid(p.get("product_id", p.get("معرف_المنتج", "")))

        if not name or price <= 0:
            skipped += 1
            continue

        item = {
            "NO":              product_no,                # ← Primary Key Make
            "product_id":      pid,
            "أسم المنتج":      name,
            "سعر المنتج":      price,                      # uinteger لـ Salla
            "رمز المنتج sku":  str(p.get("sku", p.get("رمز المنتج sku", ""))).strip(),
            "الوزن":           1,                          # ثابت حسب القاعدة
            "سعر التكلفة":     int(round(_safe_float(p.get("cost_price", p.get("سعر التكلفة", 0))))),
            "السعر المخفض":    int(round(_safe_float(p.get("sale_price",  p.get("السعر المخفض", 0))))),
            "الوصف":           str(p.get("الوصف", p.get("description", ""))).strip(),
            "صورة المنتج":     str(p.get("image_url", p.get("صورة المنتج", ""))).strip(),
            "brand_id":        _resolve_brand_id(p),
            "category_id":     _resolve_category_id(p),
        }
        # تنظيف: إزالة الحقول الفارغة None
        item = {k: v for k, v in item.items() if v not in (None, "")}

        result = _post_to_webhook(WEBHOOK_NEW_PRODUCTS, {"data": [item]})
        if result["success"]:
            sent += 1
        else:
            errors.append(name)

        if len(products) > 1:
            time.sleep(0.3)

    failed = len(errors)
    if sent == 0:
        return {
            "success": False,
            "message": f"❌ فشل إرسال جميع المنتجات المفقودة. تم تخطي {skipped}",
            "sent": sent,
            "failed": failed,
            "total": len(products),
            "status_code": 0,
        }

    skip_msg = f" (تم تخطي {skipped})" if skipped else ""
    err_msg  = f" (فشل {len(errors)})" if errors else ""
    return {
        "success": sent > 0,
        "message": f"✅ تم إرسال {sent} منتج مفقود إلى Make{skip_msg}{err_msg}",
        "sent": sent,
        "failed": failed,
        "total": len(products),
        "status_code": 200 if failed == 0 else 207,
    }


# ══════════════════════════════════════════════════════════════════════════
#  إرسال بدفعات ذكية مع retry و progress callback
# ══════════════════════════════════════════════════════════════════════════
def send_batch_smart(products: list, batch_type: str = "update",
                     batch_size: int = 20, max_retries: int = 3,
                     progress_cb=None, confidence_filter: str = "") -> Dict:
    if not products:
        return {"success": False, "message": "❌ لا توجد منتجات للإرسال",
                "sent": 0, "failed": 0, "total": 0, "errors": []}

    if confidence_filter:
        products = [p for p in products
                    if p.get("مستوى_الثقة", "green") == confidence_filter
                    or p.get("confidence_level", "green") == confidence_filter]

    total = len(products)
    if total == 0:
        return {"success": False, "message": "❌ لا توجد منتجات بهذا المستوى من الثقة",
                "sent": 0, "failed": 0, "total": 0, "errors": []}

    sent_count = 0
    fail_count = 0
    error_names = []

    for i in range(0, total, batch_size):
        batch = products[i:i + batch_size]

        for attempt in range(1, max_retries + 1):
            try:
                if batch_type == "update":
                    result = send_price_updates(batch)
                else:
                    result = send_new_products(batch)

                if result["success"]:
                    sent_count += len(batch)
                    break
                elif attempt < max_retries:
                    time.sleep(2 * attempt)
                    continue
                else:
                    fail_count += len(batch)
                    error_names.extend([p.get("name", p.get("منتج_المنافس", "?"))[:30] for p in batch])
            except Exception:
                if attempt >= max_retries:
                    fail_count += len(batch)
                    error_names.extend([p.get("name", "?")[:30] for p in batch])
                else:
                    time.sleep(2 * attempt)

        if progress_cb:
            try:
                progress_cb(sent_count, fail_count, total,
                           batch[-1].get("name", "")[:30] if batch else "")
            except Exception:
                pass

        if i + batch_size < total:
            time.sleep(0.5)

    success = sent_count > 0
    msg_parts = []
    if sent_count > 0:
        msg_parts.append(f"✅ نجح {sent_count}")
    if fail_count > 0:
        msg_parts.append(f"❌ فشل {fail_count}")
    msg = f"إرسال {total} منتج: {' | '.join(msg_parts)}"

    return {
        "success":  success,
        "message":  msg,
        "sent":     sent_count,
        "failed":   fail_count,
        "total":    total,
        "errors":   error_names[:20],
    }


# ══════════════════════════════════════════════════════════════════════════
#  فحص حالة الاتصال بـ Webhooks
# ══════════════════════════════════════════════════════════════════════════
def verify_webhook_connection() -> Dict:
    test_price_payload = {
        "products": [{
            "NO":         "1",
            "product_id": 1,
            "name":       "اختبار الاتصال",
            "price":      1.0,
            "section":    "test",
        }]
    }
    r1 = _post_to_webhook(WEBHOOK_UPDATE_PRICES, test_price_payload)

    test_new_payload = {
        "data": [{
            "NO":             "",
            "product_id":     "",
            "أسم المنتج":     "اختبار الاتصال",
            "سعر المنتج":     1.0,
            "رمز المنتج sku": "",
            "الوزن":          1,
            "سعر التكلفة":    0,
            "السعر المخفض":   0,
            "الوصف":          "test",
        }]
    }
    r2 = _post_to_webhook(WEBHOOK_NEW_PRODUCTS, test_new_payload)

    return {
        "update_prices": {
            "success": r1["success"],
            "message": r1["message"],
            "url": WEBHOOK_UPDATE_PRICES[:55] + "..." if len(WEBHOOK_UPDATE_PRICES) > 55 else WEBHOOK_UPDATE_PRICES,
        },
        "new_products": {
            "success": r2["success"],
            "message": r2["message"],
            "url": WEBHOOK_NEW_PRODUCTS[:55] + "..." if len(WEBHOOK_NEW_PRODUCTS) > 55 else WEBHOOK_NEW_PRODUCTS,
        },
        "all_connected": r1["success"] and r2["success"],
    }


# ══════════════════════════════════════════════════════════════════════════
#  تصدير ملفات سلة للرفع اليدوي (بديل عن Webhook)
# ══════════════════════════════════════════════════════════════════════════

# أعمدة ملف استيراد المنتجات في سلة (من قالب «منتج جديد.csv»)
SALLA_PRODUCT_COLUMNS = [
    "النوع ", "أسم المنتج", "تصنيف المنتج", "صورة المنتج", "وصف صورة المنتج",
    "نوع المنتج", "سعر المنتج", "الوصف", "هل يتطلب شحن؟", "رمز المنتج sku",
    "سعر التكلفة", "السعر المخفض", "تاريخ بداية التخفيض", "تاريخ نهاية التخفيض",
    "اقصي كمية لكل عميل", "إخفاء خيار تحديد الكمية", "اضافة صورة عند الطلب",
    "الوزن", "وحدة الوزن", "الماركة", "العنوان الترويجي", "تثبيت المنتج",
    "الباركود", "السعرات الحرارية", "MPN", "GTIN", "خاضع للضريبة ؟",
    "سبب عدم الخضوع للضريبة",
]

# أعمدة ملف استيراد الماركات في سلة (من قالب «ماركات مهووس.csv»)
SALLA_BRAND_COLUMNS = [
    "اسم الماركة", "وصف مختصر عن الماركة", "صورة شعار الماركة",
    "(إختياري) صورة البانر", "(Page Title) عنوان صفحة العلامة التجارية",
    "(SEO Page URL) رابط صفحة العلامة التجارية",
    "(Page Description) وصف صفحة العلامة التجارية",
]


def export_missing_products_to_salla_csv(products: List[Dict], output_path: str) -> Dict:
    """
    تصدير المنتجات المفقودة إلى ملف CSV بصيغة قالب استيراد منتجات سلة.
    للرفع اليدوي في لوحة تحكم سلة → إدارة المنتجات → استيراد.

    السعر = سعر المنافس − 1 | الوزن = 1 | الكمية الافتراضية = 100
    """
    import csv

    if not products:
        return {"success": False, "message": "❌ لا توجد منتجات للتصدير", "path": ""}

    # ── بوابة إلزامية: وصف مهووس + صورة حقيقية ────────────────────────
    try:
        from utils.product_gate import validate_and_enrich
        products, _gate_rejected = validate_and_enrich(list(products), auto_generate_desc=True)
        _gate_skipped = len(_gate_rejected)
        if _gate_skipped:
            logger.warning("🚫 بوابة التصدير: رفض %d منتج (وصف/صورة مفقود)", _gate_skipped)
    except Exception as _e:
        logger.error("فشل بوابة التصدير: %s", _e)
        _gate_skipped = 0
    if not products:
        return {"success": False,
                "message": f"❌ لا منتجات صالحة للتصدير — رُفض {_gate_skipped} لغياب وصف مهووس أو صورة حقيقية",
                "path": ""}

    rows = []
    for p in products:
        name = str(p.get("name", p.get("المنتج", p.get("منتج_المنافس", "")))).strip()
        comp_price = _safe_float(
            p.get("سعر_المنافس", 0) or p.get("comp_price", 0) or p.get("competitor_price", 0)
        )
        price = max(int(round(comp_price - 1)), 1) if comp_price > 0 else int(
            round(_safe_float(p.get("price", 0) or p.get("السعر", 0)))
        )
        if not name or price <= 0:
            continue

        row = {col: "" for col in SALLA_PRODUCT_COLUMNS}
        row["النوع "]              = "منتج"
        row["أسم المنتج"]          = name
        row["تصنيف المنتج"]        = str(p.get("category_name", p.get("التصنيف", "")))
        row["صورة المنتج"]         = str(p.get("image_url", p.get("صورة المنتج", "")))
        row["وصف صورة المنتج"]     = f"زجاجة {name}"
        row["نوع المنتج"]          = "منتج جاهز"
        row["سعر المنتج"]          = price
        row["الوصف"]               = str(p.get("الوصف", p.get("description", "")))
        row["هل يتطلب شحن؟"]       = "نعم"
        row["رمز المنتج sku"]      = str(p.get("sku", p.get("رمز المنتج sku", "")))
        row["سعر التكلفة"]         = int(round(_safe_float(p.get("cost_price", 0))))
        row["السعر المخفض"]        = int(round(_safe_float(p.get("sale_price", 0))))
        row["الوزن"]               = 1
        row["وحدة الوزن"]          = "كجم"
        row["الماركة"]             = str(p.get("brand", p.get("الماركة", "")))
        row["إخفاء خيار تحديد الكمية"] = "لا"
        row["تثبيت المنتج"]        = "لا"
        row["خاضع للضريبة ؟"]      = "نعم"
        rows.append(row)

    if not rows:
        return {"success": False, "message": "❌ لا توجد منتجات صالحة للتصدير", "path": ""}

    try:
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SALLA_PRODUCT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return {
            "success": True,
            "message": f"✅ تم تصدير {len(rows)} منتج إلى ملف سلة",
            "path": output_path,
            "count": len(rows),
        }
    except Exception as e:
        return {"success": False, "message": f"❌ فشل التصدير: {e}", "path": ""}


def export_missing_brands_to_salla_csv(
    brands: List[Dict], existing_brands: List[str], output_path: str
) -> Dict:
    """
    تصدير الماركات المفقودة إلى ملف CSV بصيغة قالب استيراد ماركات سلة.
    للرفع اليدوي → لوحة سلة → الماركات → استيراد.

    brands: قائمة dicts فيها 'name' و 'description' و 'logo_url' (اختيارية)
    existing_brands: قائمة أسماء الماركات الموجودة (للاستثناء)
    """
    import csv

    if not brands:
        return {"success": False, "message": "❌ لا توجد ماركات للتصدير", "path": ""}

    existing_norm = {str(b).strip().lower() for b in (existing_brands or []) if b}
    rows = []
    seen = set()

    for b in brands:
        name = str(b.get("name", b.get("brand", b.get("الماركة", "")))).strip()
        if not name:
            continue
        key = name.lower()
        if key in existing_norm or key in seen:
            continue
        seen.add(key)

        row = {col: "" for col in SALLA_BRAND_COLUMNS}
        row["اسم الماركة"]                                      = name
        row["وصف مختصر عن الماركة"]                             = str(
            b.get("description", f"ماركة {name} - متوفرة في مهووس للعطور")
        )
        row["صورة شعار الماركة"]                                = str(b.get("logo_url", ""))
        row["(Page Title) عنوان صفحة العلامة التجارية"]         = f"{name} | عطور فاخرة - مهووس"
        row["(SEO Page URL) رابط صفحة العلامة التجارية"]        = f"ماركة-{name.replace(' ', '-')}"
        row["(Page Description) وصف صفحة العلامة التجارية"]     = (
            f"اكتشف تشكيلة {name} الفاخرة في مهووس للعطور. عطور أصلية بأفضل الأسعار."
        )
        rows.append(row)

    if not rows:
        return {"success": False, "message": "ℹ️ كل الماركات موجودة — لا شيء للتصدير", "path": ""}

    try:
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SALLA_BRAND_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return {
            "success": True,
            "message": f"✅ تم تصدير {len(rows)} ماركة مفقودة",
            "path": output_path,
            "count": len(rows),
        }
    except Exception as e:
        return {"success": False, "message": f"❌ فشل التصدير: {e}", "path": ""}
