"""
مسارات التخزين الدائم — Railway Volume وغيرها.

اضبط المتغير DATA_DIR ليطابق مسار تثبيت الـ volume (مثلاً /data).
بدون ذلك يُستخدم مجلد data/ داخل جذر المشروع (للتطوير المحلي).
"""
import os


def get_data_dir() -> str:
    d = (os.environ.get("DATA_DIR") or "").strip()
    if d:
        os.makedirs(d, exist_ok=True)
        return d
    # Fallback: use data/ relative to this file's project root (two levels up from utils/)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local_data = os.path.join(root, "data")
    os.makedirs(local_data, exist_ok=True)
    return local_data


def get_data_db_path(filename: str) -> str:
    """مسار ملف قاعدة بيانات داخل مجلد البيانات."""
    return os.path.join(get_data_dir(), filename)


def get_catalog_data_path(filename: str) -> str:
    """
    مسار ملفات الكتالوج (brands.csv / categories.csv):
    1. DATA_DIR (Railway Volume) إن كان مضبوطاً.
    2. data/ نسبي إلى جذر المشروع (للتطوير المحلي).
    """
    data_dir = (os.environ.get("DATA_DIR") or "").strip()
    if data_dir:
        return os.path.join(data_dir, filename)
    # جذر المشروع = أعلى بمستوى واحد من هذا الملف (utils/)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", filename)
