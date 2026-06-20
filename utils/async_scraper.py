"""
utils/async_scraper.py — Shim (توافق رجعي)
════════════════════════════════════════════
المصدر الحقيقي: engines/async_scraper.py
هذا الملف مجرد جسر للتوافق مع أي import قديم يستخدم utils.async_scraper
"""
from engines.async_scraper import *  # noqa: F401, F403
from engines.async_scraper import (  # noqa: F401
    run_scraper,
    main,
    extract_product,
    fetch_product,
    Progress,
)

if __name__ == "__main__":
    main()
