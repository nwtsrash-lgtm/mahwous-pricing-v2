"""
utils/db_manager.py - v18.0
- تتبع تاريخ الأسعار (يحدث السعر إذا تغير)
- حفظ نقاط استئناف للمعالجة الخلفية
- قرارات لكل منتج (موافق/تأجيل/إزالة)
- سجل كامل بالتاريخ والوقت
"""
import hashlib
import logging
import re
import sqlite3, json, os, time
from datetime import datetime
from typing import List, Optional, Dict

# ⚡ Performance: in-memory cache for heavy queries
_CACHE = {}  # key -> (timestamp, data)
_CACHE_TTL = 60  # seconds

def _cache_get(key):
    """Get cached value if not expired."""
    if key in _CACHE:
        ts, data = _CACHE[key]
        if time.time() - ts < _CACHE_TTL:
            return data
    return None

def _cache_set(key, data):
    """Set cached value."""
    _CACHE[key] = (time.time(), data)

def invalidate_competitor_cache():
    """Clear competitor-related caches (call after upsert/delete)."""
    keys_to_del = [k for k in _CACHE if k.startswith('comp_')]
    for k in keys_to_del:
        del _CACHE[k]

from utils.data_paths import get_data_db_path

_logger = logging.getLogger(__name__)

# Main SQLite database — path via get_data_db_path() (DATA_DIR on Railway/GCP, ./data locally)
_DB_NAME = "pricing_v18.db"
DB_PATH = get_data_db_path(_DB_NAME)

# ─── GCP Integration ─────────────────────────────────────────────────────────
# Import GCP helpers; if the module is missing (e.g. first run before pip install)
# the app continues with pure SQLite — no crash.
try:
    from utils.gcp_db import (
        sync_db_from_gcs,
        sync_db_to_gcs,
        schedule_background_gcs_sync,
        is_gcs_configured,
        is_cloud_sql_configured,
        gcp_status,
    )
    _GCP_AVAILABLE = True
except ImportError:
    _GCP_AVAILABLE = False
    def sync_db_from_gcs(p): return False          # type: ignore[misc]
    def sync_db_to_gcs(p, force=False): return False  # type: ignore[misc]
    def schedule_background_gcs_sync(p, interval_secs=300): pass  # type: ignore[misc]
    def is_gcs_configured(): return False          # type: ignore[misc]
    def is_cloud_sql_configured(): return False    # type: ignore[misc]
    def gcp_status(): return {}                    # type: ignore[misc]

# On startup: pull the database from GCS if configured.
# This runs once at import time so both the UI and the scraper engine
# always start with the latest persisted data from the cloud.
if _GCP_AVAILABLE and is_gcs_configured():
    _pulled = sync_db_from_gcs(DB_PATH)
    if _pulled:
        _logger.info("db_manager: DB restored from GCS on startup → %s", DB_PATH)
    else:
        _logger.info("db_manager: GCS configured but no remote DB yet — starting fresh locally")


def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date():
    return datetime.now().strftime("%Y-%m-%d")


# In-memory product cache: persists scraped rows for the container lifetime.
# On Cloud Run without GCS, the SQLite file resets on every container restart.
# This cache lets the session survive short restarts and prevents re-reads
# from a blank DB right after a scrape that wrote to GCS but whose sync
# cooldown hasn't expired yet.
_IN_MEMORY_PRODUCTS: dict[str, list] = {}  # {domain: [row_dicts]}
_IN_MEMORY_LOCK = __import__("threading").Lock()


def trigger_gcs_sync(force: bool = False) -> bool:
    """
    Manually trigger a GCS upload of the SQLite DB.
    Called after high-value writes (bulk scrape results, job completion).
    Throttled internally — safe to call frequently.
    Returns True if an upload occurred.
    """
    if _GCP_AVAILABLE and is_gcs_configured():
        return sync_db_to_gcs(DB_PATH, force=force)
    return False


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    # WAL: يسمح بالقراءة والكتابة المتزامنة من threads مختلفة بدون تعارض
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")  # 30 ثانية انتظار بدل الخطأ الفوري
    # ⚡ ضبط أداء لكل اتصال (آمن تماماً — إعدادات اتصال محلية، لا تغيّر البيانات):
    #   • cache_size سالب = حجم بالكيلوبايت → 64MB صفحات مخبّأة بدل ~2MB الافتراضي
    #   • mmap_size = قراءة عبر الذاكرة (I/O أسرع للقاعدة الكبيرة 220MB)
    #   • temp_store=MEMORY = جداول الفرز/التجميع المؤقتة في الذاكرة (GROUP BY/ORDER BY أسرع)
    # كل واحدة داخل try مستقل: لو فشلت أي PRAGMA (نظام قديم) يبقى الاتصال صالحاً.
    for _pragma in (
        "PRAGMA cache_size=-65536;",
        "PRAGMA mmap_size=268435456;",
        "PRAGMA temp_store=MEMORY;",
    ):
        try:
            conn.execute(_pragma)
        except Exception:
            pass
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    # أحداث عامة
    c.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, page TEXT,
        event_type TEXT, details TEXT,
        product_name TEXT, action_taken TEXT
    )""")

    # قرارات المستخدم (موافق/تأجيل/إزالة)
    c.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, product_name TEXT,
        our_price REAL, comp_price REAL,
        diff REAL, competitor TEXT,
        old_status TEXT, new_status TEXT,
        reason TEXT, decided_by TEXT DEFAULT 'user'
    )""")

    # تاريخ الأسعار لكل منتج عند كل منافس
    c.execute("""CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, product_name TEXT,
        competitor TEXT, price REAL,
        our_price REAL, diff REAL,
        match_score REAL, decision TEXT,
        product_id TEXT DEFAULT ''
    )""")

    # نقطة الاستئناف للمعالجة الخلفية
    c.execute("""CREATE TABLE IF NOT EXISTS job_progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT UNIQUE,
        started_at TEXT, updated_at TEXT,
        status TEXT DEFAULT 'running',
        total INTEGER DEFAULT 0,
        processed INTEGER DEFAULT 0,
        results_json TEXT DEFAULT '[]',
        missing_json TEXT DEFAULT '[]',
        audit_json TEXT DEFAULT '{}',
        our_file TEXT, comp_files TEXT
    )""")
    # إضافة عمود missing_json إذا لم يكن موجوداً (للتوافق مع قواعد البيانات القديمة)
    try:
        c.execute("ALTER TABLE job_progress ADD COLUMN missing_json TEXT DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass  # العمود موجود بالفعل
    try:
        c.execute("ALTER TABLE job_progress ADD COLUMN audit_json TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass

    # تاريخ التحليلات
    c.execute("""CREATE TABLE IF NOT EXISTS analysis_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, our_file TEXT,
        comp_file TEXT, total_products INTEGER,
        matched INTEGER, missing INTEGER, summary TEXT
    )""")

    # AI cache
    c.execute("""CREATE TABLE IF NOT EXISTS ai_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, prompt_hash TEXT UNIQUE,
        response TEXT, source TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS hidden_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        product_key TEXT UNIQUE,
        product_name TEXT,
        action TEXT DEFAULT 'hidden'
    )""")

    # ─── Single Source of Truth: product_state ─────────────────────────────
    # كل منتج له حالة واحدة فقط في أي لحظة. مفتاح الهوية ثابت (product_key).
    # status ∈ {NEW, MISSING, REVIEW, DUPLICATE, MATCHED, DONE, NEEDS_ATTENTION}
    c.execute("""CREATE TABLE IF NOT EXISTS product_state (
        product_key TEXT PRIMARY KEY,
        product_name TEXT,
        store TEXT,
        url TEXT,
        status TEXT NOT NULL DEFAULT 'NEW',
        confidence REAL DEFAULT 0,
        duplicate_of TEXT,
        last_decision_by TEXT DEFAULT 'auto',
        payload_json TEXT DEFAULT '{}',
        created_at TEXT,
        updated_at TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pstate_status ON product_state(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pstate_updated ON product_state(updated_at)")

    # ─── Audit Log: every status transition is recorded (Undo support) ─────
    c.execute("""CREATE TABLE IF NOT EXISTS product_transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_key TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        changed_by TEXT,
        reason TEXT,
        timestamp TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ptrans_key ON product_transitions(product_key)")

    # ─── Phase 0: competitor intake ledger ─────────────────────────────────
    # Enforces "no product lost without a terminal state" for all matching
    # pipelines. See observability/ledger.py for the state vocabulary.
    c.execute("""CREATE TABLE IF NOT EXISTS competitor_intake_ledger (
        run_id        TEXT NOT NULL,
        comp_id       TEXT NOT NULL,
        competitor    TEXT NOT NULL,
        product_name  TEXT,
        raw_payload   TEXT DEFAULT '{}',
        state         TEXT NOT NULL DEFAULT 'INGESTED',
        reason_code   TEXT,
        last_score    REAL,
        error_class   TEXT,
        error_excerpt TEXT,
        ingested_at   TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (run_id, comp_id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cil_state ON competitor_intake_ledger(state)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cil_run   ON competitor_intake_ledger(run_id)")

    conn.commit()
    conn.close()
    # فهارس الأداء (idempotent) — تسرّع المطابقة/العرض/الترقيم على مستوى SQL
    ensure_indexes()


def ensure_indexes() -> None:
    """ينشئ فهارس الأداء إن لم توجد (P2 — competitor/brand/price/norm_name).

    آمن وidempotent: لا يفشل إذا غاب جدول. يسرّع reconcile (بحث norm_name)،
    استعلامات CompetitorIntelligence، والترقيم SQL (LIMIT/OFFSET) على الجداول الكبيرة.
    """
    _index_specs = [
        # competitor_products_store (مخزن المنافسين الحيّ)
        ("competitor_products_store", "idx_cps_competitor2", "competitor"),
        ("competitor_products_store", "idx_cps_brand2",      "brand"),
        ("competitor_products_store", "idx_cps_price2",      "price"),
        ("competitor_products_store", "idx_cps_norm2",       "norm_name"),
        # comp_catalog (الكتالوج التراكمي) — norm_name كان بلا فهرس (مسح كامل)
        ("comp_catalog", "idx_compcat_norm",  "norm_name"),
        ("comp_catalog", "idx_compcat_comp",  "competitor"),
        ("comp_catalog", "idx_compcat_price", "price"),
        # our_catalog
        ("our_catalog", "idx_ourcat_norm", "norm_name"),
    ]
    # فهرس مركّب مفيد فعلاً: (competitor, price) يخدم get_all_competitor_products
    # (WHERE competitor=? ORDER BY price DESC) — يفلتر ويُرتّب من الفهرس بلا فرز كامل.
    # ملاحظة: (competitor, norm_name) لا يُضاف لأن قيد UNIQUE(competitor,norm_name)
    # ينشئ أصلاً sqlite_autoindex يغطّيه تماماً — أي فهرس إضافي عليه مكرّر بلا فائدة.
    _composite_index_specs = [
        ("competitor_products_store", "idx_cps_comp_price", "competitor, price"),
    ]
    # فهارس مكرّرة أُنشئت في نسخ سابقة — تُسقَط للتنظيف (آمن وidempotent).
    # L2: 8 فهارس أحادية مكرّرة على competitor_products_store تُسقَط (نفس أعمدة
    # فهارس مُبقاة): competitor/brand/price يغطّيها idx_cps_*2 (+ المركّب)،
    # norm_name←idx_cps_norm2، first_seen←idx_cps_first_seen. الحذف لا يغيّر أي
    # خطة استعلام (المخطِّط يختار فهرساً واحداً أصلاً) ويخفّف كلفة الإدخال.
    _redundant_indexes = (
        "idx_cps_comp_norm", "idx_compcat_comp_norm",
        # ثلاثيات competitor/brand/price (نُبقي idx_cps_*2 + idx_cps_comp_price)
        "idx_cps_competitor", "idx_cps_brand", "idx_cps_price",
        # عائلة idx_ci_* كاملة (يغطّيها idx_cps_*2 / idx_cps_norm2 / idx_cps_first_seen)
        "idx_ci_comp", "idx_ci_brand", "idx_ci_price", "idx_ci_norm", "idx_ci_first",
    )
    try:
        conn = get_db()
        c = conn.cursor()
        for tbl, idx, col in _index_specs + _composite_index_specs:
            try:
                c.execute(f'CREATE INDEX IF NOT EXISTS {idx} ON {tbl}({col})')
            except Exception:
                pass  # جدول/عمود غير موجود في هذه القاعدة — تجاهل بأمان
        for idx in _redundant_indexes:
            try:
                c.execute(f'DROP INDEX IF EXISTS {idx}')
            except Exception:
                pass
        conn.commit()
        conn.close()
    except Exception:
        pass


# ─── Single Source of Truth API ────────────────────────────────────────────
def upsert_product_state(product_key, name="", store="", url="",
                         status="NEW", confidence=0.0, duplicate_of=None,
                         decided_by="auto", payload=None, reason=""):
    """
    Atomic upsert. If status changes, logs a transition for full audit + undo.
    Returns the new status.
    """
    import json as _json
    payload_json = _json.dumps(payload or {}, ensure_ascii=False)
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT status FROM product_state WHERE product_key=?", (product_key,))
        row = c.fetchone()
        old_status = row["status"] if row else None
        now = _ts()
        if row is None:
            c.execute("""INSERT INTO product_state
                (product_key, product_name, store, url, status, confidence,
                 duplicate_of, last_decision_by, payload_json, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (product_key, name, store, url, status, confidence,
                 duplicate_of, decided_by, payload_json, now, now))
        else:
            c.execute("""UPDATE product_state SET
                product_name=COALESCE(NULLIF(?, ''), product_name),
                store=COALESCE(NULLIF(?, ''), store),
                url=COALESCE(NULLIF(?, ''), url),
                status=?, confidence=?, duplicate_of=?,
                last_decision_by=?, payload_json=?, updated_at=?
                WHERE product_key=?""",
                (name, store, url, status, confidence, duplicate_of,
                 decided_by, payload_json, now, product_key))
        if old_status != status:
            c.execute("""INSERT INTO product_transitions
                (product_key, from_status, to_status, changed_by, reason, timestamp)
                VALUES (?,?,?,?,?,?)""",
                (product_key, old_status, status, decided_by, reason, now))
        conn.commit()
        return status
    finally:
        conn.close()


def get_product_state(product_key):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM product_state WHERE product_key=?",
                           (product_key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_products_by_status(status, limit=1000):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM product_state WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (status, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def undo_last_transition(product_key, decided_by="user"):
    """Revert the product to its previous status. Returns the restored status, or None."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT from_status FROM product_transitions
               WHERE product_key=? AND from_status IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (product_key,)
        ).fetchone()
        if not row or not row["from_status"]:
            return None
        prev = row["from_status"]
        now = _ts()
        cur = conn.execute("SELECT status FROM product_state WHERE product_key=?",
                           (product_key,)).fetchone()
        cur_status = cur["status"] if cur else None
        conn.execute("UPDATE product_state SET status=?, last_decision_by=?, updated_at=? WHERE product_key=?",
                     (prev, decided_by, now, product_key))
        conn.execute("""INSERT INTO product_transitions
            (product_key, from_status, to_status, changed_by, reason, timestamp)
            VALUES (?,?,?,?,?,?)""",
            (product_key, cur_status, prev, decided_by, "undo", now))
        conn.commit()
        return prev
    finally:
        conn.close()


def get_transitions(product_key, limit=20):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM product_transitions WHERE product_key=? ORDER BY id DESC LIMIT ?",
            (product_key, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stale_products(hours=24, status="NEW"):
    """Products stuck in a status longer than `hours` — for the safety-net sweep."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM product_state WHERE status=? AND updated_at < ?",
            (status, cutoff)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def state_health_counts():
    """Returns counts per status — for the health panel."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM product_state GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
    finally:
        conn.close()


# ─── أحداث ────────────────────────────────
def log_event(page, event_type, details="", product_name="", action=""):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO events (timestamp,page,event_type,details,product_name,action_taken) VALUES (?,?,?,?,?,?)",
            (_ts(), page, event_type, details, product_name, action)
        )
        conn.commit(); conn.close()
    except Exception: pass


# ─── قرارات ────────────────────────────────
def log_decision(product_name, old_status, new_status, reason="",
                 our_price=0, comp_price=0, diff=0, competitor=""):
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO decisions
               (timestamp,product_name,our_price,comp_price,diff,competitor,
                old_status,new_status,reason)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_ts(), product_name, our_price, comp_price, diff,
             competitor, old_status, new_status, reason)
        )
        conn.commit(); conn.close()
    except Exception: pass


def get_decisions(product_name=None, status=None, limit=100):
    try:
        conn = get_db()
        if product_name:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE product_name LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{product_name}%", limit)
            ).fetchall()
        elif status:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE new_status=? ORDER BY id DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception: return []


# ─── تاريخ الأسعار (الميزة الذكية) ──────────
def upsert_price_history(product_name, competitor, price,
                          our_price=0, diff=0, match_score=0,
                          decision="", product_id=""):
    """
    يحفظ السعر اليوم. إذا وُجد سعر سابق لنفس المنتج/المنافس اليوم → يحدّثه.
    إذا كان أمس → يضيف سجلاً جديداً لتتبع التغيير.
    يرجع True إذا تغير السعر عن آخر تسجيل.
    """
    conn = get_db()
    today = _date()

    # آخر سعر مسجل لهذا المنتج/المنافس
    last = conn.execute(
        """SELECT price, date FROM price_history
           WHERE product_name=? AND competitor=?
           ORDER BY id DESC LIMIT 1""",
        (product_name, competitor)
    ).fetchone()

    price_changed = False
    if last:
        last_price = last["price"]
        last_date  = last["date"]
        price_changed = abs(float(price) - float(last_price)) > 0.01

        if last_date == today:
            # نفس اليوم → حدّث فقط
            conn.execute(
                """UPDATE price_history SET price=?,our_price=?,diff=?,
                   match_score=?,decision=?,product_id=?
                   WHERE product_name=? AND competitor=? AND date=?""",
                (price, our_price, diff, match_score, decision,
                 product_id, product_name, competitor, today)
            )
        else:
            # يوم جديد → أضف سجل
            conn.execute(
                """INSERT INTO price_history
                   (date,product_name,competitor,price,our_price,diff,
                    match_score,decision,product_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (today, product_name, competitor, price, our_price,
                 diff, match_score, decision, product_id)
            )
    else:
        # أول مرة
        conn.execute(
            """INSERT INTO price_history
               (date,product_name,competitor,price,our_price,diff,
                match_score,decision,product_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (today, product_name, competitor, price, our_price,
             diff, match_score, decision, product_id)
        )

    conn.commit(); conn.close()
    return price_changed


def get_price_history(product_name, competitor="", limit=30):
    try:
        conn = get_db()
        if competitor:
            rows = conn.execute(
                """SELECT * FROM price_history
                   WHERE product_name=? AND competitor=?
                   ORDER BY date DESC LIMIT ?""",
                (product_name, competitor, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM price_history WHERE product_name=?
                   ORDER BY date DESC LIMIT ?""",
                (product_name, limit)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception: return []


def get_price_changes(days=7):
    """منتجات تغير سعرها خلال X يوم"""
    try:
        conn = get_db()
        rows = conn.execute(
            """SELECT p1.product_name, p1.competitor,
                      p1.price as new_price, p2.price as old_price,
                      p1.date as new_date, p2.date as old_date,
                      (p1.price - p2.price) as price_diff
               FROM price_history p1
               JOIN price_history p2
                 ON p1.product_name=p2.product_name
                AND p1.competitor=p2.competitor
                AND p1.id > p2.id
               WHERE p1.date >= date('now', ?)
                 AND abs(p1.price - p2.price) > 0.01
               ORDER BY abs(p1.price - p2.price) DESC
               LIMIT 100""",
            (f"-{days} days",)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception: return []


# ─── المعالجة الخلفية ──────────────────────
def save_job_progress(job_id, total, processed, results, status="running",
                      our_file="", comp_files="", missing=None, audit_stats=None):
    missing_data = json.dumps(missing if missing else [], ensure_ascii=False, default=str)
    results_data = json.dumps(results, ensure_ascii=False, default=str)
    audit_data = json.dumps(audit_stats if audit_stats is not None else {},
                            ensure_ascii=False, default=str)
    with sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute(
            """INSERT OR REPLACE INTO job_progress
               (job_id,started_at,updated_at,status,total,processed,
                results_json,missing_json,our_file,comp_files,audit_json)
               VALUES (?,
                   COALESCE((SELECT started_at FROM job_progress WHERE job_id=?), ?),
                   ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, job_id, _ts(), _ts(), status, total, processed,
             results_data, missing_data, our_file, comp_files, audit_data)
        )
        conn.commit()
    # Push to GCS when a job completes — captures full analysis results
    if status in ("done", "completed", "finished"):
        trigger_gcs_sync(force=True)


def get_job_progress(job_id, light=False):
    """يجلب تقدّم الوظيفة.

    light=True: وضع خفيف — يقرأ فقط (job_id, status, total, processed) بدون
    عمودي results_json/missing_json (≈71MB) ودون json.loads. استخدمه في **كل**
    فحوص الحالة/التقدم التي تعمل في كل rerun. حمّل النتائج الكاملة (light=False)
    مرة واحدة فقط عند التطبيق الأول واحفظها في session_state — لا تُعد تحليلها.
    """
    try:
        conn = get_db()
        if light:
            row = conn.execute(
                "SELECT job_id, status, total, processed "
                "FROM job_progress WHERE job_id=?", (job_id,)
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        row = conn.execute(
            "SELECT * FROM job_progress WHERE job_id=?", (job_id,)
        ).fetchone()
        conn.close()
        if row:
            d = dict(row)
            try: d["results"] = json.loads(d.get("results_json", "[]"))
            except Exception: d["results"] = []
            try: d["missing"] = json.loads(d.get("missing_json", "[]"))
            except Exception: d["missing"] = []
            try: d["audit"] = json.loads(d.get("audit_json") or "{}")
            except Exception: d["audit"] = {}
            return d
    except Exception: pass
    return None


def any_running_job(stale_after_seconds: int = 3600):
    """
    Returns a dict with {job_id, processed, total, updated_at} for the most
    recent analysis job whose status == 'running' AND whose updated_at is
    within ``stale_after_seconds``. Returns None if no fresh running job.
    Acts as a DB-level mutex to prevent duplicate analysis starts across
    concurrent clicks, reruns, or replicas.

    NOTE: Sitemap scraper jobs (job_id starts with 'sitemap_auto_') are
    excluded — they should NOT block the analysis button.
    """
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT job_id, processed, total, updated_at, started_at "
            "FROM job_progress WHERE status='running' "
            "AND job_id NOT LIKE 'sitemap_auto_%' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        try:
            from datetime import datetime as _dt
            _upd = d.get("updated_at") or d.get("started_at") or ""
            if _upd:
                t = _dt.strptime(str(_upd)[:19], "%Y-%m-%d %H:%M:%S")
                if (_dt.now() - t).total_seconds() > stale_after_seconds:
                    return None  # stale -> treat as no running job
        except Exception:
            pass
        return d
    except Exception:
        return None


def release_stale_running_jobs(stale_after_seconds: int = 3600) -> int:
    """Marks stuck 'running' rows (not updated recently) so the UI mutex does
    not deadlock. Returns number of rows updated.

    ⚠️ السبب الجذري: لو اكتملت المعالجة فعلاً (total=processed>0) لكن أعادت
    الحاوية التشغيل قبل حفظ 'done'، نُسجّلها 'done' (لا 'stopped') كي لا نضيّع
    تحليلاً مكتملاً. الوظائف غير المكتملة فقط تُسجَّل 'stopped'.
    """
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000;")
            _stale = (
                "status='running' AND "
                "(strftime('%s','now') - strftime('%s', COALESCE(updated_at, started_at))) > ?"
            )
            # اكتمل فعلاً → done
            cur_done = conn.execute(
                f"UPDATE job_progress SET status='done' "
                f"WHERE {_stale} AND total = processed AND processed > 0",
                (int(stale_after_seconds),),
            )
            # لم يكتمل → stopped
            cur_stop = conn.execute(
                f"UPDATE job_progress SET status='stopped' WHERE {_stale}",
                (int(stale_after_seconds),),
            )
            conn.commit()
            return (cur_done.rowcount or 0) + (cur_stop.rowcount or 0)
    except Exception:
        return 0


def get_last_job():
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM job_progress ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            d = dict(row)
            try: d["results"] = json.loads(d.get("results_json", "[]"))
            except Exception: d["results"] = []
            try: d["missing"] = json.loads(d.get("missing_json", "[]"))
            except Exception: d["missing"] = []
            try: d["audit"] = json.loads(d.get("audit_json") or "{}")
            except Exception: d["audit"] = {}
            return d
    except Exception: pass
    return None


# ─── سجل التحليلات ─────────────────────────
def log_analysis(our_file, comp_file, total, matched, missing, summary=""):
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO analysis_history
               (timestamp,our_file,comp_file,total_products,matched,missing,summary)
               VALUES (?,?,?,?,?,?,?)""",
            (_ts(), our_file, comp_file, total, matched, missing, summary)
        )
        conn.commit(); conn.close()
    except Exception: pass


def get_analysis_history(limit=20):
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM analysis_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception: return []


def get_events(page=None, limit=50):
    try:
        conn = get_db()
        if page:
            rows = conn.execute(
                "SELECT * FROM events WHERE page=? ORDER BY id DESC LIMIT ?",
                (page, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception: return []


# ── دوال المنتجات المخفية الدائمة ──────────────────────
def save_hidden_product(product_key: str, product_name: str = "", action: str = "hidden"):
    """يحفظ منتجاً مخفياً في قاعدة البيانات بشكل دائم"""
    try:
        conn = get_db()
        conn.execute(
            """INSERT OR REPLACE INTO hidden_products
               (timestamp, product_key, product_name, action)
               VALUES (?, ?, ?, ?)""",
            (_ts(), product_key, product_name, action)
        )
        conn.commit()
        conn.close()
    except Exception as _e:
        _logger.debug("save_hidden_product error: %s", _e)

def get_hidden_product_keys() -> set:
    """يُرجع مجموعة كل مفاتيح المنتجات المخفية من قاعدة البيانات"""
    try:
        conn = get_db()
        rows = conn.execute("SELECT product_key FROM hidden_products").fetchall()
        conn.close()
        return {r["product_key"] for r in rows}
    except Exception as _e:
        _logger.debug("get_hidden_product_keys error: %s", _e)
        return set()


# ── Task 3.3 — Soft Delete System ─────────────────────────────────────────────

def soft_delete_product(product_key: str, product_name: str = "") -> None:
    """
    Soft-delete: persists the product in hidden_products with action='soft_deleted'.
    Uses the stable key format  "softdel_{product_name}"  so the record survives
    across sessions and page/filter changes (unlike the fragile idx-based legacy key).
    Thread-safe — delegates to save_hidden_product which uses WAL-mode SQLite.
    """
    save_hidden_product(product_key, product_name, action="soft_deleted")


def get_soft_deleted_product_keys() -> set:
    """
    Returns the set of product_keys that were soft-deleted (action='soft_deleted').
    Called once at the top of render_pro_table() to filter before rendering.
    """
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT product_key FROM hidden_products WHERE action='soft_deleted'"
        ).fetchall()
        conn.close()
        return {r["product_key"] for r in rows}
    except Exception as _e:
        _logger.debug("get_soft_deleted_product_keys error: %s", _e)
        return set()


def restore_soft_deleted_product(product_key: str) -> bool:
    """
    Undo soft-delete: removes the row from hidden_products.
    Called by the Recycle Bin (Task 3.4) restore button.
    Returns True on success, False on failure.
    """
    try:
        conn = get_db()
        conn.execute(
            "DELETE FROM hidden_products WHERE product_key=? AND action='soft_deleted'",
            (product_key,),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        _logger.debug("restore_soft_deleted_product error: %s", _e)
        return False


def ensure_is_deleted_column(df: "pd.DataFrame") -> "pd.DataFrame":  # type: ignore[name-defined]
    """
    Guarantees df has an 'is_deleted' bool column.
    Returns the SAME df if the column already exists (no copy overhead).
    Returns a copy with the column added as False if it was missing.
    """
    if "is_deleted" not in df.columns:
        df = df.copy()
        df["is_deleted"] = False
    return df


def apply_soft_deletes_to_df(
    df: "pd.DataFrame",  # type: ignore[name-defined]
    prefix: str = "",
) -> "pd.DataFrame":  # type: ignore[name-defined]
    """
    Hydrates the 'is_deleted' column from the DB:
      - Fetches the set of soft-deleted keys (action='soft_deleted').
      - Marks rows whose stable key  "softdel_{product_name}"  is in the set.
    Stable key is based on 'المنتج' column value — not on positional idx.

    Args:
        df      : analysis results DataFrame (must have 'المنتج' column).
        prefix  : optional section prefix; currently unused but kept for future
                  per-section isolation (e.g. "raise", "lower").

    Returns:
        df with 'is_deleted' column populated (True = soft-deleted, False = visible).
    """
    df = ensure_is_deleted_column(df)
    if df.empty:
        return df

    _deleted_keys = get_soft_deleted_product_keys()
    if not _deleted_keys:
        return df  # fast-path: nothing deleted yet

    _name_col = "المنتج" if "المنتج" in df.columns else None
    if _name_col is None:
        return df  # no product-name column — cannot match

    def _check_deleted(row) -> bool:
        pname = str(row.get(_name_col, "") or "")
        return f"softdel_{pname}" in _deleted_keys

    df["is_deleted"] = df.apply(_check_deleted, axis=1)
    return df


# ── Task 3.5 & 3.6 — Product Overrides + Force Links ──────────────────────────

def init_db_v35() -> None:
    """
    Create product_overrides and force_links tables (idempotent).
    Called at module load right after init_db().
    """
    conn = get_db()
    c = conn.cursor()
    # Task 3.5 — inline-edit overrides
    c.execute("""CREATE TABLE IF NOT EXISTS product_overrides (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        stable_key TEXT    UNIQUE NOT NULL,
        new_name   TEXT    DEFAULT '',
        new_price  REAL    DEFAULT 0,
        new_url    TEXT    DEFAULT '',
        updated_at TEXT
    )""")
    # Task 3.6 — manual force-links (source='manual')
    c.execute("""CREATE TABLE IF NOT EXISTS force_links (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        our_id     TEXT    DEFAULT '',
        our_name   TEXT    DEFAULT '',
        comp_url   TEXT    NOT NULL,
        source     TEXT    DEFAULT 'manual',
        created_at TEXT,
        UNIQUE(our_id, comp_url)
    )""")
    conn.commit()
    conn.close()


# ─── Task 3.5: Inline Edit ────────────────────────────────────────────────────

def update_product_data(
    stable_key: str,
    new_name: str = "",
    new_price: float = 0.0,
    new_url: str = "",
) -> bool:
    """
    Upsert a product override into product_overrides.
    stable_key format: 'edit_{product_name}' (mirrors soft-delete key convention).
    Returns True on success.
    """
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO product_overrides
                   (stable_key, new_name, new_price, new_url, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(stable_key) DO UPDATE SET
                   new_name   = excluded.new_name,
                   new_price  = excluded.new_price,
                   new_url    = excluded.new_url,
                   updated_at = excluded.updated_at""",
            (
                str(stable_key).strip(),
                str(new_name or "").strip(),
                float(new_price or 0),
                str(new_url or "").strip(),
                _ts(),
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        _logger.debug("update_product_data error: %s", _e)
        return False


def get_product_overrides() -> dict:
    """
    Returns {stable_key: {new_name, new_price, new_url}} for every override row.
    Called once at the top of render_pro_table() — O(1) DB round-trip.
    """
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT stable_key, new_name, new_price, new_url FROM product_overrides"
        ).fetchall()
        conn.close()
        return {
            r["stable_key"]: {
                "new_name":  r["new_name"],
                "new_price": r["new_price"],
                "new_url":   r["new_url"],
            }
            for r in rows
        }
    except Exception as _e:
        _logger.debug("get_product_overrides error: %s", _e)
        return {}


def delete_product_override(stable_key: str) -> bool:
    """Remove an override row — resets product to its original scraped values."""
    try:
        conn = get_db()
        conn.execute(
            "DELETE FROM product_overrides WHERE stable_key=?", (stable_key,)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        _logger.debug("delete_product_override error: %s", _e)
        return False


# ─── Task 3.6: Force Link ─────────────────────────────────────────────────────

def force_link_product(our_id: str, our_name: str, comp_url: str) -> bool:
    """
    Write a manual competitor match into force_links with source='manual'.
    UNIQUE(our_id, comp_url) prevents duplicate entries.
    Returns True on success.
    """
    try:
        conn = get_db()
        conn.execute(
            """INSERT OR REPLACE INTO force_links
                   (our_id, our_name, comp_url, source, created_at)
               VALUES (?, ?, ?, 'manual', ?)""",
            (
                str(our_id or "").strip(),
                str(our_name or "").strip(),
                str(comp_url or "").strip(),
                _ts(),
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        _logger.debug("force_link_product error: %s", _e)
        return False


def get_force_links() -> list:
    """
    Returns all force-linked rows newest-first.
    Each row: {our_id, our_name, comp_url, source, created_at}.
    """
    try:
        conn = get_db()
        rows = conn.execute(
            """SELECT our_id, our_name, comp_url, source, created_at
               FROM force_links
               ORDER BY id DESC"""
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as _e:
        _logger.debug("get_force_links error: %s", _e)
        return []


def delete_force_link(our_id: str, comp_url: str) -> bool:
    """Remove a force link by (our_id, comp_url) pair."""
    try:
        conn = get_db()
        conn.execute(
            "DELETE FROM force_links WHERE our_id=? AND comp_url=?",
            (str(our_id or "").strip(), str(comp_url or "").strip()),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        _logger.debug("delete_force_link error: %s", _e)
        return False


# ── module-level initialisation ───────────────────────────────────────────────
init_db()
init_db_v35()

# Start background GCS sync thread (uploads DB to GCS every 5 minutes).
# Only activates when GCS_BUCKET_NAME env var is set.
if _GCP_AVAILABLE and is_gcs_configured():
    schedule_background_gcs_sync(DB_PATH, interval_secs=300)
    _logger.info("db_manager: GCS background sync active for %s", DB_PATH)

# Bootstrap: register canonical competitors list (18 stores) at first run.
# Safe to call every time — uses INSERT OR IGNORE internally.
try:
    migrate_db_v26()
    _n_registered = register_competitors_from_json()
    if _n_registered:
        _logger.info("db_manager: %d competitors registered from JSON", _n_registered)
except Exception as _boot_exc:
    _logger.debug("db_manager: competitor bootstrap skipped: %s", _boot_exc)


# ═══════════════════════════════════════════════════════════════
#  v26 — Upsert Catalog + Processed Products
# ═══════════════════════════════════════════════════════════════

def init_db_v26(conn=None):
    """إضافة جداول v26 للـ upsert ومتابعة المنتجات المعالجة"""
    c_conn = conn or get_db()
    cur = c_conn.cursor()

    # كتالوج مؤقت للمنافسين (يُحدَّث يومياً)
    cur.execute("""CREATE TABLE IF NOT EXISTS comp_catalog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        competitor TEXT NOT NULL,
        product_name TEXT NOT NULL,
        norm_name TEXT,
        price REAL,
        first_seen TEXT,
        last_seen TEXT,
        UNIQUE(competitor, norm_name)
    )""")

    # كتالوج متجرنا (يُحدَّث يومياً)
    cur.execute("""CREATE TABLE IF NOT EXISTS our_catalog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id TEXT UNIQUE,
        product_name TEXT NOT NULL,
        norm_name TEXT,
        price REAL,
        first_seen TEXT,
        last_seen TEXT
    )""")

    # المنتجات المعالجة (ترحيل/تسعير/إضافة)
    cur.execute("""CREATE TABLE IF NOT EXISTS processed_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        product_key TEXT UNIQUE,
        product_name TEXT,
        competitor TEXT,
        action TEXT,
        old_price REAL,
        new_price REAL,
        product_id TEXT,
        notes TEXT
    )""")

    c_conn.commit()
    if not conn:
        c_conn.close()


def upsert_our_catalog(our_df, name_col="اسم المنتج", id_col="رقم المنتج", price_col="السعر"):
    """يُحدِّث كتالوج متجرنا عند كل رفع جديد — بدون تكرار"""
    import re
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    rows_updated = 0
    rows_inserted = 0

    for _, row in our_df.iterrows():
        name = str(row.get(name_col, "")).strip()
        if not name:
            continue
        norm = re.sub(r'\s+', ' ', name.lower().strip())
        pid  = str(row.get(id_col, "")).strip().rstrip(".0")
        try:
            price = float(str(row.get(price_col, 0)).replace(",", ""))
        except Exception:
            price = 0.0

        existing = conn.execute(
            "SELECT id, price FROM our_catalog WHERE product_id=? OR norm_name=?",
            (pid, norm)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE our_catalog SET price=?, last_seen=?, norm_name=? WHERE id=?",
                (price, today, norm, existing[0])
            )
            rows_updated += 1
        else:
            conn.execute(
                """INSERT INTO our_catalog (product_id, product_name, norm_name, price, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?)""",
                (pid, name, norm, price, today, today)
            )
            rows_inserted += 1

    conn.commit()
    conn.close()
    return {"updated": rows_updated, "inserted": rows_inserted}


def _comp_catalog_product_key(competitor: str, norm_name: str) -> str:
    """مفتاح مستقر لصف المنافس (يتوافق مع عمود comp_product_key إن وُجد)."""
    n = (norm_name or "").strip()
    c = (competitor or "").strip() or "unknown"
    if n:
        return f"{c}::{n}"
    h = hashlib.md5(f"{c}\0{n}".encode("utf-8")).hexdigest()[:16]
    return f"{c}::__{h}"


def _pragma_column_names(conn, table: str):
    """أسماء أعمدة جدول — متوافق مع sqlite3.Row (لا تعتمد على row[1] فقط)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            out.append(str(r["name"]))
        except (KeyError, IndexError, TypeError):
            try:
                out.append(str(r[1]))
            except Exception:
                continue
    return out


def _resolve_comp_name_price_columns(cdf):
    """
    يفضّل أعمدة apply_user_column_map القياسية (المنتج، سعر المنتج) ثم يعود للتخمين.
    """
    cols = list(cdf.columns)
    cs = set(cols)

    if "المنتج" in cs:
        name_col = "المنتج"
    elif "اسم المنتج" in cs:
        name_col = "اسم المنتج"
    else:
        name_col = None
        price_col = None
        for c in cols:
            sample = str(cdf[c].dropna().iloc[0]) if not cdf[c].dropna().empty else ""
            try:
                float(sample.replace(",", ""))
                if price_col is None:
                    price_col = c
            except Exception:
                if name_col is None and len(sample) > 5:
                    name_col = c
        if name_col is None:
            name_col = cols[0]
        if price_col is None:
            price_col = cols[1] if len(cols) > 1 else cols[0]
        return name_col, price_col

    if "سعر المنتج" in cs:
        price_col = "سعر المنتج"
    elif "السعر" in cs:
        price_col = "السعر"
    elif "سعر" in cs:
        price_col = "سعر"
    else:
        price_col = None
        for c in cols:
            if c == name_col:
                continue
            sample = str(cdf[c].dropna().iloc[0]) if not cdf[c].dropna().empty else ""
            try:
                float(str(sample).replace(",", ""))
                price_col = c
                break
            except Exception:
                continue
        if price_col is None:
            price_col = cols[1] if len(cols) > 1 else cols[0]

    return name_col, price_col


def upsert_comp_catalog(comp_dfs: dict):
    """يُحدِّث كتالوج المنافسين عند كل رفع جديد — بدون تكرار.

    ⚡ مسار سريع مُجمّع (تحميل مسبق واحد + executemany) بدل SELECT+write لكل صف.
    ملاذ آمن: عند أي IntegrityError نادر (ترحيل عمود comp_product_key أو سباق
    تزامن) نُلغي العمل ونعود إلى المسار الأصلي صف-بصف المُثبَت — فالنتيجة مطابقة
    تماماً في كل الحالات الحرجة.
    """
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    _cc_cols = _pragma_column_names(conn, "comp_catalog")
    _has_cpk = any(c.lower() == "comp_product_key" for c in _cc_cols)
    try:
        try:
            res = _upsert_comp_catalog_batched(conn, comp_dfs, today, _has_cpk)
            conn.commit()
            return res
        except sqlite3.IntegrityError:
            # حالة نادرة (ترحيل/تزامن) → تراجع كامل ثم المسار الأصلي الآمن
            try:
                conn.rollback()
            except Exception:
                pass
            res = _upsert_comp_catalog_rowwise(conn, comp_dfs, today, _has_cpk)
            conn.commit()
            return res
    finally:
        conn.close()


def _upsert_comp_catalog_batched(conn, comp_dfs: dict, today: str, _has_cpk: bool):
    """المسار السريع: يحمّل مفاتيح المنافسين الموجودة مرة واحدة، ثم يُراكم
    الإدراج/التحديث وينفّذهما بـ executemany. لا commit هنا (المُوزِّع يتكفّل)."""
    import re
    total_new = 0
    rows_updated = 0

    # تحميل مسبق: (competitor, norm_name) → id لكل المنافسين في هذه الدفعة.
    _competitors = list(comp_dfs.keys())
    _existing_map: dict = {}
    _CHUNK = 500  # تفادي حد متغيرات SQLite (999) عبر التقطيع
    for _i in range(0, len(_competitors), _CHUNK):
        _part = _competitors[_i:_i + _CHUNK]
        if not _part:
            continue
        _ph = ",".join("?" * len(_part))
        for _r in conn.execute(
            f"SELECT id, competitor, norm_name FROM comp_catalog WHERE competitor IN ({_ph})",
            _part,
        ):
            _existing_map[(_r["competitor"], _r["norm_name"])] = _r["id"]

    _insert_params: list = []
    _update_params: list = []
    _pending_insert: dict = {}  # (competitor, norm) → موضع في _insert_params

    for cname, cdf in comp_dfs.items():
        name_col, price_col = _resolve_comp_name_price_columns(cdf)
        for _, row in cdf.iterrows():
            name = str(row.get(name_col, "")).strip()
            if not name or len(name) < 4 or name.startswith("styles_"):
                continue
            norm = re.sub(r'\s+', ' ', name.lower().strip())
            try:
                price = float(str(row.get(price_col, 0)).replace(",", ""))
            except Exception:
                price = 0.0
            _cpk = _comp_catalog_product_key(cname, norm)
            _key = (cname, norm)

            _eid = _existing_map.get(_key)
            if _eid is not None:
                # صف موجود في القاعدة → UPDATE مُجمّع
                rows_updated += 1
                if _has_cpk:
                    _update_params.append((price, today, _cpk, _eid))
                else:
                    _update_params.append((price, today, _eid))
            elif _key in _pending_insert:
                # تكرار داخل نفس الدفعة: الأصل يجد الصف المُدرَج للتو ويُحدّث سعره
                # فقط (الاسم/first_seen يبقيان للأول) → ندمج السعر في صف الإدراج المعلّق.
                rows_updated += 1
                _pi = _pending_insert[_key]
                _old = _insert_params[_pi]
                _insert_params[_pi] = _old[:3] + (price,) + _old[4:]
            else:
                total_new += 1
                _pending_insert[_key] = len(_insert_params)
                if _has_cpk:
                    _insert_params.append((cname, name, norm, price, today, today, _cpk))
                else:
                    _insert_params.append((cname, name, norm, price, today, today))

    if _insert_params:
        if _has_cpk:
            conn.executemany(
                """INSERT INTO comp_catalog (competitor, product_name, norm_name, price,
                       first_seen, last_seen, comp_product_key)
                   VALUES (?,?,?,?,?,?,?)""",
                _insert_params,
            )
        else:
            conn.executemany(
                """INSERT INTO comp_catalog (competitor, product_name, norm_name, price, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?)""",
                _insert_params,
            )
    if _update_params:
        if _has_cpk:
            conn.executemany(
                "UPDATE comp_catalog SET price=?, last_seen=?, comp_product_key=? WHERE id=?",
                _update_params,
            )
        else:
            conn.executemany(
                "UPDATE comp_catalog SET price=?, last_seen=? WHERE id=?",
                _update_params,
            )

    return {"new_products": total_new, "updated": rows_updated}


def _upsert_comp_catalog_rowwise(conn, comp_dfs: dict, today: str, _has_cpk: bool):
    """المسار الأصلي صف-بصف (ملاذ آمن). لا commit/close هنا (المُوزِّع يتكفّل)."""
    import re
    total_new = 0
    rows_updated = 0

    for cname, cdf in comp_dfs.items():
        name_col, price_col = _resolve_comp_name_price_columns(cdf)

        for _, row in cdf.iterrows():
            name = str(row.get(name_col, "")).strip()
            if not name or len(name) < 4 or name.startswith("styles_"):
                continue
            norm = re.sub(r'\s+', ' ', name.lower().strip())
            try:
                price = float(str(row.get(price_col, 0)).replace(",", ""))
            except Exception:
                price = 0.0

            existing = conn.execute(
                "SELECT id FROM comp_catalog WHERE competitor=? AND norm_name=?",
                (cname, norm)
            ).fetchone()
            _cpk = _comp_catalog_product_key(cname, norm)

            if existing:
                rows_updated += 1
                if _has_cpk:
                    conn.execute(
                        "UPDATE comp_catalog SET price=?, last_seen=?, comp_product_key=? WHERE id=?",
                        (price, today, _cpk, existing[0]),
                    )
                else:
                    try:
                        conn.execute(
                            "UPDATE comp_catalog SET price=?, last_seen=? WHERE id=?",
                            (price, today, existing[0]),
                        )
                    except sqlite3.IntegrityError:
                        conn.execute(
                            "UPDATE comp_catalog SET price=?, last_seen=?, comp_product_key=? WHERE id=?",
                            (price, today, _cpk, existing[0]),
                        )
                        _has_cpk = True
            else:
                try:
                    if _has_cpk:
                        conn.execute(
                            """INSERT INTO comp_catalog (competitor, product_name, norm_name, price,
                                   first_seen, last_seen, comp_product_key)
                               VALUES (?,?,?,?,?,?,?)""",
                            (cname, name, norm, price, today, today, _cpk),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO comp_catalog (competitor, product_name, norm_name, price, first_seen, last_seen)
                               VALUES (?,?,?,?,?,?)""",
                            (cname, name, norm, price, today, today),
                        )
                except sqlite3.IntegrityError as _ie:
                    _em = str(_ie).lower()
                    if "comp_product_key" in _em and not _has_cpk:
                        conn.execute(
                            """INSERT INTO comp_catalog (competitor, product_name, norm_name, price,
                                   first_seen, last_seen, comp_product_key)
                               VALUES (?,?,?,?,?,?,?)""",
                            (cname, name, norm, price, today, today, _cpk),
                        )
                        _has_cpk = True
                    else:
                        raise
                total_new += 1

    return {"new_products": total_new, "updated": rows_updated}


def save_processed(product_key: str, product_name: str, competitor: str,
                   action: str, old_price=0.0, new_price=0.0,
                   product_id="", notes="", comp_url=""):
    """يحفظ منتجاً في قائمة المعالجة — مع منع التكرار، آمن للثريدات"""
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute(
                """INSERT OR REPLACE INTO processed_products
                   (timestamp, product_key, product_name, competitor, action,
                    old_price, new_price, product_id, notes, comp_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (_ts(), product_key, product_name, competitor, action,
                 old_price, new_price, product_id, notes, str(comp_url or ""))
            )
            conn.commit()
    except Exception:
        pass  # لا يوقف الثريد الخلفي


def get_processed(limit=50000) -> list:
    """يُعيد قائمة المنتجات المعالجة — الحد الافتراضي مرتفع لدعم 8000+ منتج"""
    conn = get_db()
    rows = conn.execute(
        """SELECT timestamp, product_key, product_name, competitor,
                  action, old_price, new_price, product_id, notes, IFNULL(comp_url,'')
           FROM processed_products ORDER BY timestamp DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    keys = ["timestamp","product_key","product_name","competitor",
            "action","old_price","new_price","product_id","notes","comp_url"]
    return [dict(zip(keys, r)) for r in rows]


def undo_processed(product_key: str) -> bool:
    """تراجع: إزالة المنتج من قائمة المعالجة"""
    conn = get_db()
    conn.execute("DELETE FROM processed_products WHERE product_key=?", (product_key,))
    conn.execute("DELETE FROM hidden_products WHERE product_key=?", (product_key,))
    conn.commit()
    conn.close()
    return True


def get_processed_keys() -> set:
    """مفاتيح المنتجات المعالجة لاستبعادها من القوائم"""
    conn = get_db()
    rows = conn.execute("SELECT product_key FROM processed_products").fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_processed_hydration_sets() -> tuple:
    """
    Phase 1 — يُعيد (product_ids: set, comp_urls: set, price_map: dict)
    للتحميل السريع عند بدء التطبيق.
    price_map: {product_id: new_price} لاستخدامه في Smart Reversion.
    استعلام واحد — O(N) حيث N = عدد المنتجات المعالجة.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT product_id, IFNULL(comp_url,''), new_price FROM processed_products"
    ).fetchall()
    conn.close()
    pids = set()
    urls = set()
    price_map = {}  # product_id → new_price (السعر الذي أُرسل لـ Make)
    for r in rows:
        pid = str(r[0] or "").strip()
        url = str(r[1] or "").strip()
        nprice = float(r[2] or 0)
        if pid and pid not in ("nan", "None", "NaN", ""):
            pids.add(pid)
            price_map[pid] = nprice
        if url:
            urls.add(url)
    return pids, urls, price_map


def bulk_revert_processed(product_keys: list) -> int:
    """
    Phase 1 — Smart Reversion: يحذف عدة منتجات من processed + hidden دفعة واحدة.
    يُعيد عدد الصفوف المحذوفة. آمن للثريدات.
    """
    if not product_keys:
        return 0
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            placeholders = ",".join("?" for _ in product_keys)
            cur1 = conn.execute(
                f"DELETE FROM processed_products WHERE product_key IN ({placeholders})",
                product_keys,
            )
            count1 = cur1.rowcount
            cur2 = conn.execute(
                f"DELETE FROM hidden_products WHERE product_key IN ({placeholders})",
                product_keys,
            )
            count2 = cur2.rowcount
            deleted = count1 + count2
            conn.commit()
        return deleted
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════
#  v26.0 — Migration Script + Automation Log
# ═══════════════════════════════════════════════════════════════
def migrate_db_v26():
    """
    سكريبت ترحيل v26.0 — يُنفَّذ مرة واحدة فقط.
    يضمن وجود كل الجداول المطلوبة بدون فقدان أي بيانات.
    آمن للتشغيل المتكرر (idempotent).
    """
    try:
        conn = get_db()
        cur = conn.cursor()

        # ── 1. جدول سجل الأتمتة ──
        cur.execute("""CREATE TABLE IF NOT EXISTS automation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now','localtime')),
            product_name TEXT,
            product_id TEXT,
            rule_name TEXT,
            action TEXT,
            old_price REAL,
            new_price REAL,
            comp_price REAL,
            competitor TEXT,
            match_score REAL,
            reason TEXT,
            pushed_to_make INTEGER DEFAULT 0
        )""")

        # ── 2. جدول إعدادات الأتمتة (للحفظ بين الجلسات) ──
        cur.execute("""CREATE TABLE IF NOT EXISTS automation_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )""")

        # ── 3. جدول نسخة قاعدة البيانات (لتتبع الترحيلات) ──
        cur.execute("""CREATE TABLE IF NOT EXISTS db_version (
            version TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now','localtime')),
            description TEXT
        )""")

        # ── 4. تسجيل أن الترحيل v26.0 تم تنفيذه ──
        cur.execute("""INSERT OR IGNORE INTO db_version (version, description)
                       VALUES ('v26.0', 'إضافة جداول الأتمتة الذكية وسجل القرارات')""")

        # ── 5. إضافة أعمدة جديدة للجداول الموجودة (بأمان) ──
        # إضافة عمود cost_price لجدول our_catalog إذا لم يكن موجوداً
        try:
            cur.execute("ALTER TABLE our_catalog ADD COLUMN cost_price REAL DEFAULT 0")
        except Exception:
            pass  # العمود موجود مسبقاً

        # إضافة عمود auto_processed لجدول processed_products
        try:
            cur.execute("ALTER TABLE processed_products ADD COLUMN auto_processed INTEGER DEFAULT 0")
        except Exception:
            pass

        # إضافة عمود comp_url لجدول processed_products (Phase 1 — Smart Reversion)
        try:
            cur.execute("ALTER TABLE processed_products ADD COLUMN comp_url TEXT DEFAULT ''")
        except Exception:
            pass  # العمود موجود مسبقاً

        # عمود comp_product_key قد يُضاف يدوياً بقيد NOT NULL — نضيفه فارغاً ثم نملأه إن أمكن
        try:
            cur.execute("ALTER TABLE comp_catalog ADD COLUMN comp_product_key TEXT")
        except Exception:
            pass
        try:
            cur.execute(
                """UPDATE comp_catalog SET comp_product_key = competitor || '::' || IFNULL(norm_name, '')
                   WHERE comp_product_key IS NULL OR TRIM(comp_product_key) = ''"""
            )
        except Exception:
            pass

        try:
            cur.execute("ALTER TABLE job_progress ADD COLUMN audit_json TEXT DEFAULT '{}'")
        except Exception:
            pass

        # ── 6. جدول المنافسين الرئيسي (Phase 1) ──
        cur.execute("""CREATE TABLE IF NOT EXISTS competitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            domain TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            added_at TEXT DEFAULT (datetime('now','localtime')),
            notes TEXT DEFAULT ''
        )""")

        # ── 7. جدول الأسماء البديلة — Aliases (Phase 1) ──
        # يُربط كل اسم بديل/مشوّه بالاسم الرسمي الوحيد للمنافس
        # مثال: alias="نمشي كوم" → canonical_name="Namshi"
        cur.execute("""CREATE TABLE IF NOT EXISTS competitor_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT NOT NULL UNIQUE,
            canonical_name TEXT NOT NULL,
            added_at TEXT DEFAULT (datetime('now','localtime'))
        )""")

        # ── 8. تسجيل إصدار Phase 1 ──
        cur.execute("""INSERT OR IGNORE INTO db_version (version, description)
                       VALUES ('v26.1', 'إضافة جداول competitors و competitor_aliases + resolve_competitor')""")

        conn.commit()
        conn.close()
    except Exception as e:
        _logger.error("Migration v26 error: %s", e)
        try: conn.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════
#  Phase 1 — resolve_competitor + إدارة Aliases
# ═══════════════════════════════════════════════════════════════

def resolve_competitor(name: str) -> str:
    """
    يحل اسم المنافس عبر جدول competitor_aliases أولاً (Phase 1).

    المنطق:
      - إذا وُجد alias مطابق → يعيد canonical_name (الاسم الرسمي)
      - إذا لم يوجد          → يعيد الاسم بعد trim بدون تغيير

    يضمن عدم تكرار نفس المنافس بأسماء مختلفة في التقارير والمطابقة.
    مثال: resolve_competitor("نمشي") == resolve_competitor("namshi.com") == "Namshi"
    """
    if not name:
        return name
    clean = name.strip()
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT canonical_name FROM competitor_aliases "
            "WHERE alias = ? COLLATE NOCASE",
            (clean,)
        ).fetchone()
        conn.close()
        if row:
            return str(row["canonical_name"]).strip()
    except Exception as exc:
        _logger.debug("resolve_competitor error: %s", exc)
    return clean


def add_competitor_alias(alias: str, canonical_name: str) -> bool:
    """
    يضيف اسماً بديلاً (alias) ويربطه بالاسم الرسمي (canonical_name).

    مثال الاستخدام:
      add_competitor_alias("نمشي", "Namshi")
      add_competitor_alias("namshi.com", "Namshi")
      add_competitor_alias("NAMSHI", "Namshi")

    يعيد True عند النجاح، False عند الخطأ أو المدخلات الفارغة.
    """
    alias = (alias or "").strip()
    canonical_name = (canonical_name or "").strip()
    if not alias or not canonical_name:
        return False
    try:
        conn = get_db()
        conn.execute(
            """INSERT OR REPLACE INTO competitor_aliases
               (alias, canonical_name, added_at)
               VALUES (?, ?, datetime('now','localtime'))""",
            (alias, canonical_name)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        _logger.error("add_competitor_alias error: %s", exc)
        return False


def get_all_aliases() -> list:
    """يُرجع كل الأسماء البديلة المسجلة مرتبة حسب الاسم الرسمي."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT alias, canonical_name, added_at "
            "FROM competitor_aliases ORDER BY canonical_name"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def register_competitor(name: str, domain: str = "", notes: str = "") -> bool:
    """
    يسجل منافساً رسمياً في جدول competitors.
    آمن للاستدعاء المتكرر (INSERT OR IGNORE).
    """
    name = (name or "").strip()
    if not name:
        return False
    try:
        conn = get_db()
        conn.execute(
            """INSERT OR IGNORE INTO competitors (name, domain, notes)
               VALUES (?, ?, ?)""",
            (name, domain.strip(), notes.strip())
        )
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        _logger.error("register_competitor error: %s", exc)
        return False


def get_all_competitors() -> list:
    """يُرجع كل المنافسين المسجلين في جدول competitors."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, name, domain, is_active, added_at, notes "
            "FROM competitors ORDER BY name"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def register_competitors_from_json(json_path: str = "") -> int:
    """
    Load competitors list from JSON file and register them in the DB.
    Reads data/competitors_list_v30.json by default.
    Returns the number of competitors registered (new or updated).

    Expected JSON format:
        [{"name": "...", "store_url": "...", "sitemap_url": "..."}, ...]
    """
    if not json_path:
        # Default: data/competitors_list_v30.json in project root
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        json_path = os.path.join(root, "data", "competitors_list_v30.json")

    if not os.path.exists(json_path):
        _logger.debug("register_competitors_from_json: file not found %s", json_path)
        return 0

    try:
        with open(json_path, "r", encoding="utf-8") as _f:
            comps = json.load(_f)
    except Exception as exc:
        _logger.warning("register_competitors_from_json read error: %s", exc)
        return 0

    if not isinstance(comps, list):
        return 0

    count = 0
    for c in comps:
        name = str(c.get("name", "")).strip()
        if not name:
            continue
        domain = str(c.get("store_url", "")).strip()
        notes  = str(c.get("sitemap_url", "")).strip()
        if register_competitor(name, domain=domain, notes=notes):
            count += 1
    _logger.info("register_competitors_from_json: registered %d competitors", count)
    return count



# ═══════════════════════════════════════════════════════════════════════════
#  محرك تراكم بيانات المنافسين عبر الجلسات — Persistent Competitor Store
# ═══════════════════════════════════════════════════════════════════════════

def init_competitor_store() -> None:
    """يُنشئ جدول التراكم إذا لم يكن موجوداً — آمن للاستدعاء المتكرر."""
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS competitor_products_store (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        competitor    TEXT NOT NULL,
        product_name  TEXT NOT NULL,
        norm_name     TEXT NOT NULL,
        price         REAL DEFAULT 0,
        image_url     TEXT DEFAULT '',
        product_url   TEXT DEFAULT '',
        brand         TEXT DEFAULT '',
        size          TEXT DEFAULT '',
        gender        TEXT DEFAULT '',
        added_at      TEXT DEFAULT (datetime('now','localtime')),
        updated_at    TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(competitor, norm_name)
    )""")
    # ⚡ v31.8: أعمدة الاستخراج المسبق — تُضاف تلقائياً إذا لم تكن موجودة
    _new_cols = [
        ("extracted_brand", "TEXT DEFAULT ''"),
        ("extracted_size",  "REAL DEFAULT 0"),
        ("extracted_type",  "TEXT DEFAULT ''"),
        ("extracted_gender","TEXT DEFAULT ''"),
        ("extracted_class", "TEXT DEFAULT ''"),
        ("agg_name",        "TEXT DEFAULT ''"),
        ("product_line",    "TEXT DEFAULT ''"),
    ]
    existing = {r[1] for r in conn.execute("PRAGMA table_info(competitor_products_store)").fetchall()}
    import re as _re
    for col_name, col_type in _new_cols:
        # whitelist: اسم العمود معرّف صالح + النوع الأساسي ضمن المسموح
        if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col_name):
            continue
        if col_type.split()[0].upper() not in {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"}:
            continue
        if col_name not in existing:
            try:
                conn.execute(f"ALTER TABLE competitor_products_store ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass
    conn.commit()
    conn.close()


def fill_extracted_features(batch_size: int = 5000) -> int:
    """⚡ v31.8: ملء أعمدة الاستخراج المسبق — يعمل مرة واحدة.
    يعود بعدد الصفوف المحدّثة."""
    init_competitor_store()
    conn = get_db()
    # فحص: هل هناك صفوف تحتاج ملء؟ (agg_name فارغ = لم تُعالج بعد)
    need = conn.execute(
        "SELECT COUNT(*) FROM competitor_products_store WHERE agg_name = '' OR agg_name IS NULL"
    ).fetchone()[0]
    if need == 0:
        conn.close()
        return 0

    # استيراد محلي لتجنب circular import
    from engines.engine import (
        extract_brand, extract_size, extract_type, extract_gender,
        classify_product, normalize_name, extract_product_line
    )

    updated = 0
    while True:
        rows = conn.execute(
            """SELECT id, product_name FROM competitor_products_store
               WHERE agg_name = '' OR agg_name IS NULL
               LIMIT ?""",
            (batch_size,)
        ).fetchall()
        if not rows:
            break
        updates = []
        for row_id, pname in rows:
            pname = str(pname or "")
            br = extract_brand(pname) or ""
            sz = extract_size(pname)
            tp = extract_type(pname) or ""
            gd = extract_gender(pname) or ""
            cl = classify_product(pname) or ""
            agg = normalize_name(pname) or pname  # fallback to name to avoid empty
            pl = extract_product_line(pname, br) or ""
            updates.append((br, sz, tp, gd, cl, agg, pl, row_id))
        conn.executemany(
            """UPDATE competitor_products_store SET
                extracted_brand=?, extracted_size=?, extracted_type=?,
                extracted_gender=?, extracted_class=?, agg_name=?, product_line=?
               WHERE id=?""",
            updates
        )
        conn.commit()
        updated += len(updates)
    conn.close()
    return updated


def _normalize_for_store(s: str) -> str:
    import unicodedata, re as _re
    t = unicodedata.normalize("NFKC", str(s or ""))
    t = _re.sub(r"[\u064B-\u065F\u0670]", "", t)
    t = _re.sub(r"[أإآا]", "ا", t)
    t = _re.sub(r"[ةه]", "ه", t)
    t = _re.sub(r"[يى]", "ي", t)
    return _re.sub(r"\s+", " ", t).strip().lower()


import re as _re_schema

_GENDER_MAP = {
    "رجالي": "رجالي", "رجال": "رجالي", "رجل": "رجالي",
    "men": "رجالي", "man": "رجالي", "male": "رجالي", "m": "رجالي",
    "نسائي": "نسائي", "نساء": "نسائي", "امرأة": "نسائي",
    "women": "نسائي", "woman": "نسائي", "female": "نسائي", "w": "نسائي",
    "أطفال": "أطفال", "اطفال": "أطفال", "طفل": "أطفال",
    "kids": "أطفال", "kid": "أطفال", "children": "أطفال",
    "child": "أطفال", "baby": "أطفال", "boys": "أطفال", "girls": "أطفال",
    "للجنسين": "للجنسين", "مشترك": "للجنسين",
    "unisex": "للجنسين", "both": "للجنسين", "all": "للجنسين",
}

_SIZE_PREFIX_RE = _re_schema.compile(
    r"^(?:المقاس|مقاس|الحجم|حجم|size|Size|SIZE)\s*[:：]\s*", _re_schema.IGNORECASE
)
_BRAND_PREFIX_RE = _re_schema.compile(
    r"^(?:الماركة|العلامة|ماركة|brand|Brand|BRAND)\s*[:：]\s*", _re_schema.IGNORECASE
)
_WS_RE = _re_schema.compile(r"\s+")
_CTRL_RE = _re_schema.compile(r"[\x00-\x1f\x7f]")


def _norm_text(s: str) -> str:
    """Strip control chars, collapse whitespace."""
    s = _CTRL_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _norm_gender(s: str) -> str:
    """Map free-form gender strings to canonical Arabic labels."""
    if not s:
        return "للجنسين"
    key = _norm_text(s).lower()
    if key in _GENDER_MAP:
        return _GENDER_MAP[key]
    for k, v in _GENDER_MAP.items():
        if k in key:
            return v
    return "للجنسين"


def _norm_size(s: str) -> str:
    s = _norm_text(s)
    s = _SIZE_PREFIX_RE.sub("", s)
    return s.strip()


def _norm_brand(s: str) -> str:
    s = _norm_text(s)
    s = _BRAND_PREFIX_RE.sub("", s)
    return s.strip()


def _canonical_url(url: str) -> str:
    """
    Canonicalize a product URL for intra-store dedup:
      - lowercase scheme + host
      - strip query string and fragment
      - strip trailing slash
    Same logical product page under different tracking params collapses
    to one key.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(url.strip())
        cleaned = urlunsplit((
            p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""
        ))
        return cleaned
    except Exception:
        return url.strip()


def dedup_rows_by_url(rows: list[dict]) -> list[dict]:
    """
    Intra-batch dedup: keep the LAST row seen per canonical product URL.
    Rows without a URL pass through unchanged (cannot be safely collapsed).
    """
    seen: dict[str, int] = {}
    out: list[dict | None] = list(rows)
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        u = _canonical_url(str(r.get("product_url") or r.get("url") or ""))
        if not u:
            continue
        if u in seen:
            out[seen[u]] = None  # drop earlier duplicate
        seen[u] = i
    return [r for r in out if r is not None]


def normalize_scraped_row_for_db(
    row: dict,
    competitor_domain: str = "",
) -> dict | None:
    """
    Phase 2 — canonical scraper-row → DB-row normalizer.

    Single transformation point between the scraper's loose row schema
    (mixed English/legacy keys) and the `competitor_products_store` table's
    expected schema (Arabic name/price keys + English auxiliary columns).

    Accepts any of:  المنتج | product_name | name
                     السعر  | price
                     image_url | image
                     product_url | url
                     brand, size, gender

    Returns None when the row is unusable so callers can filter cleanly
    instead of inserting polluted rows into the analysis engine:
      * missing or too-short name
      * non-numeric or non-positive price
    """
    if not isinstance(row, dict):
        return None

    name = _norm_text(str(
        row.get("المنتج")
        or row.get("product_name")
        or row.get("name")
        or ""
    ))
    if len(name) < 2:
        return None

    raw_price = row.get("السعر", None)
    if raw_price is None:
        raw_price = row.get("price", 0)
    try:
        price = float(
            str(raw_price or 0).replace(",", "").replace("﷼", "").replace("SAR", "").strip()
        )
    except (ValueError, TypeError):
        return None
    if price <= 0:
        return None

    return {
        "المنتج":      name,
        "السعر":       price,
        "image_url":   _norm_text(str(row.get("image_url") or row.get("image") or "")),
        "product_url": _norm_text(str(row.get("product_url") or row.get("url") or "")),
        "brand":       _norm_brand(str(row.get("brand") or "")),
        "size":        _norm_size(str(row.get("size") or "")),
        "gender":      _norm_gender(str(row.get("gender") or "")),
    }


_PHANTOM_NAME_RE = re.compile(
    # أنماط أسماء الـ placeholder التي كان يولدها sitemap_automation سابقاً
    # (slug URL على شكل P + أرقام/أحرف، قد يكون مسبوقاً بـ "منتج ").
    # نرفض الحفظ إذا طابق الاسم هذا النمط. لا يؤثر على الأسماء الحقيقية.
    r"^(?:منتج\s+)?[Pp][A-Za-z0-9_\-]*$"
)


def _is_phantom_row(name: str, price: float) -> bool:
    """منتج «وهمي» = سعر ≤ 0 واسم يبدو كـ slug/ID عشوائي.

    هذه هي الحارس الدفاعي الذي يمنع تلويث pricing_v30.db بآلاف الصفوف
    الفاسدة الناتجة عن فشل الكشط (403/Cloudflare/timeout).
    """
    if price > 0:
        return False
    if not name:
        return True
    try:
        return bool(_PHANTOM_NAME_RE.match(name.strip()))
    except Exception:
        return False


def upsert_competitor_products(
    competitor: str,
    products: list[dict],
    name_key: str = "المنتج",
    price_key: str = "السعر",
) -> dict:
    """
    يحفظ منتجات المنافس في جدول التراكم — INSERT OR UPDATE.
    يُعيد {'inserted': N, 'updated': M, 'skipped_phantom': K}
    """
    init_competitor_store()
    conn = get_db()
    inserted = updated = skipped_phantom = 0
    today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Phase 3: intra-batch dedup by canonical product URL (within the same
    # competitor). Prevents wasted UPDATE/INSERT cycles when a store's
    # sitemap + product-listing scrape both emit the same page.
    products = dedup_rows_by_url(list(products))

    # Phase 4: Atomic transaction — prevents race conditions during high-volume scraping
    try:
        conn.execute("BEGIN IMMEDIATE;")
    except Exception:
        pass  # SQLite auto-transaction fallback

    try:
        # ⚡ تحميل مسبق واحد لكل صفوف هذا المنافس (id + القيم القابلة لـ COALESCE)
        # بدل SELECT لكل منتج. norm_name مفتاح فريد ضمن المنافس (UNIQUE(competitor,norm_name)).
        # _existing_map[norm] = [id, image_url, product_url, brand]  (قائمة لنحدّثها داخل الدفعة)
        _existing_map: dict = {}
        for _er in conn.execute(
            "SELECT id, norm_name, image_url, product_url, brand "
            "FROM competitor_products_store WHERE competitor=?",
            (competitor,)
        ):
            _existing_map[_er["norm_name"]] = [
                _er["id"], _er["image_url"] or "", _er["product_url"] or "", _er["brand"] or ""
            ]

        # نراكم عمليات الإدراج والتحديث ثم ننفّذها دفعةً واحدة (executemany).
        _insert_params: list = []
        _update_params: list = []
        # norm → موضع في _insert_params لمنتج لم يُكتب بعد (لدمج التكرار داخل نفس الدفعة).
        _pending_insert_idx: dict = {}

        for p in products:
            pname = str(p.get(name_key, "") or "").strip()
            if not pname or len(pname) < 2:
                continue
            try:
                price = float(str(p.get(price_key, 0) or 0).replace(",", ""))
            except (ValueError, TypeError):
                price = 0.0

            # دفاع نهائي ضد المنتجات الوهمية: ارفض الحفظ إذا
            # (سعر ≤ 0) + (اسم على شكل placeholder «منتج P…»).
            if _is_phantom_row(pname, price):
                skipped_phantom += 1
                continue

            norm = _normalize_for_store(pname)
            _img = str(p.get("image_url", "") or "")
            _url = str(p.get("product_url", "") or "")
            _brand = str(p.get("brand", "") or "")

            _ex = _existing_map.get(norm)
            if _ex is not None:
                # صف موجود في القاعدة (أو حُدِّث سابقاً في هذه الدفعة):
                # COALESCE(NULLIF(جديد,''), قديم) = «احتفظ بالقديم إن كان الجديد فارغاً».
                _eid, _e_img, _e_url, _e_brand = _ex
                _n_img = _img if _img else _e_img
                _n_url = _url if _url else _e_url
                _n_brand = _brand if _brand else _e_brand
                if _eid is not None:
                    # صف فعلي في القاعدة → UPDATE مُجمّع (لا استعلامات فرعية: القيم محسوبة هنا).
                    _update_params.append((price, today, _n_img, _n_url, _n_brand, _eid))
                else:
                    # تكرار لاسمٍ أُدرج للتو في نفس الدفعة → ادمجه في صف الإدراج المعلّق.
                    # السلوك الأصلي: UPDATE اللاحق يحدّث السعر/الصور فقط ويُبقي الاسم/المقاس
                    #                للمنتج الأول، فنحافظ عليها هنا تماماً.
                    _pi = _pending_insert_idx[norm]
                    _old = _insert_params[_pi]
                    _insert_params[_pi] = (
                        _old[0], _old[1], _old[2], price, _n_img, _n_url, _n_brand,
                        _old[7], _old[8], _old[9], today
                    )
                # حدّث الخريطة كي يَسلسل أي تكرار لاحق فوق هذه القيم (مطابق للأصل).
                _existing_map[norm] = [_eid, _n_img, _n_url, _n_brand]
                updated += 1
            else:
                _size = str(p.get("size", "") or "")
                _gender = str(p.get("gender", "") or "للجنسين")
                _insert_params.append((
                    competitor, pname, norm, price, _img, _url, _brand,
                    _size, _gender, today, today
                ))
                _pending_insert_idx[norm] = len(_insert_params) - 1
                # سجّله كـ«موجود» بمعرّف None كي يُدمج أي تكرار لاحق ضمن الدفعة.
                _existing_map[norm] = [None, _img, _url, _brand]
                inserted += 1

        # كتابة مُجمّعة: إدراج كل الجديد ثم تحديث كل القديم (صفوف منفصلة، فلا يهم الترتيب بينها).
        if _insert_params:
            conn.executemany(
                """INSERT INTO competitor_products_store
                   (competitor, product_name, norm_name, price, image_url,
                    product_url, brand, size, gender, added_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                _insert_params
            )
        if _update_params:
            conn.executemany(
                """UPDATE competitor_products_store
                   SET price=?, updated_at=?, image_url=?, product_url=?, brand=?
                   WHERE id=?""",
                _update_params
            )

        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    # Update in-memory cache so the UI reflects new rows immediately,
    # even before the GCS sync cooldown expires on the next container restart.
    if inserted > 0 or updated > 0:
        invalidate_competitor_cache()  # ⚡ Clear cached queries
        with _IN_MEMORY_LOCK:
            existing_cache = _IN_MEMORY_PRODUCTS.get(competitor, [])
            _IN_MEMORY_PRODUCTS[competitor] = existing_cache + list(products)

        # Force a GCS upload after every scrape batch so data survives restarts.
        trigger_gcs_sync(force=True)

    if skipped_phantom:
        _logger.info(
            "upsert_competitor_products[%s]: skipped %d phantom row(s) — "
            "name-only placeholders with price ≤ 0 were rejected.",
            competitor, skipped_phantom,
        )

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_phantom": skipped_phantom,
    }


def get_all_competitor_products(competitor: str = "", limit: int = 150000) -> list[dict]:
    """يُرجع منتجات المنافس — مع حد أقصى لحماية الذاكرة."""
    init_competitor_store()
    conn = get_db()
    _extra = """,
                      COALESCE(extracted_brand,'') as extracted_brand,
                      COALESCE(extracted_size,0) as extracted_size,
                      COALESCE(extracted_type,'') as extracted_type,
                      COALESCE(extracted_gender,'') as extracted_gender,
                      COALESCE(extracted_class,'') as extracted_class,
                      COALESCE(agg_name,'') as agg_name,
                      COALESCE(product_line,'') as product_line"""
    if competitor:
        rows = conn.execute(
            f"""SELECT competitor, product_name, price, image_url, product_url,
                      brand, size, gender, added_at, updated_at{_extra}
               FROM competitor_products_store WHERE competitor=?
               ORDER BY price DESC
               LIMIT ?""",
            (competitor, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT competitor, product_name, price, image_url, product_url,
                      brand, size, gender, added_at, updated_at{_extra}
               FROM competitor_products_store
               WHERE price > 0
               ORDER BY updated_at DESC
               LIMIT ?""",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_scraped_urls_today(competitor: str) -> set[str]:
    """يُرجع مجموعة روابط المنتجات التي كُشطت بنجاح اليوم لهذا المنافس.

    تُستخدم كـ checkpoint للاستئناف: إذا أُغلق المتصفح في منتصف الكشط،
    يعود المستخدم ويضغط "تحديث ذكي" مرة أخرى، وسيتخطى المحرك آلياً كل
    الروابط التي سبق حفظها اليوم بسعر صحيح — بدلاً من البدء من الصفر.

    الشرط: price > 0 (لا نعتبر الأخطاء «نجاح»)
           AND date(updated_at) = date('now')
    """
    if not competitor:
        return set()
    init_competitor_store()
    try:
        conn = get_db()
        rows = conn.execute(
            """SELECT product_url
                 FROM competitor_products_store
                WHERE competitor = ?
                  AND price > 0
                  AND product_url IS NOT NULL
                  AND product_url != ''
                  AND date(updated_at) = date('now', 'localtime')""",
            (competitor,),
        ).fetchall()
        conn.close()
        return {str(r[0]).strip() for r in rows if r and r[0]}
    except Exception:
        _logger.debug("get_scraped_urls_today failed", exc_info=True)
        return set()


def get_competitor_products_df(competitor: str = "") -> "pd.DataFrame":
    """
    Returns a DataFrame from the persistent competitor store.
    ⚡ Cached for 60s to avoid repeated heavy DB queries.
    """
    import pandas as _pd
    cache_key = f"comp_df_{competitor}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    rows = get_all_competitor_products(competitor)
    if not rows:
        with _IN_MEMORY_LOCK:
            if competitor:
                mem_cached = _IN_MEMORY_PRODUCTS.get(competitor, [])
            else:
                mem_cached = [r for rows_list in _IN_MEMORY_PRODUCTS.values() for r in rows_list]
        if mem_cached:
            _logger.info(
                "get_competitor_products_df: SQLite empty, serving %d rows from memory cache for '%s'",
                len(mem_cached), competitor or "ALL",
            )
            result = _pd.DataFrame(mem_cached)
            _cache_set(cache_key, result)
            return result
        return _pd.DataFrame()
    result = _pd.DataFrame(rows)
    _cache_set(cache_key, result)
    return result


def get_competitor_store_stats() -> dict:
    """إحصاءات جدول التراكم. ⚡ Cached for 60s."""
    cache_key = "comp_stats"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    init_competitor_store()
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM competitor_products_store").fetchone()[0]
    with_price = conn.execute(
        "SELECT COUNT(*) FROM competitor_products_store WHERE price IS NOT NULL AND price > 0"
    ).fetchone()[0]
    comps = conn.execute(
        "SELECT competitor, COUNT(*) as cnt FROM competitor_products_store GROUP BY competitor"
    ).fetchall()
    conn.close()
    result = {
        "total_products": total,
        "with_price": with_price,
        "by_competitor": {r[0]: r[1] for r in comps},
    }
    _cache_set(cache_key, result)
    return result


def clear_competitor_store(competitor: str = "") -> int:
    """يحذف منتجات منافس محدد أو الكل. يُعيد عدد الصفوف المحذوفة."""
    init_competitor_store()
    conn = get_db()
    if competitor:
        conn.execute("DELETE FROM competitor_products_store WHERE competitor=?", (competitor,))
    else:
        conn.execute("DELETE FROM competitor_products_store")
    deleted = conn.execute("SELECT changes()").fetchone()[0]
    conn.commit()
    conn.close()
    return deleted


# ═══════════════════════════════════════════════════════════════════════════
#  Pillar 4 — GCP Real-Time Persistence
#  data/ directory is mounted as a Cloud persistent volume on GCP.
#  Every analysis result and scraped batch is written here so data survives
#  container restarts and is not lost in ephemeral memory.
# ═══════════════════════════════════════════════════════════════════════════

def save_realtime_results(
    df: "pd.DataFrame",   # type: ignore[name-defined]
    label: str = "pipeline",
    also_update_price_history: bool = True,
) -> str:
    """
    Persist a real-time analysis results DataFrame to two places:
      1. data/results_<label>_<timestamp>.csv  — GCP persistent volume, CSV format
      2. price_history table (SQLite)          — optional, for trend tracking

    Returns the path of the written CSV (or "" on failure).

    Thread-safe: CSV write uses a unique timestamp in the filename (no race conditions).
    SQLite writes use WAL mode (concurrent readers + one writer).

    Args:
        df                       : analysis results DataFrame from run_full_analysis()
                                   or the real-time fuzzy fallback.
        label                    : filename prefix (e.g. "pipeline", "batch_run").
        also_update_price_history: if True, upserts each matched row into price_history
                                   table for trend analysis and alerts.
    """
    import pandas as _pd
    if df is None or (hasattr(df, "empty") and df.empty):
        _logger.debug("save_realtime_results: empty DataFrame — skipping")
        return ""

    _data_dir = os.environ.get("DATA_DIR", "data")
    os.makedirs(_data_dir, exist_ok=True)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(_data_dir, f"results_{label}_{ts}.csv")

    # ── 1. Write CSV ─────────────────────────────────────────────────────────
    try:
        safe = df.copy()
        # Serialise list/dict columns that would corrupt CSV
        for col in safe.columns:
            try:
                if safe[col].apply(lambda x: isinstance(x, (list, dict))).any():
                    safe[col] = safe[col].astype(str)
            except Exception:
                pass
        safe.to_csv(path, index=False, encoding="utf-8-sig")
        _logger.info("save_realtime_results: CSV saved → %s  (%d rows)", path, len(df))
    except Exception as _csv_err:
        _logger.error("save_realtime_results: CSV write failed: %s", _csv_err)
        path = ""

    # ── 2. Update price_history (SQLite) ────────────────────────────────────
    if also_update_price_history:
        try:
            for _, row in df.iterrows():
                prod_name  = str(row.get("المنتج") or row.get("product_name") or "")
                competitor = str(row.get("المنافس") or row.get("competitor") or "")
                comp_price = float(row.get("سعر_المنافس") or row.get("comp_price") or 0)
                our_price  = float(row.get("السعر") or row.get("our_price") or 0)
                diff       = float(row.get("الفرق") or 0)
                score      = float(row.get("نسبة_التطابق") or 0)
                decision   = str(row.get("القرار") or "")
                pid        = str(row.get("معرف_المنتج") or row.get("product_id") or "")
                if prod_name and competitor and comp_price > 0:
                    upsert_price_history(
                        prod_name, competitor, comp_price,
                        our_price=our_price, diff=diff,
                        match_score=score, decision=decision,
                        product_id=pid,
                    )
        except Exception as _ph_err:
            _logger.debug("save_realtime_results: price_history update error: %s", _ph_err)

    return path


def append_scraper_csv(
    rows: List[dict],
    competitor: str,
    output_path: str = "",
) -> int:
    """
    Append a list of scraped product rows to a persistent CSV file in data/.
    Creates the file if it does not exist; appends without rewriting existing rows.

    Used by the realtime pipeline consumer to ensure every batch of scraped products
    is durably written to the GCP volume even if the analysis stage fails.

    Returns the total row-count of the file after appending.
    """
    import pandas as _pd

    if not rows:
        return 0

    _data_dir = os.environ.get("DATA_DIR", "data")
    os.makedirs(_data_dir, exist_ok=True)

    if not output_path:
        output_path = os.path.join(
            _data_dir,
            f"scraped_{competitor}_{datetime.now().strftime('%Y%m%d')}.csv",
        )

    new_df = _pd.DataFrame(rows)
    try:
        if os.path.exists(output_path):
            existing = _pd.read_csv(output_path, encoding="utf-8-sig", low_memory=False)
            combined = _pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df
        combined.to_csv(output_path, index=False, encoding="utf-8-sig")
        return len(combined)
    except Exception as _e:
        _logger.warning("append_scraper_csv error for %s: %s", competitor, _e)
        try:
            # Last resort: write just the new rows
            new_df.to_csv(output_path, index=False, encoding="utf-8-sig")
            return len(new_df)
        except Exception:
            return 0

