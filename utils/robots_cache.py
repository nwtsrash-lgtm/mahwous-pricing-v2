"""
utils/robots_cache.py — كاش robots.txt لكل نطاق (Phase 3)
═══════════════════════════════════════════════════════════
يُحمِّل robots.txt مرة واحدة لكل نطاق خلال 24 ساعة ويُخزّن نتيجة
`RobotFileParser` في الذاكرة، بحيث نتجنّب:
  - جلب robots.txt مع كل منتج (ضغط غير ضروري + تأخير).
  - كشط روابط مُصرّح بها كـ Disallow (احترام قواعد المتجر).

سلوك fail-open متعمَّد: إذا فشل تحميل robots.txt (شبكة/404/Cloudflare)
نسمح بالكشط — بدلاً من حظر كامل المتجر بسبب خطأ عابر.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import aiohttp

logger = logging.getLogger("RobotsCache")

_TTL_SECONDS = 24 * 3600
_DEFAULT_UA = "MahwousScraper/1.0"
_FETCH_TIMEOUT = 8

_CACHE: Dict[str, Tuple[RobotFileParser, float]] = {}
_LOCKS: Dict[str, asyncio.Lock] = {}
_LOCKS_LOOP_ID: int = 0  # id() of the event loop that owns current locks


def _base_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT), ssl=False
        ) as resp:
            if resp.status == 200:
                return await resp.text(errors="ignore")
    except Exception as e:
        logger.debug("robots fetch error %s: %s", url, e)
    return ""


def _build_parser(text: str) -> RobotFileParser:
    rp = RobotFileParser()
    rp.parse(text.splitlines() if text else [])
    return rp


async def get_parser(
    session: aiohttp.ClientSession,
    url: str,
) -> RobotFileParser:
    base = _base_of(url)
    now = time.time()
    hit = _CACHE.get(base)
    if hit and (now - hit[1]) < _TTL_SECONDS:
        return hit[0]

    global _LOCKS_LOOP_ID
    try:
        current_loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        current_loop_id = 0

    # Flush stale locks when the event loop has been replaced
    if current_loop_id and current_loop_id != _LOCKS_LOOP_ID:
        _LOCKS.clear()
        _LOCKS_LOOP_ID = current_loop_id

    lock = _LOCKS.setdefault(base, asyncio.Lock())
    async with lock:
        hit = _CACHE.get(base)
        if hit and (now - hit[1]) < _TTL_SECONDS:
            return hit[0]
        text = await _fetch_text(session, f"{base}/robots.txt")
        rp = _build_parser(text)
        _CACHE[base] = (rp, now)
        return rp


async def can_fetch(
    session: aiohttp.ClientSession,
    url: str,
    user_agent: str = _DEFAULT_UA,
) -> bool:
    try:
        rp = await get_parser(session, url)
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True  # fail-open


def clear_cache() -> None:
    _CACHE.clear()
    _LOCKS.clear()


def cache_size() -> int:
    return len(_CACHE)
