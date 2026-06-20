"""
utils/missing_queue_manager.py — نظام طابور المنتجات المفقودة الذكي
═══════════════════════════════════════════════════════════════════
يدير دورة حياة المنتجات المفقودة من الاكتشاف حتى الإرسال لسلة.

المنطق الأساسي:
  1. تطبيع أسماء الماركات/المنتجات → بصمة hash فريدة لمنع التكرار
  2. كل منتج له حالة: waiting_brand | ready_to_send | sent_success | sent_failed
  3. كل اكتشاف جديد يُفحص ضد الطابور → لا إضافة مكررة
  4. عند تحديث كتالوج الماركات → المنتجات waiting_brand تُرقَّى تلقائياً

ملفات البيانات (data/):
  brand_catalog.csv        — ماركات المتجر (يُحدَّث من رفع ماركات مهووس)
  category_catalog.csv     — تصنيفات المتجر (ثابتة)
  missing_brands_queue.csv — ماركات مفقودة بانتظار الرفع اليدوي
  missing_products_queue.csv — طابور كامل المنتجات المفقودة
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("MissingQueueManager")

# ── مسارات الملفات ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent / "data"
BRAND_CATALOG_FILE    = BASE_DIR / "brand_catalog.csv"
CATEGORY_CATALOG_FILE = BASE_DIR / "category_catalog.csv"
BRANDS_QUEUE_FILE     = BASE_DIR / "missing_brands_queue.csv"
PRODUCTS_QUEUE_FILE   = BASE_DIR / "missing_products_queue.csv"

# أعمدة الطوابير
BRANDS_QUEUE_COLS = [
    "brand_key", "brand_name", "status",       # pending | uploaded
    "discovered_at", "uploaded_at", "notes",
]
PRODUCTS_QUEUE_COLS = [
    "product_key", "brand_key", "brand_name",
    "product_name", "price", "comp_price",
    "sku", "image_url", "description",
    "category_name", "category_id",
    "status",                                   # waiting_brand | ready_to_send | sent_success | sent_failed
    "discovered_at", "sent_at", "error_msg",
]

# أعمدة ملف استيراد الماركات في سلة
SALLA_BRAND_COLS = [
    "اسم الماركة", "وصف مختصر عن الماركة", "صورة شعار الماركة",
    "(إختياري) صورة البانر",
    "(Page Title) عنوان صفحة العلامة التجارية",
    "(SEO Page URL) رابط صفحة العلامة التجارية",
    "(Page Description) وصف صفحة العلامة التجارية",
]

# أعمدة ملف استيراد المنتجات في سلة
SALLA_PRODUCT_COLS = [
    "النوع ", "أسم المنتج", "تصنيف المنتج", "صورة المنتج",
    "وصف صورة المنتج", "نوع المنتج", "سعر المنتج", "الوصف",
    "هل يتطلب شحن؟", "رمز المنتج sku", "سعر التكلفة", "السعر المخفض",
    "تاريخ بداية التخفيض", "تاريخ نهاية التخفيض",
    "اقصي كمية لكل عميل", "إخفاء خيار تحديد الكمية",
    "اضافة صورة عند الطلب", "الوزن", "وحدة الوزن",
    "الماركة", "العنوان الترويجي", "تثبيت المنتج",
    "الباركود", "السعرات الحرارية", "MPN", "GTIN",
    "خاضع للضريبة ؟", "سبب عدم الخضوع للضريبة",
]


# ══════════════════════════════════════════════════════════════════════════════
#  أدوات التطبيع والبصمة
# ══════════════════════════════════════════════════════════════════════════════

_AR_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670]")
_NON_ALNUM    = re.compile(r"[^\w\u0600-\u06FF]")


def _normalize(text: str) -> str:
    """تطبيع النص العربي: إزالة تشكيل + توحيد حروف + lowercase."""
    if not isinstance(text, str):
        return ""
    t = _AR_DIACRITICS.sub("", text)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ة", "ه").replace("ى", "ي")
    t = _NON_ALNUM.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _brand_key(brand_name: str) -> str:
    """بصمة فريدة للماركة."""
    return _normalize(brand_name)[:64]


def _product_key(brand_name: str, product_name: str) -> str:
    """بصمة فريدة للمنتج = hash(brand_key + product_normalized)."""
    raw = _brand_key(brand_name) + "|" + _normalize(product_name)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ══════════════════════════════════════════════════════════════════════════════
#  قراءة / كتابة الملفات
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_dir():
    BASE_DIR.mkdir(parents=True, exist_ok=True)


def _read_csv(path: Path, cols: List[str]) -> pd.DataFrame:
    if path.exists() and path.stat().st_size > 0:
        try:
            df = pd.read_csv(path, dtype=str).fillna("")
            for c in cols:
                if c not in df.columns:
                    df[c] = ""
            return df[cols]
        except Exception:
            pass
    return pd.DataFrame(columns=cols)


def _write_csv(df: pd.DataFrame, path: Path):
    _ensure_dir()
    df.to_csv(path, index=False, encoding="utf-8-sig")


# ══════════════════════════════════════════════════════════════════════════════
#  كتالوج الماركات
# ══════════════════════════════════════════════════════════════════════════════

def load_brand_catalog() -> Dict[str, str]:
    """يعيد dict: brand_key → brand_name للماركات الموجودة في المتجر."""
    df = _read_csv(BRAND_CATALOG_FILE, ["brand_key", "brand_name"])
    return dict(zip(df["brand_key"], df["brand_name"]))


def load_brand_id_map() -> Dict[str, str]:
    """يعيد dict: brand_key → brand_id لربط اسم الماركة بمعرّف سلة الرقمي.
    يقرأ عمود brand_id من brand_catalog.csv (يُضاف فارغاً إن لم يكن موجوداً)؛
    يتجاهل القيم الفارغة/الصفرية. مكمّل لـ load_category_catalog للتصنيفات."""
    df = _read_csv(BRAND_CATALOG_FILE, ["brand_key", "brand_name", "brand_id"])
    out: Dict[str, str] = {}
    for _, r in df.iterrows():
        bid = str(r.get("brand_id", "")).strip()
        if not bid or bid in ("0", "nan", "None"):
            continue
        key = str(r.get("brand_key", "")).strip() or _brand_key(str(r.get("brand_name", "")))
        if key:
            out[key] = bid
    return out


def update_brand_catalog_from_file(uploaded_df: pd.DataFrame) -> Dict:
    """
    يحدّث كتالوج الماركات من DataFrame مرفوع (بصيغة ماركات مهووس.csv).
    يقبل عمود 'اسم الماركة' أو 'brand_name'.
    """
    _ensure_dir()
    col = None
    for c in ["اسم الماركة", "brand_name", "name", "الماركة"]:
        if c in uploaded_df.columns:
            col = c
            break
    if col is None:
        return {"success": False, "message": "❌ لم يُعثر على عمود اسم الماركة في الملف"}

    # brand_id: رقم الماركة في سلة (قد لا يكون في الملف — يُترك فارغاً)
    col_id = None
    for c in ["brand_id", "id", "الرقم", "معرف الماركة"]:
        if c in uploaded_df.columns:
            col_id = c
            break

    names = uploaded_df[col].dropna().astype(str).str.strip()
    names = names[names != ""]
    ids   = uploaded_df[col_id].astype(str) if col_id else pd.Series([""] * len(names))
    rows = [{"brand_key": _brand_key(n), "brand_name": n, "brand_id": str(i).strip()}
            for n, i in zip(names, ids)]
    df = pd.DataFrame(rows).drop_duplicates("brand_key")
    _write_csv(df, BRAND_CATALOG_FILE)

    # رقّي المنتجات waiting_brand التي أصبحت ماركاتها متوفرة
    upgraded = _upgrade_waiting_products(set(df["brand_key"]))
    return {
        "success": True,
        "message": f"✅ تم تحديث الكتالوج: {len(df)} ماركة",
        "count": len(df),
        "upgraded_products": upgraded,
    }


def _upgrade_waiting_products(available_keys: set) -> int:
    """رقّي المنتجات waiting_brand → ready_to_send إذا أصبحت ماركاتها متوفرة."""
    pq = _read_csv(PRODUCTS_QUEUE_FILE, PRODUCTS_QUEUE_COLS)
    if pq.empty:
        return 0
    mask = (pq["status"] == "waiting_brand") & pq["brand_key"].isin(available_keys)
    count = int(mask.sum())
    if count:
        pq.loc[mask, "status"] = "ready_to_send"
        _write_csv(pq, PRODUCTS_QUEUE_FILE)
    return count


# ══════════════════════════════════════════════════════════════════════════════
#  كتالوج التصنيفات (ثابت)
# ══════════════════════════════════════════════════════════════════════════════

def load_category_catalog() -> Dict[str, str]:
    """يعيد dict: category_name → category_id."""
    df = _read_csv(CATEGORY_CATALOG_FILE, ["category_name", "category_id"])
    return dict(zip(df["category_name"], df["category_id"]))


def update_category_catalog_from_file(uploaded_df: pd.DataFrame) -> Dict:
    """
    يحدّث كتالوج التصنيفات من DataFrame (بصيغة تصنيفات مهووس.csv).
    يقبل عمود 'التصنيفات' أو 'category_name'.
    """
    _ensure_dir()
    col_name, col_id = None, None
    for c in ["التصنيفات", "category_name", "name", "التصنيف"]:
        if c in uploaded_df.columns:
            col_name = c
            break
    # category_id: رقم سلة (قد لا يكون في الملف — يُترك فارغاً)
    for c in ["category_id", "id", "الرقم"]:
        if c in uploaded_df.columns:
            col_id = c
            break

    if col_name is None:
        return {"success": False, "message": "❌ لم يُعثر على عمود اسم التصنيف"}

    names = uploaded_df[col_name].dropna().astype(str).str.strip()
    ids   = uploaded_df[col_id].astype(str) if col_id else pd.Series([""] * len(names))
    rows  = [{"category_name": n, "category_id": i} for n, i in zip(names, ids) if n]
    df    = pd.DataFrame(rows).drop_duplicates("category_name")
    _write_csv(df, CATEGORY_CATALOG_FILE)
    return {"success": True, "message": f"✅ تم تحديث التصنيفات: {len(df)} تصنيف", "count": len(df)}


# ══════════════════════════════════════════════════════════════════════════════
#  إضافة منتجات مفقودة إلى الطابور
# ══════════════════════════════════════════════════════════════════════════════

def enqueue_missing_products(products: List[Dict]) -> Dict:
    """
    يضيف منتجات مفقودة للطابور مع:
    - dedup بالبصمة
    - ربط تلقائي بكتالوج الماركات
    - توليد ماركات مفقودة جديدة
    يعيد ملخص العملية.
    """
    _ensure_dir()
    brand_catalog = load_brand_catalog()
    pq = _read_csv(PRODUCTS_QUEUE_FILE, PRODUCTS_QUEUE_COLS)
    bq = _read_csv(BRANDS_QUEUE_FILE, BRANDS_QUEUE_COLS)

    existing_pkeys = set(pq["product_key"])
    existing_bkeys = set(bq["brand_key"])

    new_products, new_brands = [], []
    added, skipped_dup, new_brand_count = 0, 0, 0

    for p in products:
        name  = str(p.get("name", p.get("المنتج", p.get("منتج_المنافس", "")))).strip()
        brand = str(p.get("brand", p.get("الماركة", ""))).strip()
        if not name:
            continue

        pkey = _product_key(brand, name)
        if pkey in existing_pkeys:
            skipped_dup += 1
            continue
        existing_pkeys.add(pkey)

        bkey = _brand_key(brand)
        comp_price = float(p.get("سعر_المنافس", 0) or p.get("comp_price", 0) or 0)
        price = max(int(round(comp_price - 1)), 1) if comp_price > 0 else int(
            round(float(p.get("price", 0) or p.get("السعر", 0) or 0))
        )

        # حالة المنتج بناءً على توفر الماركة
        if bkey in brand_catalog or not brand:
            status = "ready_to_send"
        else:
            status = "waiting_brand"
            # أضف الماركة لطابور المفقود إذا لم تكن موجودة
            if bkey not in existing_bkeys:
                existing_bkeys.add(bkey)
                new_brand_count += 1
                new_brands.append({
                    "brand_key":     bkey,
                    "brand_name":    brand,
                    "status":        "pending",
                    "discovered_at": _now(),
                    "uploaded_at":   "",
                    "notes":         "",
                })

        new_products.append({
            "product_key":   pkey,
            "brand_key":     bkey,
            "brand_name":    brand,
            "product_name":  name,
            "price":         price,
            "comp_price":    comp_price,
            "sku":           str(p.get("sku", p.get("رمز المنتج sku", ""))).strip(),
            "image_url":     str(p.get("image_url", p.get("صورة المنتج", ""))).strip(),
            "description":   str(p.get("الوصف", p.get("description", ""))).strip(),
            "category_name": str(p.get("category_name", p.get("التصنيف", ""))).strip(),
            "category_id":   str(p.get("category_id", "")).strip(),
            "status":        status,
            "discovered_at": _now(),
            "sent_at":       "",
            "error_msg":     "",
        })
        added += 1

    if new_products:
        pq = pd.concat([pq, pd.DataFrame(new_products)], ignore_index=True)
        _write_csv(pq, PRODUCTS_QUEUE_FILE)
    if new_brands:
        bq = pd.concat([bq, pd.DataFrame(new_brands)], ignore_index=True)
        _write_csv(bq, BRANDS_QUEUE_FILE)

    return {
        "added": added,
        "skipped_dup": skipped_dup,
        "new_brands": new_brand_count,
        "message": (
            f"✅ أضفت {added} منتج جديد للطابور"
            + (f" | تجاهل {skipped_dup} مكرر" if skipped_dup else "")
            + (f" | اكتشاف {new_brand_count} ماركة مفقودة جديدة" if new_brand_count else "")
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  إحصاءات الطابور
# ══════════════════════════════════════════════════════════════════════════════

def get_queue_stats() -> Dict:
    """إحصاءات سريعة للطابور."""
    pq = _read_csv(PRODUCTS_QUEUE_FILE, PRODUCTS_QUEUE_COLS)
    bq = _read_csv(BRANDS_QUEUE_FILE, BRANDS_QUEUE_COLS)

    def _count(df, col, val):
        return int((df[col] == val).sum()) if not df.empty and col in df.columns else 0

    return {
        "waiting_brand":   _count(pq, "status", "waiting_brand"),
        "ready_to_send":   _count(pq, "status", "ready_to_send"),
        "sent_success":    _count(pq, "status", "sent_success"),
        "sent_failed":     _count(pq, "status", "sent_failed"),
        "total_products":  len(pq),
        "brands_pending":  _count(bq, "status", "pending"),
        "brands_uploaded": _count(bq, "status", "uploaded"),
        "total_brands":    len(bq),
    }


def get_ready_products() -> List[Dict]:
    """يعيد قائمة المنتجات الجاهزة للإرسال (ready_to_send)."""
    pq = _read_csv(PRODUCTS_QUEUE_FILE, PRODUCTS_QUEUE_COLS)
    if pq.empty:
        return []
    ready = pq[pq["status"] == "ready_to_send"]
    return ready.to_dict("records")


def get_waiting_products() -> List[Dict]:
    """يعيد قائمة المنتجات بانتظار ماركاتها."""
    pq = _read_csv(PRODUCTS_QUEUE_FILE, PRODUCTS_QUEUE_COLS)
    if pq.empty:
        return []
    return pq[pq["status"] == "waiting_brand"].to_dict("records")


def get_failed_products() -> List[Dict]:
    """يعيد قائمة المنتجات الفاشلة لإعادة المحاولة."""
    pq = _read_csv(PRODUCTS_QUEUE_FILE, PRODUCTS_QUEUE_COLS)
    if pq.empty:
        return []
    return pq[pq["status"] == "sent_failed"].to_dict("records")


def get_pending_brands() -> List[Dict]:
    """يعيد الماركات المفقودة بانتظار الرفع اليدوي."""
    bq = _read_csv(BRANDS_QUEUE_FILE, BRANDS_QUEUE_COLS)
    if bq.empty:
        return []
    return bq[bq["status"] == "pending"].to_dict("records")


# ══════════════════════════════════════════════════════════════════════════════
#  تحديث حالة المنتجات بعد الإرسال
# ══════════════════════════════════════════════════════════════════════════════

def mark_products_sent(product_keys: List[str], success: bool, error: str = "") -> int:
    """تحديث حالة منتجات بعد محاولة الإرسال."""
    pq = _read_csv(PRODUCTS_QUEUE_FILE, PRODUCTS_QUEUE_COLS)
    if pq.empty:
        return 0
    mask = pq["product_key"].isin(product_keys)
    count = int(mask.sum())
    if count:
        pq.loc[mask, "status"]   = "sent_success" if success else "sent_failed"
        pq.loc[mask, "sent_at"]  = _now()
        pq.loc[mask, "error_msg"] = "" if success else error
        _write_csv(pq, PRODUCTS_QUEUE_FILE)
    return count


def retry_failed_products() -> int:
    """إعادة تعيين المنتجات الفاشلة إلى ready_to_send."""
    pq = _read_csv(PRODUCTS_QUEUE_FILE, PRODUCTS_QUEUE_COLS)
    if pq.empty:
        return 0
    mask = pq["status"] == "sent_failed"
    count = int(mask.sum())
    if count:
        pq.loc[mask, "status"]    = "ready_to_send"
        pq.loc[mask, "error_msg"] = ""
        _write_csv(pq, PRODUCTS_QUEUE_FILE)
    return count


def mark_brand_uploaded(brand_key: str):
    """تعليم ماركة كمرفوعة + ترقية منتجاتها تلقائياً."""
    bq = _read_csv(BRANDS_QUEUE_FILE, BRANDS_QUEUE_COLS)
    if not bq.empty:
        mask = bq["brand_key"] == brand_key
        bq.loc[mask, "status"]      = "uploaded"
        bq.loc[mask, "uploaded_at"] = _now()
        _write_csv(bq, BRANDS_QUEUE_FILE)
    _upgrade_waiting_products({brand_key})


# ══════════════════════════════════════════════════════════════════════════════
#  تصدير ملفات سلة
# ══════════════════════════════════════════════════════════════════════════════

def export_missing_brands_csv(output_path: str) -> Dict:
    """تصدير الماركات المفقودة بصيغة استيراد سلة."""
    pending = get_pending_brands()
    if not pending:
        return {"success": False, "message": "ℹ️ لا توجد ماركات مفقودة في الطابور"}

    rows = []
    for b in pending:
        name = b.get("brand_name", "")
        if not name:
            continue
        row = {c: "" for c in SALLA_BRAND_COLS}
        row["اسم الماركة"]                                      = name
        row["وصف مختصر عن الماركة"]                             = f"ماركة {name} - متوفرة في مهووس للعطور"
        row["(Page Title) عنوان صفحة العلامة التجارية"]         = f"{name} | عطور فاخرة - مهووس"
        row["(SEO Page URL) رابط صفحة العلامة التجارية"]        = f"ماركة-{name.replace(' ', '-')}"
        row["(Page Description) وصف صفحة العلامة التجارية"]     = (
            f"اكتشف تشكيلة {name} الفاخرة في مهووس. عطور أصلية بأفضل الأسعار."
        )
        rows.append(row)

    _ensure_dir()
    try:
        pd.DataFrame(rows)[SALLA_BRAND_COLS].to_csv(
            output_path, index=False, encoding="utf-8-sig"
        )
        return {
            "success": True,
            "message": f"✅ تم تصدير {len(rows)} ماركة مفقودة",
            "count": len(rows),
            "path": output_path,
        }
    except Exception as e:
        return {"success": False, "message": f"❌ فشل التصدير: {e}"}


def export_ready_products_salla_csv(output_path: str) -> Dict:
    """تصدير المنتجات الجاهزة بصيغة استيراد منتجات سلة."""
    ready = get_ready_products()
    if not ready:
        return {"success": False, "message": "ℹ️ لا توجد منتجات جاهزة"}

    rows = []
    for p in ready:
        row = {c: "" for c in SALLA_PRODUCT_COLS}
        row["النوع "]            = "منتج"
        row["أسم المنتج"]        = p.get("product_name", "")
        row["تصنيف المنتج"]      = p.get("category_name", "")
        row["صورة المنتج"]       = p.get("image_url", "")
        row["وصف صورة المنتج"]   = f"زجاجة {p.get('product_name','')}"
        row["نوع المنتج"]        = "منتج جاهز"
        row["سعر المنتج"]        = p.get("price", "")
        row["الوصف"]             = p.get("description", "")
        row["هل يتطلب شحن؟"]     = "نعم"
        row["رمز المنتج sku"]    = p.get("sku", "")
        row["الوزن"]             = 1
        row["وحدة الوزن"]        = "كجم"
        row["الماركة"]           = p.get("brand_name", "")
        row["إخفاء خيار تحديد الكمية"] = "لا"
        row["تثبيت المنتج"]      = "لا"
        row["خاضع للضريبة ؟"]    = "نعم"
        rows.append(row)

    _ensure_dir()
    try:
        pd.DataFrame(rows)[SALLA_PRODUCT_COLS].to_csv(
            output_path, index=False, encoding="utf-8-sig"
        )
        return {
            "success": True,
            "message": f"✅ تم تصدير {len(rows)} منتج جاهز",
            "count": len(rows),
            "path": output_path,
        }
    except Exception as e:
        return {"success": False, "message": f"❌ فشل التصدير: {e}"}
