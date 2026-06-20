"""
utils/gcp_db.py — Google Cloud Database Integration v1.0
==========================================================
Provides two persistence backends for vision2030-v2:

  1. Google Cloud Storage (GCS) — SQLite sync
     The SQLite database is downloaded from a GCS bucket on startup
     and uploaded back after every batch of writes.
     This keeps all existing SQL logic intact while making data
     permanent on GCP.

  2. Cloud SQL (PostgreSQL) — optional full cloud backend
     When CLOUD_SQL_CONNECTION_NAME + DB_USER + DB_PASS + DB_NAME
     are all set, creates a SQLAlchemy engine via cloud-sql-python-connector.
     Intended for future migration once the schema is ported to PostgreSQL.

  3. Firestore — optional document-store backend
     When GCP_PROJECT_ID is set and USE_FIRESTORE=true, provides
     a Firestore client for document-level writes.

Environment variables (all optional — app falls back to SQLite if absent):
  GCP_PROJECT_ID              Google Cloud project ID
  GCS_BUCKET_NAME             GCS bucket that holds the DB backup
  GCS_DB_BLOB_NAME            Blob path inside bucket (default: vision2030/pricing_v30.db)
  CLOUD_SQL_CONNECTION_NAME   e.g. project:region:instance
  DB_USER                     Cloud SQL user
  DB_PASS                     Cloud SQL password
  DB_NAME                     Cloud SQL database name (default: vision2030)
  USE_FIRESTORE               Set to "true" to enable Firestore writes
"""

import logging
import os
import sqlite3
import tempfile
import threading
import time
from typing import Optional

_logger = logging.getLogger(__name__)

# ─── Read GCP config from environment ────────────────────────────────────────
GCP_PROJECT_ID           = os.environ.get("GCP_PROJECT_ID", "").strip()
GCS_BUCKET_NAME          = os.environ.get("GCS_BUCKET_NAME", "").strip()
GCS_DB_BLOB_NAME         = os.environ.get("GCS_DB_BLOB_NAME", "vision2030/pricing_v30.db").strip()
CLOUD_SQL_CONNECTION_NAME = os.environ.get("CLOUD_SQL_CONNECTION_NAME", "").strip()
DB_USER                  = os.environ.get("DB_USER", "").strip()
DB_PASS                  = os.environ.get("DB_PASS", "").strip()
DB_NAME                  = os.environ.get("DB_NAME", "vision2030").strip()
USE_FIRESTORE            = os.environ.get("USE_FIRESTORE", "").strip().lower() == "true"

# ─── GCS sync throttle: don't upload more than once per N seconds ─────────────
_GCS_SYNC_COOLDOWN_SECS = int(os.environ.get("GCS_SYNC_COOLDOWN", "60"))
_last_gcs_upload_time: float = 0.0
_gcs_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════
#  Status helpers
# ═══════════════════════════════════════════════════════════════════

def is_gcs_configured() -> bool:
    """True if GCS bucket sync is configured via environment."""
    return bool(GCS_BUCKET_NAME)


def is_cloud_sql_configured() -> bool:
    """True if all Cloud SQL connection parameters are present."""
    return bool(CLOUD_SQL_CONNECTION_NAME and DB_USER and DB_PASS)


def is_firestore_configured() -> bool:
    """True if Firestore is explicitly enabled and GCP project is set."""
    return bool(USE_FIRESTORE and GCP_PROJECT_ID)


def gcp_status() -> dict:
    """Returns a dict summarising which GCP backends are active."""
    return {
        "gcs_enabled":       is_gcs_configured(),
        "cloud_sql_enabled": is_cloud_sql_configured(),
        "firestore_enabled": is_firestore_configured(),
        "project_id":        GCP_PROJECT_ID or "(not set)",
        "gcs_bucket":        GCS_BUCKET_NAME or "(not set)",
        "cloud_sql_conn":    CLOUD_SQL_CONNECTION_NAME or "(not set)",
    }


# ═══════════════════════════════════════════════════════════════════
#  GCS — SQLite Sync
# ═══════════════════════════════════════════════════════════════════

def _cleanup_sqlite_sidecars(base_path: str) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = f"{base_path}{suffix}"
        try:
            if os.path.exists(sidecar):
                os.remove(sidecar)
        except OSError as exc:
            _logger.debug("GCS: failed to remove sidecar %s: %s", sidecar, exc)


def _validate_sqlite_file(db_path: str) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        result = conn.execute("PRAGMA integrity_check;").fetchone()
        status = str(result[0] if result else "").strip().lower()
        if status != "ok":
            raise sqlite3.DatabaseError(f"integrity_check failed: {status or 'unknown'}")
    finally:
        conn.close()


def _create_sqlite_snapshot(local_path: str) -> str:
    """Create a consistent SQLite snapshot for upload, even when WAL is active."""
    tmp = tempfile.NamedTemporaryFile(prefix="sqlite_snapshot_", suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    src = sqlite3.connect(local_path, timeout=30, check_same_thread=False)
    dst = sqlite3.connect(tmp_path, timeout=30, check_same_thread=False)
    try:
        src.execute("PRAGMA busy_timeout=30000;")
        try:
            src.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except sqlite3.DatabaseError:
            pass
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    _validate_sqlite_file(tmp_path)
    return tmp_path


def sync_db_from_gcs(local_path: str) -> bool:
    """
    Download the SQLite DB file from GCS to local_path safely.
    The downloaded file is first validated, then atomically moved in place.
    Returns True if the file was restored successfully.
    """
    if not is_gcs_configured():
        return False
    try:
        from google.cloud import storage  # type: ignore
        client = storage.Client(project=GCP_PROJECT_ID or None)
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(GCS_DB_BLOB_NAME)
        if not blob.exists():
            _logger.info("GCS: blob gs://%s/%s does not exist yet — will create on first write",
                         GCS_BUCKET_NAME, GCS_DB_BLOB_NAME)
            return False

        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(prefix="sqlite_restore_", suffix=".db", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            blob.download_to_filename(tmp_path)
            _validate_sqlite_file(tmp_path)
            os.replace(tmp_path, local_path)
            _cleanup_sqlite_sidecars(local_path)
            size_kb = os.path.getsize(local_path) // 1024
            _logger.info("GCS: restored validated DB (%d KB) from gs://%s/%s → %s",
                         size_kb, GCS_BUCKET_NAME, GCS_DB_BLOB_NAME, local_path)
            return True
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    except ImportError:
        _logger.warning("GCS: google-cloud-storage not installed — run pip install google-cloud-storage")
    except Exception as exc:
        _logger.warning("GCS sync_from_gcs error: %s", exc)
    return False


def sync_db_to_gcs(local_path: str, force: bool = False) -> bool:
    """
    Upload a consistent SQLite snapshot to GCS instead of the raw live DB file.
    This prevents corruption when the application is running in WAL mode.
    """
    if not is_gcs_configured():
        return False
    if not os.path.exists(local_path):
        _logger.debug("GCS sync_to_gcs: local file not found: %s", local_path)
        return False

    global _last_gcs_upload_time
    with _gcs_lock:
        now = time.time()
        if not force and (now - _last_gcs_upload_time) < _GCS_SYNC_COOLDOWN_SECS:
            _logger.debug("GCS: upload throttled (cooldown %ds)", _GCS_SYNC_COOLDOWN_SECS)
            return False
        snapshot_path = None
        try:
            from google.cloud import storage  # type: ignore
            snapshot_path = _create_sqlite_snapshot(local_path)
            client = storage.Client(project=GCP_PROJECT_ID or None)
            bucket = client.bucket(GCS_BUCKET_NAME)
            blob = bucket.blob(GCS_DB_BLOB_NAME)
            blob.upload_from_filename(snapshot_path)
            size_kb = os.path.getsize(snapshot_path) // 1024
            _last_gcs_upload_time = time.time()
            _logger.info("GCS: uploaded validated SQLite snapshot (%d KB) → gs://%s/%s",
                         size_kb, GCS_BUCKET_NAME, GCS_DB_BLOB_NAME)
            return True
        except ImportError:
            _logger.warning("GCS: google-cloud-storage not installed")
        except Exception as exc:
            _logger.warning("GCS sync_to_gcs error: %s", exc)
        finally:
            if snapshot_path and os.path.exists(snapshot_path):
                try:
                    os.remove(snapshot_path)
                except OSError:
                    pass
    return False


def schedule_background_gcs_sync(local_path: str,
                                  interval_secs: int = 300) -> None:
    """
    Start a daemon thread that uploads the SQLite DB to GCS every interval_secs.
    Safe to call multiple times — only the first call starts the thread.
    """
    if not is_gcs_configured():
        return

    if getattr(schedule_background_gcs_sync, "_started", False):
        return
    schedule_background_gcs_sync._started = True

    def _loop():
        while True:
            time.sleep(interval_secs)
            try:
                sync_db_to_gcs(local_path, force=True)
            except Exception as exc:
                _logger.debug("GCS background sync error: %s", exc)

    t = threading.Thread(target=_loop, name="gcs-sync", daemon=True)
    t.start()
    _logger.info("GCS: background sync thread started (interval=%ds)", interval_secs)


# ═══════════════════════════════════════════════════════════════════
#  Cloud SQL — SQLAlchemy Engine (PostgreSQL)
# ═══════════════════════════════════════════════════════════════════

_cloud_sql_engine = None
_cloud_sql_engine_lock = threading.Lock()


def get_cloud_sql_engine():
    """
    Return (or create) a SQLAlchemy engine connected to Cloud SQL via
    the cloud-sql-python-connector.  Returns None if not configured or
    if the required libraries are not installed.

    The engine uses pg8000 (pure-Python PostgreSQL driver) — no C libs needed.
    Connection pooling is handled internally by SQLAlchemy.
    """
    global _cloud_sql_engine
    if _cloud_sql_engine is not None:
        return _cloud_sql_engine

    if not is_cloud_sql_configured():
        return None

    with _cloud_sql_engine_lock:
        if _cloud_sql_engine is not None:
            return _cloud_sql_engine
        try:
            from google.cloud.sql.connector import Connector  # type: ignore
            from sqlalchemy import create_engine              # type: ignore

            connector = Connector()

            def _get_conn():
                return connector.connect(
                    CLOUD_SQL_CONNECTION_NAME,
                    "pg8000",
                    user=DB_USER,
                    password=DB_PASS,
                    db=DB_NAME,
                )

            _cloud_sql_engine = create_engine(
                "postgresql+pg8000://",
                creator=_get_conn,
                pool_size=5,
                max_overflow=10,
                pool_timeout=30,
                pool_recycle=1800,
            )
            _logger.info("Cloud SQL: engine created for %s (database=%s)",
                         CLOUD_SQL_CONNECTION_NAME, DB_NAME)
            return _cloud_sql_engine
        except ImportError as imp_err:
            _logger.warning("Cloud SQL: missing library — %s. "
                            "Run: pip install cloud-sql-python-connector[pg8000] SQLAlchemy",
                            imp_err)
        except Exception as exc:
            _logger.error("Cloud SQL: failed to create engine: %s", exc)
    return None


def test_cloud_sql_connection() -> bool:
    """Ping Cloud SQL with a simple SELECT 1. Returns True on success."""
    engine = get_cloud_sql_engine()
    if engine is None:
        return False
    try:
        from sqlalchemy import text  # type: ignore
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _logger.info("Cloud SQL: connection test passed")
        return True
    except Exception as exc:
        _logger.error("Cloud SQL: connection test failed: %s", exc)
        return False


# ═══════════════════════════════════════════════════════════════════
#  Firestore — Document Store Client
# ═══════════════════════════════════════════════════════════════════

_firestore_client = None
_firestore_lock = threading.Lock()


def get_firestore_client():
    """
    Return (or create) a Firestore client.
    Returns None if Firestore is not configured or library not installed.
    """
    global _firestore_client
    if _firestore_client is not None:
        return _firestore_client

    if not is_firestore_configured():
        return None

    with _firestore_lock:
        if _firestore_client is not None:
            return _firestore_client
        try:
            from google.cloud import firestore  # type: ignore
            _firestore_client = firestore.Client(project=GCP_PROJECT_ID)
            _logger.info("Firestore: client created for project %s", GCP_PROJECT_ID)
            return _firestore_client
        except ImportError:
            _logger.warning("Firestore: google-cloud-firestore not installed. "
                            "Run: pip install google-cloud-firestore")
        except Exception as exc:
            _logger.error("Firestore: failed to create client: %s", exc)
    return None


def firestore_upsert(collection: str, doc_id: str, data: dict) -> bool:
    """
    Write (merge) a document into Firestore.
    Returns True on success, False if Firestore is not available.
    """
    client = get_firestore_client()
    if client is None:
        return False
    try:
        client.collection(collection).document(doc_id).set(data, merge=True)
        return True
    except Exception as exc:
        _logger.warning("Firestore upsert error (%s/%s): %s", collection, doc_id, exc)
        return False


def firestore_get(collection: str, doc_id: str) -> Optional[dict]:
    """Fetch a Firestore document. Returns None if not found or unavailable."""
    client = get_firestore_client()
    if client is None:
        return None
    try:
        doc = client.collection(collection).document(doc_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception as exc:
        _logger.warning("Firestore get error (%s/%s): %s", collection, doc_id, exc)
        return None


# ═══════════════════════════════════════════════════════════════════
#  Auth check helper
# ═══════════════════════════════════════════════════════════════════

def check_gcp_auth() -> dict:
    """
    Verify that Application Default Credentials (ADC) are available.
    Returns {'ok': bool, 'email': str, 'error': str}.
    Run 'gcloud auth application-default login' if ok=False.
    """
    result = {"ok": False, "email": "", "error": ""}
    try:
        import google.auth  # type: ignore
        credentials, project = google.auth.default()
        # Refresh to confirm credentials are valid
        import google.auth.transport.requests  # type: ignore
        credentials.refresh(google.auth.transport.requests.Request())
        result["ok"]    = True
        result["email"] = getattr(credentials, "service_account_email",
                                  getattr(credentials, "_service_account_email", "ADC"))
        result["project"] = project or GCP_PROJECT_ID or "(unknown)"
        _logger.info("GCP auth OK — project=%s", result["project"])
    except ImportError:
        result["error"] = "google-auth library not installed"
    except Exception as exc:
        result["error"] = str(exc)
        _logger.warning("GCP auth check failed: %s", exc)
    return result
