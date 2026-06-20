"""
engines/anti_ban.py — Shim (توافق رجعي)
════════════════════════════════════════
المصدر الحقيقي: scrapers/anti_ban.py
هذا الملف مجرد جسر للتوافق مع أي import قديم يستخدم engines.anti_ban
"""
# noinspection PyUnresolvedReferences
from scrapers.anti_ban import (  # noqa: F401, F403
    get_browser_headers,
    get_xml_headers,
    get_rate_limiter,
    fetch_with_retry,
    try_curl_cffi,
    try_cloudscraper,
    try_all_sync_fallbacks,
    AdaptiveRateLimiter,
    _REAL_UA_POOL,
    _ACCEPT_LANGUAGES,
    _ACCEPT_HEADERS,
    _rate_limiter,
)

# wildcard للتوافق الكامل
from scrapers.anti_ban import *  # noqa: F401, F403
