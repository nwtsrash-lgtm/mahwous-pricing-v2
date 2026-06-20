"""
engines/scheduler.py — Shim (توافق رجعي)
══════════════════════════════════════════
المصدر الحقيقي: scrapers/scheduler.py (Master v2.0)
هذا الملف مجرد جسر للتوافق مع أي import قديم يستخدم engines.scheduler

لا تعدّل هذا الملف — عدّل scrapers/scheduler.py مباشرة.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scrapers.scheduler import *  # noqa: F401, F403
from scrapers.scheduler import (  # noqa: F401
    get_scheduler_status,
    enable_scheduler,
    disable_scheduler,
    trigger_now,
    start_scheduler_thread,
    stop_scheduler_thread,
    DEFAULT_INTERVAL_HOURS,
)
