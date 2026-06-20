"""
engines/smart_router.py
The single brain that decides where every product belongs.
Always writes through `product_state` (the single source of truth) and
records every transition in the audit log.

Status map:
  NEW              → just arrived, awaiting classification
  MATCHED          → matches our catalog confidently (≥ DUPLICATE_THRESHOLD)
  MISSING          → genuinely missing (≤ MATCH_THRESHOLD)
  REVIEW           → borderline OR confirmed duplicate from missing list
  DUPLICATE        → confirmed duplicate of an existing product
  NEEDS_ATTENTION  → safety-net: stuck > 24h
  DONE             → manually resolved by user
"""
from typing import Optional, Callable

from utils.product_key import make_product_key
from utils.db_manager import (
    upsert_product_state, get_product_state, list_products_by_status,
    stale_products,
)
from engines.duplicate_detector import detect, Verdict


def ingest_product(product: dict,
                   catalog: list,
                   existing_keys: Optional[set] = None,
                   ai_verify: Optional[Callable] = None,
                   decided_by: str = "auto_router") -> dict:
    """
    Single entry point for every scraped product.
    Detects duplicates → routes to the correct status → records audit trail.
    Returns the resulting state row.
    """
    name = product.get("name", "") or product.get("product_name", "")
    store = product.get("store", "") or product.get("competitor", "")
    url = product.get("url", "") or product.get("link", "")
    key = make_product_key(name, store, url)

    verdict: Verdict = detect(product, catalog, existing_keys, ai_verify)

    status_map = {
        "DUPLICATE": "REVIEW",   # duplicates land in Under-Review for human confirm
        "REVIEW":    "REVIEW",
        "MATCHED":   "MATCHED",
        "NEW":       "MISSING",  # truly new = missing from our catalog
    }
    target = status_map.get(verdict.decision, "REVIEW")

    upsert_product_state(
        product_key=key, name=name, store=store, url=url,
        status=target, confidence=verdict.confidence,
        duplicate_of=verdict.duplicate_of, decided_by=decided_by,
        payload={**product, "verdict": verdict.to_dict()},
        reason=verdict.reason,
    )
    return get_product_state(key) or {}


def reroute_after_reanalysis(product_key: str, verdict: Verdict,
                             decided_by: str = "reanalysis") -> dict:
    """Apply a fresh verdict (from re-analysis) to an existing product."""
    state = get_product_state(product_key) or {}
    status_map = {
        "DUPLICATE": "REVIEW",
        "REVIEW":    "REVIEW",
        "MATCHED":   "MATCHED",
        "NEW":       "MISSING",
    }
    target = status_map.get(verdict.decision, "REVIEW")
    upsert_product_state(
        product_key=product_key,
        name=state.get("product_name", ""),
        store=state.get("store", ""),
        url=state.get("url", ""),
        status=target,
        confidence=verdict.confidence,
        duplicate_of=verdict.duplicate_of,
        decided_by=decided_by,
        reason=f"إعادة تحليل: {verdict.reason}",
    )
    return get_product_state(product_key) or {}


def mark_decision(product_key: str, new_status: str,
                  decided_by: str = "user", reason: str = "") -> dict:
    """User action: explicit move (approved/missing/duplicate/done/...)."""
    state = get_product_state(product_key)
    if not state:
        return {}
    upsert_product_state(
        product_key=product_key,
        name=state.get("product_name", ""),
        store=state.get("store", ""),
        url=state.get("url", ""),
        status=new_status,
        confidence=state.get("confidence") or 0,
        duplicate_of=state.get("duplicate_of"),
        decided_by=decided_by,
        reason=reason,
    )
    return get_product_state(product_key) or {}


def safety_sweep(hours: int = 24) -> int:
    """Move products stuck in NEW/MISSING > N hours into NEEDS_ATTENTION."""
    moved = 0
    for st in ("NEW", "MISSING"):
        for p in stale_products(hours=hours, status=st):
            mark_decision(p["product_key"], "NEEDS_ATTENTION",
                          decided_by="safety_net",
                          reason=f"ثابت > {hours} ساعة في {st}")
            moved += 1
    return moved
