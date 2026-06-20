"""
engines/realtime_pipeline.py — Real-Time Scraping + Matching Pipeline v2.0
===========================================================================
CRITICAL FIXES in v2.0 (replacing broken v1.0):

  PILLAR 2 — Extreme Parallel Scraping
    • ALL stores launch simultaneously via asyncio.gather — no sequential bottleneck
    • Shared TCPConnector(limit=500, limit_per_host=50) — high concurrency, WAF-safe
    • Staggered producer starts (0.5s × store_index) — anti-burst WAF protection

  PILLAR 3 — Real-Time Analysis
    • Per-row reverse matching in consumer — product arrives → analysed → UI updated
      IMMEDIATELY, without waiting for all stores to finish
    • Full engine analysis (run_full_analysis) runs in ThreadPoolExecutor — event loop
      is NEVER blocked during the potentially minutes-long analysis phase
    • Unbounded asyncio.Queue() — eliminates deadlock that occurred when 25 producers
      raced to put() into a maxsize=500 queue while consumer was slow

  PILLAR 4 — GCP Persistence
    • Competitor products: upserted to SQLite (WAL mode) every 50 rows per store
    • Analysis results: saved to data/results_pipeline_<timestamp>.csv
      (the data/ directory is the GCP-mounted persistent volume)

Event protocol (unchanged — backward-compatible with scraper_advanced.py bridge):
  "scraping_progress"     {"store": str, "count": int, "row": dict}
  "match_result"          {"row": dict}                 ← NEW: per-row live match
  "scraping_done"         {"store": str, "total": int}
  "matching_start"        {"total_rows": int, "stores": list[str]}
  "complete"              {"df": pd.DataFrame, "audit": dict}
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import traceback
from datetime import datetime
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

import aiohttp
import pandas as pd

from observability.ledger import (
    CompetitorIntakeLedger,
    NullLedger,
    REJECTED_LOW_CONFIDENCE,
    CONFIRMED_MATCH,
)
from utils.data_paths import get_data_db_path

logger = logging.getLogger("RealtimePipeline")

_DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(_DATA_DIR, exist_ok=True)

# Unique sentinel — signals that a producer has finished.
_STORE_DONE = object()

# How many scraped rows to accumulate per store before flushing to SQLite.
_PERSIST_BATCH_SIZE = 50


# ══════════════════════════════════════════════════════════════════════════════
#  Real-time reverse-match helpers
#  (competitor product → our closest product, per-row, O(N) per batch)
# ══════════════════════════════════════════════════════════════════════════════

def _build_our_lookup(our_df: pd.DataFrame) -> list:
    """
    Pre-index our product catalogue for fast per-row reverse matching.
    Called ONCE before scraping starts. Cost: O(len(our_df)).

    Returns a list of dicts with normalised names so that the consumer can
    do  fuzz.WRatio(comp_norm, entry["norm"])  for every incoming row.
    """
    try:
        from engines.engine import normalize, extract_brand
    except Exception:
        def normalize(s: str) -> str:   # type: ignore[misc]
            return str(s or "").lower().strip()
        def extract_brand(s: str) -> str:   # type: ignore[misc]
            return ""

    entries = []
    for _, row in our_df.iterrows():
        name = str(
            row.get("اسم المنتج") or row.get("product_name") or
            row.get("المنتج") or row.get("name") or ""
        ).strip()
        if not name or len(name) < 2:
            continue
        try:
            price = float(
                str(row.get("السعر") or row.get("سعر المنتج") or
                    row.get("price") or 0).replace(",", "").strip() or 0
            )
        except Exception:
            price = 0.0
        pid = str(
            row.get("رقم المنتج") or row.get("معرف_المنتج") or
            row.get("product_id") or row.get("id") or ""
        ).strip().rstrip(".0") or ""
        entries.append({
            "norm":  normalize(name),
            "name":  name,
            "price": price,
            "id":    pid,
            "brand": extract_brand(name),
        })
    return entries


def _reverse_match_one(
    comp_row: dict,
    our_entries: list,
    threshold: float = 62.0,
    on_error: Optional[Callable[[str, str], None]] = None,
) -> Optional[dict]:
    """
    Reverse-match one scraped competitor product against the pre-indexed our_entries.
    Returns a result dict (same schema as engine._row output) or None if no match
    is found above `threshold`.

    Phase 0: ``on_error(error_class, detail)`` is invoked for every scoring
    failure so the ledger's ``errors`` counter can track them — no more silent
    ``except: continue``.
    """
    if not our_entries:
        return None
    try:
        from rapidfuzz import fuzz
        from engines.engine import normalize
    except ImportError as _imp:
        if on_error is not None:
            on_error("rt_import_error", str(_imp)[:200])
        return None

    comp_name = str(
        comp_row.get("name") or comp_row.get("المنتج") or
        comp_row.get("product_name") or ""
    ).strip()
    if not comp_name or len(comp_name) < 2:
        return None

    try:
        comp_norm  = normalize(comp_name)
        comp_price = float(str(
            comp_row.get("price") or comp_row.get("السعر") or 0
        ).replace(",", "") or 0)
        comp_store = str(comp_row.get("store") or comp_row.get("المتجر") or "")
        comp_url   = str(comp_row.get("url") or comp_row.get("رابط_المنافس") or "")
        comp_img   = str(comp_row.get("image") or "")
    except Exception as _pe:
        if on_error is not None:
            on_error("rt_parse_error", str(_pe)[:200])
        return None

    best_score  = 0.0
    best_entry  = None
    for entry in our_entries:
        try:
            s = fuzz.WRatio(comp_norm, entry["norm"])
        except Exception as _se:
            # Phase 0: per-entry scoring failure — row is kept (appended to
            # store_rows above), we only lose this one comparison. Log so the
            # ledger errors counter moves.
            if on_error is not None:
                on_error("rt_score_error", str(_se)[:150])
            continue
        if s > best_score:
            best_score = s
            best_entry = entry

    if best_score < threshold or best_entry is None:
        return None

    our_price   = best_entry["price"]
    diff        = round(our_price - comp_price, 2)
    diff_pct    = abs(diff / comp_price * 100) if comp_price > 0 else 0.0

    return {
        "المنتج":           best_entry["name"],
        "معرف_المنتج":      best_entry["id"],
        "السعر":            our_price,
        "منتج_المنافس":     comp_name,
        "سعر_المنافس":      comp_price,
        "الفرق":            diff,
        "نسبة_التطابق":     round(best_score, 1),
        "المنافس":          comp_store,
        "رابط_المنافس":     comp_url,
        "صورة_المنافس":     comp_img,
        "القرار": (
            "🔴 سعر أعلى" if diff > 5
            else ("🟢 سعر أقل" if diff < -5 else "✅ موافق")
        ),
        "الخطورة": (
            "🔴 حرج"    if diff_pct > 20
            else ("🟡 متوسط" if diff_pct > 10 else "🟢 منخفض")
        ),
        "تاريخ_المطابقة":   datetime.now().strftime("%Y-%m-%d"),
        "مصدر_المطابقة":    "realtime_fuzzy",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  GCP Persistence helpers (data/ = mounted Cloud Storage / persistent disk)
# ══════════════════════════════════════════════════════════════════════════════

def _persist_comp_batch(domain: str, rows: List[dict]) -> None:
    """
    Upsert a batch of scraped competitor products to SQLite (WAL mode, thread-safe).
    Called every _PERSIST_BATCH_SIZE rows so data is never only in memory.
    """
    try:
        from utils.db_manager import upsert_competitor_products
        db_rows = [
            {
                "المنتج":      r.get("name") or r.get("المنتج") or "",
                "السعر":       r.get("price") or r.get("السعر") or 0,
                "image_url":   r.get("image") or "",
                "product_url": r.get("url")   or "",
                "brand":       r.get("brand") or "",
                "size":        "",
                "gender":      "للجنسين",
            }
            for r in rows
            if (r.get("name") or r.get("المنتج"))
        ]
        if db_rows:
            upsert_competitor_products(
                domain, db_rows, name_key="المنتج", price_key="السعر"
            )
    except Exception as exc:
        logger.debug("_persist_comp_batch error for %s: %s", domain, exc)


def _persist_results_csv(df: pd.DataFrame, label: str = "pipeline") -> None:
    """
    Save analysis results DataFrame to data/<label>_<timestamp>.csv.
    The data/ directory is mapped to a GCP persistent volume — this survives container restarts.
    """
    if df is None or df.empty:
        return
    try:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(_DATA_DIR, f"results_{label}_{ts}.csv")
        safe = df.copy()
        # Serialise any list/dict columns that would break to_csv
        for col in safe.columns:
            try:
                if safe[col].apply(lambda x: isinstance(x, (list, dict))).any():
                    safe[col] = safe[col].astype(str)
            except Exception:
                pass
        safe.to_csv(path, index=False, encoding="utf-8-sig")
        logger.info("Pipeline: results saved → %s  (%d rows)", path, len(df))
    except Exception as exc:
        logger.warning("_persist_results_csv error: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════════════

async def run_realtime_pipeline(
    our_df: pd.DataFrame,
    store_urls: List[str],
    concurrency: int = 10,
    max_products_per_store: int = 0,
    use_ai: bool = False,
    result_callback: Optional[Callable[[str, Any], None]] = None,
    parallel_stores: int = 5,
    ledger: Optional[CompetitorIntakeLedger] = None,
) -> AsyncGenerator[Tuple[str, Any], None]:
    """
    Async generator that drives the full scrape-then-match pipeline and yields
    structured progress events for the Streamlit UI.

    v2.0: ALL stores scrape simultaneously, per-row real-time analysis in consumer,
    full analysis non-blocking in executor, immediate GCP persistence.

    Phase 0: a ``CompetitorIntakeLedger`` is instantiated per run (unless one
    is passed in). Every scraped competitor row is marked INGESTED before any
    filter runs, and the run-end sweep + invariant report are attached to the
    ``complete`` event's ``audit`` payload.
    """
    # ── Guard: reject obviously bad inputs early ──────────────────────────────
    if our_df is None or our_df.empty:
        logger.warning("run_realtime_pipeline: our_df is empty — aborting")
        yield ("complete", {"df": pd.DataFrame(), "audit": {"error": "our_df_empty"}})
        return

    if not store_urls:
        logger.warning("run_realtime_pipeline: no store URLs — aborting")
        yield ("complete", {"df": pd.DataFrame(), "audit": {"error": "no_store_urls"}})
        return

    # Phase 0 ledger — owned by this run unless injected for tests.
    _owns_ledger = ledger is None
    if ledger is None:
        try:
            ledger = CompetitorIntakeLedger(get_data_db_path("pricing_v18.db"))
        except Exception as _le:
            logger.warning("ledger init failed, falling back to NullLedger: %s", _le)
            ledger = NullLedger()  # type: ignore[assignment]

    def _rt_error_hook(error_class: str, detail: str) -> None:
        try:
            ledger.counters_inc_error(error_class)
        except Exception:
            pass

    from engines.async_scraper import scrape_one_store_streaming, _domain

    # ── Pre-index our products for per-row real-time matching ────────────────
    # Built ONCE here (O(len(our_df))) then accessed read-only by consumer.
    our_entries = _build_our_lookup(our_df)
    logger.info(
        "Pipeline: pre-indexed %d products for real-time matching", len(our_entries)
    )

    # ── High-concurrency shared session (all stores share one connector) ─────
    # 25 stores × 20 concurrent each = 500 total connections.
    # limit_per_host=50 caps per-domain to avoid WAF burst detection.
    connector = aiohttp.TCPConnector(
        ssl=False,
        limit=500,
        limit_per_host=50,
        enable_cleanup_closed=True,
        keepalive_timeout=30,
    )
    shared_session = aiohttp.ClientSession(
        connector=connector,
        connector_owner=True,
        timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
    )

    # ── UNBOUNDED queues (CRITICAL FIX) ──────────────────────────────────────
    # The old maxsize=500 caused a classic producer-consumer deadlock:
    #   25 producers × 50-batch asyncio.gather → 1250 tasks racing to put()
    #   into a 500-slot queue → queue full → all _fetch_one tasks blocked →
    #   consumer task starved → no items consumed → permanent deadlock.
    # Backpressure comes from the per-store Semaphore, NOT from the queue.
    raw_queue:   asyncio.Queue[Tuple[str, Any]] = asyncio.Queue()
    event_queue: asyncio.Queue[Tuple[str, Any]] = asyncio.Queue()

    store_rows:      Dict[str, List[dict]] = {_domain(u): [] for u in store_urls}
    realtime_results: List[dict]           = []   # accumulates per-row RT matches

    # ── Producers: ALL stores start AT THE SAME TIME ─────────────────────────
    # Staggered by 0.5s × index to avoid a simultaneous DNS/TCP burst that
    # triggers WAF rate-limiting at the network layer.
    async def _producer(url: str, stagger_secs: float = 0.0) -> None:
        """Scrape one store; put each row into raw_queue the instant it's scraped."""
        domain = _domain(url)
        if stagger_secs > 0:
            await asyncio.sleep(stagger_secs)
        try:
            async for row in scrape_one_store_streaming(
                url,
                concurrency=concurrency,
                max_products=max_products_per_store,
                client_session=shared_session,
            ):
                await raw_queue.put((domain, row))
        except Exception:
            logger.error(
                "Pipeline producer error for %s: %s",
                domain, traceback.format_exc()[:300],
            )
        finally:
            # Sentinel ALWAYS sent — consumer must never hang waiting for a crashed producer
            await raw_queue.put((domain, None))

    # ── Consumer: pull rows → match in real-time → persist → emit events ─────
    async def _consumer() -> None:
        finished = 0
        total    = len(store_urls)

        while finished < total:
            try:
                # 10-minute timeout: if no row arrives for 10 min, something is badly wrong
                domain, payload = await asyncio.wait_for(
                    raw_queue.get(), timeout=600.0
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Pipeline consumer: 10-min wait on raw_queue — "
                    "%d/%d stores still pending. Aborting consumer.",
                    total - finished, total,
                )
                break

            # ── Store finished (sentinel) ─────────────────────────────────
            if payload is None:
                finished += 1
                store_total = len(store_rows[domain])

                # Flush tail of batch to SQLite
                _tail = store_total % _PERSIST_BATCH_SIZE
                if _tail and store_rows[domain]:
                    _persist_comp_batch(domain, store_rows[domain][-_tail:])

                logger.info(
                    "Pipeline: store done — %s  %d rows  (%d/%d)",
                    domain, store_total, finished, total,
                )
                try:
                    await event_queue.put((
                        "scraping_done",
                        {"store": domain, "total": store_total},
                    ))
                except Exception:
                    pass
                continue

            # ── New product row ───────────────────────────────────────────
            if not isinstance(payload, dict):
                # Phase 0: not a dict means the scraper emitted garbage — log
                # via the ledger error counter so it is visible.
                _rt_error_hook("rt_non_dict_payload",
                               f"type={type(payload).__name__}")
                continue

            store_rows[domain].append(payload)
            count = len(store_rows[domain])

            # Phase 0: ingest this scraped row into the ledger BEFORE any
            # filter / match. Never lose a row without a terminal state.
            try:
                _p_name = str(payload.get("name") or payload.get("المنتج")
                              or payload.get("product_name") or "").strip()
                _p_url = str(payload.get("url") or payload.get("رابط_المنافس")
                             or "").strip()
                if _p_name:
                    ledger.mark_ingested(
                        domain, _p_name, url=_p_url,
                        raw={k: v for k, v in payload.items()
                             if k in ("price", "السعر", "image", "brand")},
                    )
            except Exception as _ing_exc:
                _rt_error_hook("rt_ingest_error", str(_ing_exc)[:200])

            # Batch-persist to SQLite (GCP persistent volume)
            if count % _PERSIST_BATCH_SIZE == 0:
                _persist_comp_batch(
                    domain, store_rows[domain][-_PERSIST_BATCH_SIZE:]
                )

            # ── Per-row real-time reverse match (THE FIX: analysis on-the-fly) ──
            # Wrap in try/except so a single bad product never kills the consumer.
            try:
                rt_result = _reverse_match_one(
                    payload, our_entries, on_error=_rt_error_hook,
                )
                if rt_result is not None:
                    realtime_results.append(rt_result)
                    await event_queue.put(("match_result", {"row": rt_result}))
            except Exception as _me:
                # Phase 0: record the failure — do not swallow silently.
                _rt_error_hook("rt_match_error", str(_me)[:200])
                logger.error("Consumer: per-row match error: %s", _me)

            # Progress event for UI counter
            try:
                await event_queue.put((
                    "scraping_progress",
                    {"store": domain, "count": count, "row": dict(payload)},
                ))
            except Exception:
                pass

    # ── Launch everything ────────────────────────────────────────────────────
    # asyncio.gather is NOT used here for producers — we want each producer
    # to run as its own independent Task so one slow store cannot starve others.
    producer_tasks = [
        asyncio.create_task(_producer(url, stagger_secs=i * 0.5))
        for i, url in enumerate(store_urls)
    ]
    consumer_task = asyncio.create_task(_consumer())

    stores_finished = 0
    total_stores    = len(store_urls)

    try:
        while stores_finished < total_stores:
            event_type, data = await event_queue.get()
            if result_callback is not None:
                try:
                    result_callback(event_type, data)
                except Exception:
                    logger.debug(
                        "result_callback error: %s",
                        traceback.format_exc()[:150],
                    )
            yield (event_type, data)
            if event_type == "scraping_done":
                stores_finished += 1
                logger.info(
                    "Pipeline: %s done — %d rows  (%d/%d stores)",
                    data["store"], data["total"], stores_finished, total_stores,
                )
    finally:
        for t in producer_tasks:
            if not t.done():
                t.cancel()
        if not consumer_task.done():
            consumer_task.cancel()
        await asyncio.gather(*producer_tasks, consumer_task, return_exceptions=True)
        if not shared_session.closed:
            await shared_session.close()

    # ── Phase 2: Build competitor DataFrames from accumulated rows ───────────
    comp_dfs: Dict[str, pd.DataFrame] = {}
    total_rows       = 0
    finished_stores: List[str] = []

    for domain, rows in store_rows.items():
        if rows:
            comp_dfs[domain] = pd.DataFrame(rows)
            total_rows      += len(rows)
            finished_stores.append(domain)
            logger.info(
                "Pipeline: built comp_df for %s  (%d rows)", domain, len(rows)
            )

    if not comp_dfs:
        logger.warning("Pipeline: no competitor data scraped from any store")
        # Fall back to real-time results if we got any
        if realtime_results:
            yield ("complete", {
                "df":    pd.DataFrame(realtime_results),
                "audit": {"error": "no_competitor_data_full", "fallback": "realtime_fuzzy",
                          "total_input": len(realtime_results)},
            })
        else:
            yield ("complete", {
                "df":    pd.DataFrame(),
                "audit": {"error": "no_competitor_data", "total_input": 0},
            })
        return

    yield ("matching_start", {"total_rows": total_rows, "stores": finished_stores})
    logger.info("Pipeline: starting full analysis — %d competitor rows", total_rows)

    # ── Phase 3: Full engine analysis — NON-BLOCKING via ThreadPoolExecutor ──
    # run_full_analysis is a CPU-bound sync function that can take minutes for large
    # datasets. Running it directly in the async generator would BLOCK the entire
    # event loop, killing all aiohttp connections and the Streamlit WebSocket.
    loop = asyncio.get_running_loop()
    try:
        from engines.engine import run_full_analysis

        results_df, audit = await loop.run_in_executor(
            None,
            functools.partial(
                run_full_analysis,
                our_df,
                comp_dfs,
                None,    # progress_callback
                use_ai,  # use_ai
                ledger,  # Phase 0: share the run-level ledger
            ),
        )
        logger.info(
            "Pipeline: full analysis complete — %d result rows  (audit=%s)",
            len(results_df), audit,
        )
    except Exception:
        tb = traceback.format_exc()
        logger.error("Pipeline: full analysis failed: %s", tb[:400])
        # Fall back to real-time fuzzy results collected during scraping
        results_df = pd.DataFrame(realtime_results) if realtime_results else pd.DataFrame()
        audit = {
            "error":    "matching_failed",
            "traceback": tb[:200],
            "fallback": "realtime_fuzzy",
            "rt_rows":  len(realtime_results),
        }

    # ── Phase 4: Persist results to GCP data/ volume ─────────────────────────
    # Fire-and-forget: run in executor so we don't delay emitting "complete".
    try:
        asyncio.ensure_future(
            loop.run_in_executor(
                None,
                functools.partial(_persist_results_csv, results_df, "pipeline"),
            )
        )
    except Exception as _pe:
        logger.debug("Async persist scheduling error: %s", _pe)

    # ── Phase 0: finalize ledger (sweep + invariant) ─────────────────────────
    # run_full_analysis already swept + reported; we re-sweep here to catch any
    # rows that were ingested into the ledger by the consumer but never made
    # it into comp_dfs (e.g. if the full analysis failed early).
    try:
        swept = ledger.sweep_untransitioned(
            default_state=REJECTED_LOW_CONFIDENCE,
            reason_code="rt_not_reached_full_analysis",
        )
        ok, report = ledger.check_invariant()
        if isinstance(audit, dict):
            audit["ledger"] = report
            audit["ledger_sweep_count"] = swept
        else:
            audit = {"ledger": report, "ledger_sweep_count": swept,
                     "original_audit": audit}
        if not ok:
            logger.error("realtime pipeline invariant FAILED: %s", report)
    except Exception as _lfe:
        logger.warning("ledger finalize error (non-fatal): %s", _lfe)
    finally:
        if _owns_ledger and hasattr(ledger, "close"):
            try:
                ledger.close()
            except Exception:
                pass

    yield ("complete", {"df": results_df, "audit": audit})


# ══════════════════════════════════════════════════════════════════════════════
#  Sync convenience wrapper (for non-async callers / testing / Streamlit threads)
# ══════════════════════════════════════════════════════════════════════════════

def run_realtime_pipeline_sync(
    our_df: pd.DataFrame,
    store_urls: List[str],
    concurrency: int = 10,
    max_products_per_store: int = 0,
    use_ai: bool = False,
    on_event: Optional[Any] = None,
    result_callback: Optional[Callable[[str, Any], None]] = None,
    parallel_stores: int = 5,
) -> pd.DataFrame:
    """
    Synchronous wrapper around run_realtime_pipeline().
    Runs a fresh event loop (safe from Streamlit daemon threads or CLI scripts).
    Returns the final results DataFrame.

    Args:
        on_event:        legacy alias for result_callback.
        result_callback: optional callable(event_type, data) fired on every event.
    """
    _cb = result_callback if result_callback is not None else on_event

    async def _run() -> pd.DataFrame:
        result_df = pd.DataFrame()
        async for event_type, data in run_realtime_pipeline(
            our_df,
            store_urls,
            concurrency=concurrency,
            max_products_per_store=max_products_per_store,
            use_ai=use_ai,
            result_callback=_cb,
            parallel_stores=parallel_stores,
        ):
            if event_type == "complete":
                result_df = data.get("df", pd.DataFrame())
        return result_df

    return asyncio.run(_run())
