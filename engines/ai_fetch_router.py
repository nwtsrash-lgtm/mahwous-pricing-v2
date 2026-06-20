"""
engines/ai_fetch_router.py — Smart per-domain fetch strategy router (v1.0)
═══════════════════════════════════════════════════════════════════════════
يتتبع نجاح/فشل كل استراتيجية جلب لكل دومين ويختار الأفضل في الطلب التالي.
استراتيجيات:
  • aiohttp    — السريع، أول الاختيارات لو ما حُجب
  • curl_cffi  — بصمة TLS لكروم (يكسر Cloudflare)
  • cloudscraper — يحل تحديات JS
  • selenium   — الملاذ الأخير (يُفعّل عند طلب صريح)

كل عدة فشلات متتالية لاستراتيجية → تُنقل في الترتيب لأسفل لهذا الدومين.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("AIFetchRouter")

_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "fetch_router_state.json"
_DEFAULT_ORDER: List[str] = ["aiohttp", "curl_cffi", "cloudscraper"]
_FAIL_THRESHOLD = 3
_COOLDOWN_SECS = 600  # 10 min — after this, reset failures


class FetchRouter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, Dict] = defaultdict(self._new_entry)
        self._load()

    @staticmethod
    def _new_entry() -> Dict:
        return {
            "order":    list(_DEFAULT_ORDER),
            "fails":    {s: 0 for s in _DEFAULT_ORDER},
            "last":     0.0,
        }

    # ─── persistence ─────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            if _STATE_PATH.exists():
                raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
                for dom, entry in raw.items():
                    e = self._new_entry()
                    e.update(entry)
                    e.setdefault("order", list(_DEFAULT_ORDER))
                    e.setdefault("fails", {s: 0 for s in _DEFAULT_ORDER})
                    self._state[dom] = e
        except Exception as err:
            logger.debug("router state load failed: %s", err)

    def _save(self) -> None:
        try:
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STATE_PATH.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as err:
            logger.debug("router state save failed: %s", err)

    # ─── public API ──────────────────────────────────────────────────────
    def best_order(self, domain: str) -> List[str]:
        """Return strategies in order of historical success for this domain."""
        with self._lock:
            entry = self._state[domain]
            # Cooldown: if last activity was long ago, reset failure counters
            if entry["last"] and (time.time() - entry["last"] > _COOLDOWN_SECS):
                entry["fails"] = {s: 0 for s in _DEFAULT_ORDER}
                entry["order"] = list(_DEFAULT_ORDER)
            return list(entry["order"])

    def record(self, domain: str, strategy: str, success: bool) -> None:
        with self._lock:
            entry = self._state[domain]
            entry["last"] = time.time()
            if success:
                entry["fails"][strategy] = 0
                # Promote the winning strategy to the front
                if strategy in entry["order"]:
                    entry["order"].remove(strategy)
                    entry["order"].insert(0, strategy)
            else:
                entry["fails"][strategy] = entry["fails"].get(strategy, 0) + 1
                if entry["fails"][strategy] >= _FAIL_THRESHOLD and strategy in entry["order"]:
                    entry["order"].remove(strategy)
                    entry["order"].append(strategy)  # demote to the back
            # Persist occasionally — every 10 failure updates at most
            fail_total = sum(entry["fails"].values())
            if fail_total > 0 and fail_total % 10 == 0:
                self._save()


_router = FetchRouter()


def get_router() -> FetchRouter:
    return _router
