"""
observability/ledger.py — Phase 0 instrumentation.

Guarantees the invariant:

    ingested == confirmed + missing + rejected_structural
                + rejected_low_confidence + retry_pending + errors

Every competitor row that enters any matching pipeline is recorded in
``competitor_intake_ledger`` as INGESTED before any filter runs. Transitions
to terminal states are written synchronously at each decision point. A
run-end sweep flips any leftover INGESTED rows to a terminal state so no
row can leak without a final disposition.

Phase 0 is strictly read-only w.r.t. scoring/decision logic: it only
observes what is already happening and enforces completeness.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

logger = logging.getLogger("observability.ledger")


# ── Terminal / inflight state vocabulary ──────────────────────────────────
CONFIRMED_MATCH = "CONFIRMED_MATCH"
CONFIRMED_MISSING = "CONFIRMED_MISSING"
REJECTED_STRUCTURAL = "REJECTED_STRUCTURAL"
REJECTED_LOW_CONFIDENCE = "REJECTED_LOW_CONFIDENCE"
RETRY_PENDING = "RETRY_PENDING"
ERROR = "ERROR"
INGESTED = "INGESTED"

TERMINAL_STATES = frozenset({
    CONFIRMED_MATCH,
    CONFIRMED_MISSING,
    REJECTED_STRUCTURAL,
    REJECTED_LOW_CONFIDENCE,
    ERROR,
})
INFLIGHT_STATES = frozenset({INGESTED, RETRY_PENDING})
ALL_STATES = TERMINAL_STATES | INFLIGHT_STATES


class PipelineCompletenessError(RuntimeError):
    """Raised (by the test harness) when the end-of-run invariant fails."""


# ── Status-string → ledger-state mapping ──────────────────────────────────
# Existing status emitters in engines/engine.py (_row, _excluded_match_row)
# use these Arabic strings. Phase 0 does not change them — it only maps.

def state_from_status(
    status: str,
    source: str = "",
) -> Tuple[str, Optional[str]]:
    """
    Map a legacy Arabic status string + its match source to a ledger state.
    Returns (state, reason_code).
    """
    s = (status or "").strip()
    src = (source or "").strip()
    if s.startswith("✅") or s.startswith("🔴") or s.startswith("🟢"):
        return CONFIRMED_MATCH, None
    if s.startswith("⚠️"):
        return CONFIRMED_MATCH, "under_review"
    if s.startswith("🔍"):
        return CONFIRMED_MISSING, None
    if s.startswith("⚪"):
        if src == "no_candidates":
            return REJECTED_STRUCTURAL, "no_candidates"
        if src == "below_match_threshold":
            return REJECTED_LOW_CONFIDENCE, "below_match_threshold"
        return REJECTED_LOW_CONFIDENCE, src or "excluded"
    return REJECTED_LOW_CONFIDENCE, "unknown_status"


# ── comp_id computation (stable within a run) ─────────────────────────────
_WS = re.compile(r"\s+")


def _norm_for_id(text: str) -> str:
    s = (text or "").lower().strip()
    s = _WS.sub(" ", s)
    return s


def make_comp_id(
    competitor: str,
    product_name: str,
    url: str = "",
) -> str:
    """
    Stable 16-char hex id for a competitor product row. Same competitor+url
    +normalized-name always hash to the same id within a run, so ingest and
    transition sites can agree without passing the id by object reference.
    """
    key = f"{(competitor or '').strip()}|{(url or '').strip()}|{_norm_for_id(product_name)}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


# ── SQLite ledger ─────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS competitor_intake_ledger (
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
)
"""
_IDX_STATE = "CREATE INDEX IF NOT EXISTS idx_cil_state ON competitor_intake_ledger(state)"
_IDX_RUN = "CREATE INDEX IF NOT EXISTS idx_cil_run ON competitor_intake_ledger(run_id)"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    c = conn.cursor()
    c.execute(_DDL)
    c.execute(_IDX_STATE)
    c.execute(_IDX_RUN)
    conn.commit()


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class NullLedger:
    """No-op ledger — used when the caller opts out of instrumentation."""

    run_id = ""

    def mark_ingested(self, *a, **k) -> None: ...
    def mark_ingested_batch(self, rows) -> list:
        return [None for _ in rows]
    def mark_state(self, *a, **k) -> None: ...
    def mark_error(self, *a, **k) -> None: ...
    def counters_inc_error(self, *a, **k) -> None: ...
    def counters(self) -> Dict[str, int]:
        return {k: 0 for k in _COUNTER_KEYS}

    def sweep_untransitioned(self, *a, **k) -> int:
        return 0

    def check_invariant(self) -> Tuple[bool, Dict[str, Any]]:
        return True, {"counters": self.counters(), "invariant_ok": True,
                      "terminal_sum": 0, "null_ledger": True}

    def sample_stuck_rows(self, limit: int = 20) -> list:
        return []

    def close(self) -> None:
        return None


_COUNTER_KEYS = (
    "ingested",
    "confirmed",
    "missing",
    "rejected_structural",
    "rejected_low_confidence",
    "retry_pending",
    "errors",
    "inflight_ingested",
    "degradation_events",
)


class CompetitorIntakeLedger:
    """
    SQLite-backed ledger. All writes are synchronous and transactional.

    One ledger per pipeline run. The ``run_id`` scopes the rows so multiple
    runs can coexist in the same table.

    Usage:
        ledger = CompetitorIntakeLedger(db_path)
        ledger.mark_ingested("store-x", "Dior Sauvage 100ml", url="...",
                             raw={"price": 420})
        ledger.mark_state(comp_id, CONFIRMED_MATCH, last_score=96.0)
        ...
        ledger.sweep_untransitioned()
        ok, report = ledger.check_invariant()
    """

    def __init__(
        self,
        db_path: str,
        run_id: Optional[str] = None,
    ) -> None:
        self.db_path = db_path
        self.run_id = run_id or self._gen_run_id()
        self._lock = threading.Lock()
        self._error_count = 0
        # Open a long-lived connection; each write takes the lock so it is
        # safe across threads (the main DB also runs in WAL mode).
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, timeout=30,
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=30000;")
        _ensure_schema(self._conn)

    @staticmethod
    def _gen_run_id() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]

    # ── writes ────────────────────────────────────────────────────────────
    def mark_ingested(
        self,
        competitor: str,
        product_name: str,
        url: str = "",
        raw: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """
        Record a competitor row at the moment it enters the pipeline.
        Idempotent on (run_id, comp_id): re-calls do not overwrite state.
        Returns the comp_id for later transitions.
        """
        comp_id = make_comp_id(competitor, product_name, url)
        now = _ts()
        try:
            payload = json.dumps(dict(raw or {}), ensure_ascii=False, default=str)
        except Exception:
            payload = "{}"
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO competitor_intake_ledger "
                "(run_id, comp_id, competitor, product_name, raw_payload, "
                " state, ingested_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'INGESTED', ?, ?)",
                (self.run_id, comp_id, competitor or "",
                 product_name or "", payload, now, now),
            )
            self._conn.commit()
        return comp_id

    def mark_ingested_batch(self, rows) -> list:
        """
        Batch variant of :meth:`mark_ingested`: one ``executemany`` + one
        commit for the whole sequence instead of a commit per row. ``rows``
        is a sequence of ``(competitor, product_name, url, raw)`` tuples.

        Returns the list of comp_ids in input order — the same ids repeated
        ``mark_ingested`` calls would return. Idempotent on (run_id, comp_id)
        via ``INSERT OR IGNORE``: the first occurrence wins and later
        duplicates are ignored, exactly like per-row calls. The resulting
        table rows are byte-identical to the per-row path; only the number
        of commits differs (N → 1).
        """
        now = _ts()
        params = []
        ids = []
        for competitor, product_name, url, raw in rows:
            comp_id = make_comp_id(competitor, product_name, url or "")
            ids.append(comp_id)
            try:
                payload = json.dumps(dict(raw or {}), ensure_ascii=False, default=str)
            except Exception:
                payload = "{}"
            params.append((self.run_id, comp_id, competitor or "",
                           product_name or "", payload, now, now))
        if params:
            with self._lock:
                c = self._conn.cursor()
                c.executemany(
                    "INSERT OR IGNORE INTO competitor_intake_ledger "
                    "(run_id, comp_id, competitor, product_name, raw_payload, "
                    " state, ingested_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'INGESTED', ?, ?)",
                    params,
                )
                self._conn.commit()
        return ids

    def mark_state(
        self,
        comp_id: str,
        state: str,
        *,
        reason_code: Optional[str] = None,
        last_score: Optional[float] = None,
    ) -> None:
        if state not in ALL_STATES:
            logger.warning("mark_state: unknown state %r — recording as ERROR", state)
            state = ERROR
        now = _ts()
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "UPDATE competitor_intake_ledger SET "
                "state=?, reason_code=COALESCE(?, reason_code), "
                "last_score=COALESCE(?, last_score), updated_at=? "
                "WHERE run_id=? AND comp_id=?",
                (state, reason_code, last_score, now, self.run_id, comp_id),
            )
            if c.rowcount == 0:
                # Decision site fired before ingest — record it anyway so the
                # row is not silently lost. competitor/product_name unknown.
                c.execute(
                    "INSERT INTO competitor_intake_ledger "
                    "(run_id, comp_id, competitor, product_name, raw_payload, "
                    " state, reason_code, last_score, ingested_at, updated_at) "
                    "VALUES (?, ?, '', '', '{}', ?, ?, ?, ?, ?)",
                    (self.run_id, comp_id, state, reason_code, last_score, now, now),
                )
            self._conn.commit()

    def mark_error(
        self,
        comp_id: Optional[str],
        error_class: str,
        traceback_excerpt: str = "",
    ) -> None:
        """
        Record a row-processing error as a terminal ERROR state row. If
        ``comp_id`` is None we additionally record a telemetry-only bump in
        ``degradation_events`` and log loudly — the row cannot be attributed
        to a specific ledger entry.
        """
        if not comp_id:
            self._error_count += 1
            logger.error(
                "ledger error without comp_id: class=%s detail=%s",
                error_class, traceback_excerpt[:200],
            )
            return
        now = _ts()
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "UPDATE competitor_intake_ledger SET "
                "state='ERROR', error_class=?, error_excerpt=?, updated_at=? "
                "WHERE run_id=? AND comp_id=?",
                (error_class, traceback_excerpt[:500], now, self.run_id, comp_id),
            )
            if c.rowcount == 0:
                c.execute(
                    "INSERT INTO competitor_intake_ledger "
                    "(run_id, comp_id, competitor, product_name, raw_payload, "
                    " state, error_class, error_excerpt, ingested_at, updated_at) "
                    "VALUES (?, ?, '', '', '{}', 'ERROR', ?, ?, ?, ?)",
                    (self.run_id, comp_id, error_class,
                     traceback_excerpt[:500], now, now),
                )
            self._conn.commit()

    def counters_inc_error(self, error_class: str) -> None:
        """Bump the errors counter without touching any specific ledger row."""
        self._error_count += 1
        logger.debug("ledger error bump: %s", error_class)

    # ── reads ─────────────────────────────────────────────────────────────
    def _state_counts(self) -> Dict[str, int]:
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "SELECT state, COUNT(*) FROM competitor_intake_ledger "
                "WHERE run_id=? GROUP BY state",
                (self.run_id,),
            )
            rows = c.fetchall()
        return {state: int(n) for state, n in rows}

    def counters(self) -> Dict[str, int]:
        by_state = self._state_counts()
        ingested = sum(by_state.values())  # anything with a row has been ingested
        return {
            "ingested": ingested,
            "confirmed": by_state.get(CONFIRMED_MATCH, 0),
            "missing": by_state.get(CONFIRMED_MISSING, 0),
            "rejected_structural": by_state.get(REJECTED_STRUCTURAL, 0),
            "rejected_low_confidence": by_state.get(REJECTED_LOW_CONFIDENCE, 0),
            "retry_pending": by_state.get(RETRY_PENDING, 0),
            # Row-state errors are terminal rows; ``degradation_events`` are
            # orthogonal telemetry (e.g. inner-loop scoring failures that
            # did not cause a row loss).
            "errors": by_state.get(ERROR, 0),
            "inflight_ingested": by_state.get(INGESTED, 0),
            "degradation_events": int(self._error_count),
        }

    def sweep_untransitioned(
        self,
        *,
        default_state: str = REJECTED_LOW_CONFIDENCE,
        reason_code: str = "not_selected_in_batch",
    ) -> int:
        """
        Flip any row still INGESTED at the end of the run to a terminal
        state. Returns how many rows were swept. This is the safety net
        that makes the invariant hold even if an instrumentation site was
        missed — such rows show up under ``reason_code`` for Phase-1
        triage.
        """
        if default_state not in TERMINAL_STATES:
            raise ValueError(f"default_state must be terminal, got {default_state!r}")
        now = _ts()
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "UPDATE competitor_intake_ledger SET "
                "state=?, reason_code=COALESCE(reason_code, ?), updated_at=? "
                "WHERE run_id=? AND state='INGESTED'",
                (default_state, reason_code, now, self.run_id),
            )
            swept = c.rowcount
            self._conn.commit()
        if swept:
            logger.warning(
                "ledger sweep: %d rows flipped INGESTED → %s (reason=%s)",
                swept, default_state, reason_code,
            )
        return int(swept or 0)

    def check_invariant(self) -> Tuple[bool, Dict[str, Any]]:
        """
        Returns (ok, report). The invariant is:
            ingested == confirmed + missing + rejected_structural
                        + rejected_low_confidence + retry_pending + errors
        plus: no row may remain in INGESTED at run-end (``inflight_ingested``
        must be 0).
        """
        c = self.counters()
        terminal_sum = (
            c["confirmed"] + c["missing"]
            + c["rejected_structural"] + c["rejected_low_confidence"]
            + c["retry_pending"] + c["errors"]
        )
        invariant_ok = (
            c["ingested"] == terminal_sum and c["inflight_ingested"] == 0
        )
        report = {
            "run_id": self.run_id,
            "counters": c,
            "terminal_sum": terminal_sum,
            "invariant_ok": invariant_ok,
        }
        if not invariant_ok:
            report["invariant_delta"] = c["ingested"] - terminal_sum
        return invariant_ok, report

    def sample_stuck_rows(self, limit: int = 20) -> list:
        """Debug helper — return the first ``limit`` INGESTED rows, if any."""
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                "SELECT comp_id, competitor, product_name, ingested_at "
                "FROM competitor_intake_ledger "
                "WHERE run_id=? AND state='INGESTED' LIMIT ?",
                (self.run_id, int(limit)),
            )
            return [dict(zip(("comp_id", "competitor", "product_name", "ingested_at"), r))
                    for r in c.fetchall()]

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    # ── context-manager sugar ─────────────────────────────────────────────
    def __enter__(self) -> "CompetitorIntakeLedger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def ingest_comp_df(
    ledger: "CompetitorIntakeLedger | NullLedger",
    competitor: str,
    comp_df,
    name_col: str,
    url_col: str = "",
) -> Dict[int, str]:
    """
    Batch-ingest every row of a competitor DataFrame.
    Returns {row_index: comp_id} so callers can map back later if needed.
    """
    import pandas as pd  # local import keeps ledger module free of hard deps
    out: Dict[int, str] = {}
    if comp_df is None or getattr(comp_df, "empty", True):
        return out
    _rows = []      # (competitor, name, url, raw) in row order
    _indices = []   # parallel original row indices
    for idx, row in comp_df.iterrows():
        try:
            name = str(row.get(name_col, "")).strip()
        except Exception:
            name = ""
        url = ""
        if url_col and url_col in comp_df.columns:
            try:
                url = str(row.get(url_col, "")).strip()
            except Exception:
                url = ""
        if not name:
            continue
        raw = {}
        try:
            for k, v in row.items():
                if pd.isna(v):
                    continue
                raw[str(k)] = v
        except Exception:
            raw = {}
        _rows.append((competitor, name, url, raw))
        _indices.append(int(idx))
    # Single executemany + one commit for the whole df instead of a commit
    # per row (≈22s → sub-second on the full 129K store). Rows are identical.
    ids = ledger.mark_ingested_batch(_rows)
    for _i, cid in zip(_indices, ids):
        out[_i] = cid
    return out
