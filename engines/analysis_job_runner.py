"""
تشغيل التحليل والمطابقة في خيط خلفي — مشترك بين app.py وصفحات الرفع.
يستدعي run_full_analysis (ومعها كاش match_cache_v21.db عبر محرك المطابقة).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import traceback

import pandas as pd

from engines.engine import run_full_analysis, smart_missing_barrier
from engines.reconciliation_engine import (
    merge_reconciliation_into_audit,
    reconcile_competitor_upload,
)
from utils.data_helpers import (
    merge_missing_products_dataframes,
    merge_price_analysis_dataframes,
    restore_results_from_json,
    safe_results_for_json,
)
from utils.helpers import safe_float
from utils.db_manager import log_analysis, save_job_progress, upsert_price_history

_logger = logging.getLogger(__name__)

# حد أقصى لعمر وظيفة التحليل — 2 ساعة للأجهزة البطيئة
_ANALYSIS_JOB_TIMEOUT_SEC = int(
    os.environ.get("ANALYSIS_JOB_TIMEOUT_SEC", "7200")
)

# نمط أسماء الـ placeholder الوهمية («منتج P12345» / «P1172895619» …).
_PHANTOM_NAME_RE = re.compile(r"^(?:منتج\s+)?[Pp][A-Za-z0-9_\-]*$")


def _is_phantom_name(name: object) -> bool:
    try:
        s = str(name or "").strip()
    except Exception:
        return False
    if not s:
        return True
    return bool(_PHANTOM_NAME_RE.match(s))


def _strip_phantom_rows(
    df: "pd.DataFrame",
    name_cols: tuple[str, ...] = ("المنتج", "product_name", "name"),
    price_cols: tuple[str, ...] = ("السعر", "price", "سعر_المنافس"),
) -> "pd.DataFrame":
    """يُزيل الصفوف الوهمية (اسم placeholder + سعر ≤ 0) قبل بدء المطابقة.

    هذه الحلقة دفاعية: حتى لو تسلّل منتج وهمي إلى قاعدة البيانات، لن يدخل
    محرك التحليل ويسبّب Deadlock.
    """
    if df is None or df.empty:
        return df
    try:
        name_col = next((c for c in name_cols if c in df.columns), None)
        price_col = next((c for c in price_cols if c in df.columns), None)
        if name_col is None:
            return df
        mask_phantom_name = df[name_col].apply(_is_phantom_name)
        if price_col is not None:
            mask_zero_price = pd.to_numeric(
                df[price_col], errors="coerce"
            ).fillna(0) <= 0
            mask = mask_phantom_name & mask_zero_price
        else:
            mask = mask_phantom_name
        dropped = int(mask.sum())
        if dropped:
            _logger.warning(
                "analysis_job_runner: تم تخطي %d صف وهمي (اسم placeholder + سعر ≤ 0) قبل التحليل.",
                dropped,
            )
            return df.loc[~mask].reset_index(drop=True)
        return df
    except Exception:
        _logger.debug("_strip_phantom_rows failed", exc_info=True)
        return df


def run_analysis_background_job(
    job_id: str,
    our_df: pd.DataFrame,
    comp_dfs: dict,
    our_file_name: str,
    comp_names: str,
    merge_previous: bool = False,
    prev_analysis_records: list | None = None,
    prev_missing_records: list | None = None,
) -> None:
    """تعمل في thread منفصل — تحفظ النتائج كل 25 منتجاً مع حماية من الأخطاء.

    دفاعات جديدة:
      - يتخطى كل المنتجات الوهمية (اسم «منتج P…» + سعر 0) قبل بدء المطابقة،
        حتى لا تُعلّق محرك التحليل عند تسرّب صفوف فاسدة.
      - watchdog timer يُنهي المهمة تلقائياً إذا تجاوزت
        ANALYSIS_JOB_TIMEOUT_SEC بدل أن تبقى «running» للأبد وتقفل الواجهة.
    """
    # تنظيف المدخلات من المنتجات الوهمية — يحمي التحليل من deadlock
    our_df = _strip_phantom_rows(our_df)
    if isinstance(comp_dfs, dict):
        comp_dfs = {
            k: _strip_phantom_rows(v)
            for k, v in comp_dfs.items()
            if isinstance(v, pd.DataFrame)
        }

    total = len(our_df)
    processed = 0
    _last_save = [0]

    # Watchdog: يُنهي المهمة إذا تجاوزت الحد الأقصى.
    _timed_out = threading.Event()

    def _on_timeout() -> None:
        _timed_out.set()
        try:
            save_job_progress(
                job_id, total, processed,
                [],
                f"error: انتهت مهلة التحليل ({_ANALYSIS_JOB_TIMEOUT_SEC}s) — "
                "تم إلغاء المهمة تلقائياً لتحرير الواجهة.",
                our_file_name, comp_names,
            )
        except Exception:
            traceback.print_exc()
        _logger.warning(
            "analysis_job_runner[%s]: watchdog timeout after %ds — marked as error.",
            job_id, _ANALYSIS_JOB_TIMEOUT_SEC,
        )

    _watchdog = threading.Timer(_ANALYSIS_JOB_TIMEOUT_SEC, _on_timeout)
    _watchdog.daemon = True
    _watchdog.start()

    def progress_cb(pct, current_results):
        nonlocal processed
        processed = int(pct * total)
        gap = processed - _last_save[0]
        if gap < 50 and processed < total:
            return  # لا حفظ — لم يمر 50 منتج بعد
        _last_save[0] = processed
        try:
            # حفظ كامل كل 200 منتج أو عند الاكتمال
            if gap >= 200 or processed >= total:
                safe_res = safe_results_for_json(current_results)
            else:
                safe_res = []  # حفظ خفيف (تقدم فقط)
            save_job_progress(
                job_id, total, processed,
                safe_res,
                "running",
                our_file_name, comp_names,
            )
        except Exception:
            pass

    analysis_df = pd.DataFrame()
    missing_df = pd.DataFrame()
    audit_stats: dict = {}

    try:
        analysis_df, audit_stats = run_full_analysis(
            our_df, comp_dfs,
            progress_callback=progress_cb,
            use_ai=False,  # إصلاح المهلة: مطابقة حتمية سريعة (AI داخل الحلقة كان يسبب توقّف 7200s).
                           # النطاق الرمادي → مراجعة؛ AI يُشغَّل يدوياً على قسم المراجعة لاحقاً.
        )
    except Exception as e:
        traceback.print_exc()
        save_job_progress(
            job_id, total, processed,
            [], f"error: تحليل المقارنة فشل — {str(e)[:200]}",
            our_file_name, comp_names,
        )
        _watchdog.cancel()
        return

    if _timed_out.is_set():
        # انقضت المهلة أثناء run_full_analysis — لا تستمر كي لا نكتب فوق
        # حالة error التي سجّلها الـ watchdog.
        _watchdog.cancel()
        return

    try:
        for _, row in analysis_df.iterrows():
            if safe_float(row.get("نسبة_التطابق", 0)) > 0:
                upsert_price_history(
                    str(row.get("المنتج", "")),
                    str(row.get("المنافس", "")),
                    safe_float(row.get("سعر_المنافس", 0)),
                    safe_float(row.get("السعر", 0)),
                    safe_float(row.get("الفرق", 0)),
                    safe_float(row.get("نسبة_التطابق", 0)),
                    str(row.get("القرار", "")),
                )
    except Exception:
        pass

    rec = None
    missing_df = pd.DataFrame()
    try:
        rec = reconcile_competitor_upload(our_df, comp_dfs)
        missing_df = smart_missing_barrier(rec.new_products_df, our_df)
        rec.apply_smart_barrier_adjustment(missing_df)
        audit_stats = merge_reconciliation_into_audit(audit_stats, rec)
        if rec.failed_df is not None and not rec.failed_df.empty:
            data_dir = os.environ.get("DATA_DIR", "data")
            os.makedirs(data_dir, exist_ok=True)
            fp = os.path.join(data_dir, f"failed_rows_{job_id}.csv")
            try:
                rec.failed_df.to_csv(fp, index=False, encoding="utf-8-sig")
                audit_stats["reconciliation_failed_csv_path"] = fp
            except Exception:
                traceback.print_exc()
    except Exception:
        traceback.print_exc()
        missing_df = pd.DataFrame()
        try:
            from engines.engine import find_missing_products

            raw_missing_df = find_missing_products(our_df, comp_dfs)
            missing_df = smart_missing_barrier(raw_missing_df, our_df)
        except Exception:
            traceback.print_exc()

    if merge_previous and prev_analysis_records:
        try:
            prev_adf = pd.DataFrame(restore_results_from_json(prev_analysis_records))
            if not prev_adf.empty:
                analysis_df = merge_price_analysis_dataframes(prev_adf, analysis_df)
        except Exception:
            traceback.print_exc()
    if merge_previous and prev_missing_records:
        try:
            prev_m = pd.DataFrame(prev_missing_records)
            if not prev_m.empty:
                missing_df = merge_missing_products_dataframes(prev_m, missing_df)
        except Exception:
            traceback.print_exc()

    if _timed_out.is_set():
        _watchdog.cancel()
        return

    # ── تصالح حفظ البيانات: منتج منافس تمّت مطابقته في بطاقة سعرية مؤكّدة
    #    (🔴/🟢/✅) لا يُعلَن «مفقوداً» أيضاً. يحلّ تعارض المسارين (مطابقة
    #    run_full_analysis مقابل reconcile) الذي ينتج «مطابق ومفقود معاً». ──
    try:
        _card_dec = {"🔴 سعر أعلى", "🟢 سعر أقل", "✅ موافق"}
        if (not missing_df.empty and "منتج_المنافس" in missing_df.columns
                and not analysis_df.empty
                and {"منتج_المنافس", "القرار"} <= set(analysis_df.columns)):
            _cards = analysis_df[analysis_df["القرار"].astype(str).isin(_card_dec)]
            _matched_keys = {
                k for k in _cards["منتج_المنافس"].fillna("").astype(str)
                .str.strip().str.lower().tolist()
                if k and k not in ("nan", "—")
            }
            if _matched_keys:
                _mk = missing_df["منتج_المنافس"].fillna("").astype(str).str.strip().str.lower()
                _before = len(missing_df)
                missing_df = missing_df[~_mk.isin(_matched_keys)].reset_index(drop=True)
                _removed = _before - len(missing_df)
                if _removed:
                    audit_stats["missing_dedup_vs_matched"] = _removed
                    _logger.info(
                        "تصالح المفقودات: أُزيل %d منتج مطابَق من قائمة المفقودة (حفظ البيانات).",
                        _removed,
                    )
    except Exception:
        _logger.debug("missing-vs-matched reconcile failed", exc_info=True)

    try:
        safe_records = safe_results_for_json(analysis_df.to_dict("records"))
        safe_missing = missing_df.to_dict("records") if not missing_df.empty else []

        save_job_progress(
            job_id, total, total,
            safe_records,
            "done",
            our_file_name, comp_names,
            missing=safe_missing,
            audit_stats=audit_stats,
        )
        log_analysis(
            our_file_name, comp_names, total,
            int((analysis_df.get("نسبة_التطابق", pd.Series(dtype=float)) > 0).sum()),
            len(missing_df),
        )
    except Exception as e:
        traceback.print_exc()
        try:
            save_job_progress(
                job_id, total, total,
                safe_results_for_json(analysis_df.to_dict("records")),
                "done",
                our_file_name, comp_names,
                missing=[],
                audit_stats=audit_stats,
            )
        except Exception:
            save_job_progress(
                job_id, total, processed,
                [], f"error: فشل الحفظ النهائي — {str(e)[:200]}",
                our_file_name, comp_names,
            )
    finally:
        _watchdog.cancel()
