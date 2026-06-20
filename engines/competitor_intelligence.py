"""
engines/competitor_intelligence.py — محرك ذكاء المنافسين v31
=============================================================
تحليل ذكي لمنتجات المنافسين مباشرة من SQLite:
✅ Pagination — لا يحمّل 103K دفعة واحدة
✅ فهارس DB — بحث أسرع 10x
✅ بصمة ذكية — كشف المفقود بدون تكرار
✅ تجهيز للإرسال عبر Make.com
"""
import sqlite3
import os
import re
import unicodedata
import logging
import time
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from rapidfuzz import fuzz, process as rf_process
    _HAS_RF = True
except ImportError:
    _HAS_RF = False

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.environ.get("DATA_DIR", "data"), "pricing_v18.db")

# ── تطبيع عربي ──────────────────────────────────────────────────────────
_AR_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")
_SIZE_RE = re.compile(r"(\d+)\s*(?:ml|مل)", re.IGNORECASE)
_NOISE_WORDS = re.compile(
    r"\b(عطر|تستر|بارفيوم|ماء|eau\s*de|parfum|toilette|edp|edt|cologne|spray|"
    r"for\s*men|for\s*women|للرجال|للنساء|رجالي|نسائي|للجنسين|unisex|"
    r"أو\s*دي|او\s*دو|او\s*دي)\b",
    re.IGNORECASE | re.UNICODE,
)


def _normalize(text: str) -> str:
    """تطبيع نص عربي للمقارنة"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    s = _AR_DIACRITICS.sub("", s)
    s = (s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
         .replace("ة", "ه").replace("ى", "ي").replace("ـ", ""))
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _fingerprint(name: str, brand: str = "") -> str:
    """بصمة فريدة لمنتج: brand|core_name|size"""
    norm_brand = _normalize(brand)[:30]
    norm_name = _normalize(name)
    # استخراج الحجم
    size_match = _SIZE_RE.search(name)
    size = size_match.group(1) if size_match else ""
    # إزالة الكلمات الزائدة
    core = _NOISE_WORDS.sub(" ", norm_name)
    core = re.sub(r"\s+", " ", core).strip()
    # إزالة الأرقام المنفردة (إلا الحجم)
    return f"{norm_brand}|{core}|{size}"


class CompetitorIntelligence:
    """محرك ذكاء المنافسين — كل التحليلات من DB مباشرة"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        if os.path.exists(self.db_path):
            self._ensure_columns()
            self._ensure_indexes()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_columns(self):
        """إضافة أعمدة جديدة إذا لم تكن موجودة"""
        cols = [
            ("first_seen_at", "TEXT"),
            ("rating_count", "INTEGER DEFAULT 0"),
            ("discount_pct", "REAL DEFAULT 0"),
            ("original_price", "REAL DEFAULT 0"),
            ("category", "TEXT DEFAULT ''"),
            ("sku", "TEXT DEFAULT ''"),
        ]
        import re as _re
        from contextlib import closing as _closing
        _ALLOWED_TYPES = {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"}
        try:
            # contextlib.closing: يضمن إغلاق الاتصال فعلياً حتى عند حدوث استثناء
            # (ملاحظة: `with sqlite3.connect()` وحده يلتزم/يتراجع لكنه لا يُغلق الاتصال)
            with _closing(sqlite3.connect(self.db_path)) as conn:
                cur = conn.cursor()
                # تأكد من وجود الجدول
                cur.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='competitor_products_store'
                """)
                if not cur.fetchone():
                    return
                for col_name, col_type in cols:
                    # whitelist: اسم العمود معرّف صالح + النوع الأساسي ضمن القائمة المسموحة
                    if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col_name):
                        continue
                    if col_type.split()[0].upper() not in _ALLOWED_TYPES:
                        continue
                    try:
                        cur.execute(f"ALTER TABLE competitor_products_store ADD COLUMN {col_name} {col_type}")
                    except sqlite3.OperationalError:
                        pass
                conn.commit()
        except Exception as e:
            log.warning("_ensure_columns: %s", e)

    def _ensure_indexes(self):
        """فهارس الأداء يملكها db_manager.ensure_indexes() (المصدر الوحيد).

        L2/C2: كانت هذه الدالة تنشئ عائلة idx_ci_* المكرّرة (نفس أعمدة
        idx_cps_*2 / idx_cps_norm2 / idx_cps_first_seen) فتُضخّم كلفة الإدخال
        وتُعيد التكرار في المسار الحيّ. أُفرِغت لمنع إعادة الإنشاء؛ الأعمدة
        (competitor/brand/price/norm_name/first_seen) مفهرسة قانونياً في db_manager.
        (لا تُمَسّ _ensure_columns — إضافة الأعمدة لا تزال مطلوبة.)
        """
        return

    # ══════════════════════════════════════════════════════════════════
    #  استعلامات مع Pagination
    # ══════════════════════════════════════════════════════════════════

    def _build_where(self, filters: dict = None) -> Tuple[str, list]:
        """بناء WHERE clause من الفلاتر"""
        conditions = []
        params = []
        if not filters:
            return "", params

        if filters.get("competitor"):
            conditions.append("competitor = ?")
            params.append(filters["competitor"])
        if filters.get("brand"):
            conditions.append("brand = ?")
            params.append(filters["brand"])
        if filters.get("category"):
            conditions.append("category LIKE ?")
            params.append(f"%{filters['category']}%")
        if filters.get("price_min"):
            conditions.append("price >= ?")
            params.append(float(filters["price_min"]))
        if filters.get("price_max"):
            conditions.append("price <= ?")
            params.append(float(filters["price_max"]))
        if filters.get("search"):
            conditions.append("(product_name LIKE ? OR brand LIKE ?)")
            s = f"%{filters['search']}%"
            params.extend([s, s])

        where = " AND ".join(conditions)
        return f"WHERE {where}" if where else "", params

    def _order_by(self, filters: dict = None) -> str:
        """ترتيب النتائج"""
        sort = (filters or {}).get("sort_by", "newest")
        return {
            "price_asc": "ORDER BY price ASC",
            "price_desc": "ORDER BY price DESC",
            "newest": "ORDER BY COALESCE(first_seen_at, added_at) DESC",
            "rating": "ORDER BY COALESCE(rating_count, 0) DESC, COALESCE(discount_pct, 0) DESC",
        }.get(sort, "ORDER BY id DESC")

    def get_products_page(self, page=0, per_page=25, filters=None) -> Tuple[List[Dict], int]:
        """صفحة من المنتجات مع فلاتر"""
        where, params = self._build_where(filters)
        order = self._order_by(filters)
        offset = page * per_page

        try:
            conn = self._get_conn()
            # العدد الإجمالي
            total = conn.execute(
                f"SELECT COUNT(*) FROM competitor_products_store {where}", params
            ).fetchone()[0]

            # الصفحة
            rows = conn.execute(
                f"SELECT * FROM competitor_products_store {where} {order} LIMIT ? OFFSET ?",
                params + [per_page, offset]
            ).fetchall()
            conn.close()

            products = [dict(r) for r in rows]
            return products, total
        except Exception as e:
            log.error("get_products_page: %s", e)
            return [], 0

    def get_new_products(self, days=7, page=0, per_page=25, filters=None) -> Tuple[List[Dict], int]:
        """المنتجات الجديدة في آخر N أيام"""
        where, params = self._build_where(filters)
        date_filter = f"COALESCE(first_seen_at, added_at) >= date('now', '-{int(days)} days')"

        if where:
            where = f"{where} AND {date_filter}"
        else:
            where = f"WHERE {date_filter}"

        order = self._order_by(filters) if filters else "ORDER BY COALESCE(first_seen_at, added_at) DESC"
        offset = page * per_page

        try:
            conn = self._get_conn()
            total = conn.execute(
                f"SELECT COUNT(*) FROM competitor_products_store {where}", params
            ).fetchone()[0]

            rows = conn.execute(
                f"SELECT * FROM competitor_products_store {where} {order} LIMIT ? OFFSET ?",
                params + [per_page, offset]
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows], total
        except Exception as e:
            log.error("get_new_products: %s", e)
            return [], 0

    def get_best_sellers(self, page=0, per_page=25, filters=None) -> Tuple[List[Dict], int]:
        """الأكثر مبيعاً / أعلى خصم"""
        where, params = self._build_where(filters)
        extra = "COALESCE(rating_count, 0) > 0 OR COALESCE(discount_pct, 0) > 0"

        if where:
            where = f"{where} AND ({extra})"
        else:
            where = f"WHERE {extra}"

        order = "ORDER BY COALESCE(rating_count, 0) DESC, COALESCE(discount_pct, 0) DESC"
        offset = page * per_page

        try:
            conn = self._get_conn()
            total = conn.execute(
                f"SELECT COUNT(*) FROM competitor_products_store {where}", params
            ).fetchone()[0]

            rows = conn.execute(
                f"SELECT * FROM competitor_products_store {where} {order} LIMIT ? OFFSET ?",
                params + [per_page, offset]
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows], total
        except Exception as e:
            log.error("get_best_sellers: %s", e)
            return [], 0

    # ══════════════════════════════════════════════════════════════════
    #  كشف المنتجات المفقودة بالبصمة
    # ══════════════════════════════════════════════════════════════════

    def find_missing_products(self, our_catalog_df, page=0, per_page=25, filters=None) -> Tuple[List[Dict], int]:
        """كشف المنتجات غير المتوفرة لدينا — بدون تكرار"""
        if our_catalog_df is None or (pd and isinstance(our_catalog_df, pd.DataFrame) and our_catalog_df.empty):
            return [], 0

        # بناء بصمات منتجاتنا
        our_fingerprints = set()
        name_col = None
        for c in our_catalog_df.columns:
            cl = str(c).lower()
            if any(k in cl for k in ("المنتج", "اسم", "name", "product")):
                name_col = c
                break
        if not name_col:
            name_col = our_catalog_df.columns[0]

        for _, row in our_catalog_df.iterrows():
            name = str(row.get(name_col, ""))
            brand = ""
            for bc in our_catalog_df.columns:
                if any(k in str(bc).lower() for k in ("ماركة", "brand", "علامة")):
                    brand = str(row.get(bc, ""))
                    break
            fp = _fingerprint(name, brand)
            if fp and len(fp) > 5:
                our_fingerprints.add(fp)

        # جلب منتجات المنافسين — إسقاط الأعمدة الـ6 المستخدمة فقط (لا SELECT *
        # على 25 عمودًا/130K صف). الترقيم لا يمكن نقله إلى SQL لأن البصمة تُحسب
        # في Python وتتطلب مسحًا كاملًا للتجميع، لكن إسقاط الأعمدة يقلّص الزمن جذريًا.
        # H1: بثّ بالدفعات (fetchmany) بدل fetchall — لا نحتجز كل الصفوف الخام في
        # الذاكرة دفعة واحدة (خطر OOM عند مئات الآلاف). نفس الترتيب والنتيجة تمامًا.
        where, params = self._build_where(filters)
        try:
            conn = self._get_conn()
            cur = conn.execute(
                "SELECT product_name, brand, category, image_url, price, competitor "
                f"FROM competitor_products_store {where} "
                f"{'AND' if where else 'WHERE'} price > 0",
                params
            )
        except Exception as e:
            log.error("find_missing: %s", e)
            return [], 0

        def _stream_rows():
            # يبثّ الصفوف 5000-دفعةً ويغلق الاتصال عند الانتهاء (أو عند التحرير)
            try:
                while True:
                    batch = cur.fetchmany(5000)
                    if not batch:
                        break
                    for _r in batch:
                        yield _r
            finally:
                conn.close()

        # تجميع بالبصمة — فهرسة مباشرة للصف (أسرع من dict(row) لكل صف)
        # حارس ذاكرة محمول (بلا psutil): سقف على عدد المفقودات الفريدة. خريطة
        # التجميع هي التكلفة المهيمنة، ونموّها غير المحدود = OOM على Railway عند
        # مئات الآلاف × 10 منافسين. الافتراضي 250,000 > الحجم الحالي (118K) فلا
        # أثر الآن (مطابق بايت-ببايت)، ويُضبط عبر MISSING_MAX_UNIQUE.
        try:
            _MAX_UNIQUE = int(os.environ.get("MISSING_MAX_UNIQUE", "250000"))
        except (TypeError, ValueError):
            _MAX_UNIQUE = 250000
        _capped = False
        missing_map = {}  # fingerprint → aggregated data
        for row in _stream_rows():
            _pn, _br, _cat, _img, _price_raw, _comp = (
                row[0], row[1], row[2], row[3], row[4], row[5]
            )
            r = {"product_name": _pn, "brand": _br, "category": _cat,
                 "image_url": _img, "price": _price_raw, "competitor": _comp}
            fp = _fingerprint(r.get("product_name", ""), r.get("brand", ""))
            if not fp or len(fp) <= 5:
                continue
            if fp in our_fingerprints:
                continue

            if fp not in missing_map:
                # سقف الأمان: عند بلوغ الحدّ لا نُنشئ مفقوداً فريداً جديداً، لكن
                # نتابع تجميع الموجود (أسعار/منافسون يبقون دقيقين) — أقلّ القصّ ضرراً.
                if len(missing_map) >= _MAX_UNIQUE:
                    _capped = True
                    continue
                missing_map[fp] = {
                    "fingerprint": fp,
                    "product_name": r.get("product_name", ""),
                    "brand": r.get("brand", ""),
                    "category": r.get("category", ""),
                    "image_url": r.get("image_url", ""),
                    "min_price": float(r.get("price", 0) or 0),
                    "max_price": float(r.get("price", 0) or 0),
                    # متوسط تدريجي (مجموع+عدّاد) بدل قائمة كل الأسعار — يحذف ~عشرات
                    # الآلاف من كائنات القوائم عند الحجم. المتوسط مطابق بايت-ببايت.
                    "price_sum": float(r.get("price", 0) or 0),
                    "price_count": 1,
                    "competitors": [r.get("competitor", "")],
                    "competitor_count": 1,
                    "suggested_price": max(0, float(r.get("price", 0) or 0) - 1),
                }
            else:
                m = missing_map[fp]
                price = float(r.get("price", 0) or 0)
                m["price_sum"] += price
                m["price_count"] += 1
                if price < m["min_price"]:
                    m["min_price"] = price
                if price > m["max_price"]:
                    m["max_price"] = price
                comp = r.get("competitor", "")
                if comp and comp not in m["competitors"]:
                    m["competitors"].append(comp)
                    m["competitor_count"] = len(m["competitors"])
                if not m["image_url"] and r.get("image_url"):
                    m["image_url"] = r["image_url"]
                m["suggested_price"] = max(0, m["min_price"] - 1)

        if _capped:
            log.warning(
                "find_missing: بلغ سقف المفقودات الفريدة %d (MISSING_MAX_UNIQUE) — "
                "أُوقف إنشاء عناصر فريدة جديدة لحماية الذاكرة. ارفع السقف أو ضيّق "
                "الفلاتر إن لزم عرض المزيد.", _MAX_UNIQUE,
            )

        # تحويل لقائمة مرتبة
        missing_list = sorted(missing_map.values(), key=lambda x: x["competitor_count"], reverse=True)

        # حساب المتوسط من المجموع/العدّاد التدريجي (نفس قيمة sum(prices)/len تماماً)
        for m in missing_list:
            _psum = m.pop("price_sum", 0.0)
            _pcnt = m.pop("price_count", 0)
            m["avg_price"] = round(_psum / _pcnt, 2) if _pcnt else 0
            m["competitors_list"] = ", ".join(m.pop("competitors", []))

        total = len(missing_list)
        start = page * per_page
        end = start + per_page
        return missing_list[start:end], total

    # ══════════════════════════════════════════════════════════════════
    #  مقارنة المتاجر
    # ══════════════════════════════════════════════════════════════════

    def compare_stores(self) -> List[Dict]:
        """مقارنة إحصائيات المتاجر"""
        try:
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT competitor,
                       COUNT(*) as total_products,
                       ROUND(AVG(price), 0) as avg_price,
                       ROUND(MIN(price), 0) as min_price,
                       ROUND(MAX(price), 0) as max_price,
                       SUM(CASE WHEN COALESCE(discount_pct, 0) > 0 THEN 1 ELSE 0 END) as on_sale,
                       SUM(CASE WHEN COALESCE(first_seen_at, added_at) >= date('now','-7 days') THEN 1 ELSE 0 END) as new_7d
                FROM competitor_products_store
                WHERE price > 0
                GROUP BY competitor
                ORDER BY total_products DESC
            """).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            log.error("compare_stores: %s", e)
            return []

    # ══════════════════════════════════════════════════════════════════
    #  معلومات مساعدة
    # ══════════════════════════════════════════════════════════════════

    def get_available_brands(self) -> List[str]:
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT DISTINCT brand FROM competitor_products_store WHERE brand != '' AND brand IS NOT NULL ORDER BY brand"
            ).fetchall()
            conn.close()
            return [r[0] for r in rows]
        except Exception:
            return []

    def get_available_competitors(self) -> List[str]:
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT DISTINCT competitor FROM competitor_products_store ORDER BY competitor"
            ).fetchall()
            conn.close()
            return [r[0] for r in rows]
        except Exception:
            return []

    def get_stats(self) -> Dict:
        """إحصائيات سريعة"""
        try:
            conn = self._get_conn()
            r = conn.execute("""
                SELECT 
                    COUNT(*) as total_products,
                    COUNT(DISTINCT competitor) as total_competitors,
                    COUNT(DISTINCT CASE WHEN brand != '' AND brand IS NOT NULL THEN brand END) as total_brands,
                    SUM(CASE WHEN COALESCE(discount_pct, 0) > 0 THEN 1 ELSE 0 END) as products_on_sale,
                    SUM(CASE WHEN COALESCE(first_seen_at, added_at) >= date('now','-7 days') THEN 1 ELSE 0 END) as new_7d
                FROM competitor_products_store
                WHERE price > 0
            """).fetchone()
            conn.close()
            return dict(r) if r else {}
        except Exception as e:
            log.error("get_stats: %s", e)
            return {}

    def prepare_for_make(self, product: dict) -> dict:
        """تجهيز منتج مفقود للإرسال عبر Make.com"""
        name = product.get("product_name", "")
        brand = product.get("brand", "")
        price = product.get("suggested_price", 0)
        image = product.get("image_url", "")

        # تحديد التصنيف من الاسم
        name_lower = name.lower()
        if any(w in name_lower for w in ("نسائي", "نساء", "women", "femme", "lady")):
            category = "العطور > عطور نسائية"
        elif any(w in name_lower for w in ("رجالي", "رجال", "men", "homme")):
            category = "العطور > عطور رجالية"
        else:
            category = "العطور > عطور للجنسين"

        # محاولة ربط الماركة
        canonical_brand = brand
        try:
            from utils.brand_manager import resolve_brand
            canonical_brand, is_new = resolve_brand(brand, auto_generate=False)
        except Exception:
            pass

        return {
            "اسم المنتج": name,
            "سعر المنتج": round(float(price), 2),
            "رمز المنتج sku": "",
            "صورة المنتج": image,
            "تصنيف المنتج": category,
            "الماركة": canonical_brand,
            "السعر المقترح": round(float(price), 2),
        }
