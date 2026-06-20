"""
محرك المحاسبة والمطابقة الصارم لصفوف ملفات المنافسين.

- معادلة المحاسبة: إجمالي_المدخلات = متطابق + جديد_للتصدير + تالف
- لا dropna صامت: كل صف تالف يُحفظ في failed_rows_log (DataFrame).
- مطابقة متعددة الطبقات: product_url → SKU → اسم مطبّع تام → RapidFuzz ≥95% + تحقق هيكلي (حجم/نوع).
"""
from __future__ import annotations

import io
import logging
import re
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("ReconciliationEngine")

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process as rf_process

from engines.closed_loop_engine import (
    _check_structural_constraints,
    normalize_text,
)

# كلمات تسويقية / ضجيج تُزال قبل المقارنة النصية (لا تُحذف من الاسم الأصلي المعروض)
_MARKETING_NOISE_RE = re.compile(
    r"(?:^|\s)(?:تستر|تيستر|tester|أصلي|اصلي|original|عرض|تخفيض|خصم|مميز|"
    r"limited\s*edition|limited|جديد|new|وفر|save|off)(?:\s|$)",
    re.IGNORECASE | re.UNICODE,
)

_FUZZY_MIN: float = 95.0
_CDIST_CHUNK: int = 400


def _cell_str(r: pd.Series, col: Optional[str]) -> str:
    if not col or col not in r.index:
        return ""
    v = r.get(col, "")
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    if s.lower() in ("nan", "none", "<na>"):
        return ""
    return s


def _strip_marketing_noise(raw: str) -> str:
    t = str(raw or "")
    t = _MARKETING_NOISE_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _normalize_product_url(u: Any) -> str:
    s = str(u or "").strip().lower()
    if not s.startswith("http"):
        return ""
    s = s.split("?", 1)[0].strip().rstrip("/")
    return s


def _norm_sku_key(s: Any) -> str:
    if s is None:
        return ""
    t = str(s).strip()
    if not t or t.lower() in ("nan", "none", "0", "0.0"):
        return ""
    try:
        return str(int(float(t)))
    except (ValueError, TypeError):
        return t


@dataclass
class ReconciliationReport:
    total_read: int = 0
    matched: int = 0
    new_ready: int = 0
    corrupted: int = 0
    balance_ok: bool = True
    warning_message: str = ""
    failed_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    new_products_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_read": self.total_read,
            "matched": self.matched,
            "new_ready": self.new_ready,
            "corrupted": self.corrupted,
            "balance_ok": self.balance_ok,
            "warning_message": self.warning_message,
            "diagnostics": self.diagnostics,
        }

    def apply_smart_barrier_adjustment(self, missing_after_barrier: pd.DataFrame) -> None:
        """
        بعد smart_missing_barrier: الصفوف المُزالة تُعاد تصنيفها كـ «متطابقة» مع كتالوجنا
        لإبقاء معادلة المحاسبة صحيحة.
        """
        before_n = int(self.new_ready)
        after_n = int(len(missing_after_barrier)) if missing_after_barrier is not None else 0
        extra_matched = max(0, before_n - after_n)
        self.matched += extra_matched
        self.new_ready = after_n
        if missing_after_barrier is not None:
            self.new_products_df = missing_after_barrier.reset_index(drop=True)
        _chk = self.matched + self.new_ready + self.corrupted
        _msg = ""
        if _chk != self.total_read:
            _msg = (
                f"محاسبة بعد الحاجز: المدخل={self.total_read} ≠ "
                f"{_chk} (متطابق={self.matched}+جديد={self.new_ready}+تالف={self.corrupted})"
            )
            warnings.warn(_msg, UserWarning, stacklevel=2)
        if _chk != self.total_read:
            _log.warning("⚠️ %s", _msg or "انتهاك معادلة المحاسبة بعد الحاجز")


def _known_brands_list() -> List[str]:
    try:
        from config import KNOWN_BRANDS

        return [str(b) for b in (KNOWN_BRANDS or []) if b]
    except Exception:
        return []


def _build_our_indexes(our_df: pd.DataFrame) -> Tuple[List[str], Dict[str, int], Dict[str, int], Dict[str, int]]:
    """فهارس: أسماء مطبّعة للـ fuzzy، url→idx، sku→idx، اسم_مطبّع_تام→idx."""
    from engines.engine import (
        _fcol_optional,
        _find_product_name_column,
        resolve_catalog_columns,
    )

    if our_df is None or our_df.empty:
        return [], {}, {}, {}

    rc = resolve_catalog_columns(our_df)
    name_col = rc.get("name") or _find_product_name_column(our_df)
    if not name_col or name_col not in our_df.columns:
        return [], {}, {}, {}

    sku_col = _fcol_optional(
        our_df,
        [
            "رقم المنتج",
            "معرف المنتج",
            "المعرف",
            "معرف",
            "رقم_المنتج",
            "معرف_المنتج",
            "product_id",
            "Product ID",
            "SKU",
            "sku",
            "رمز المنتج sku",
            "الباركود",
            "barcode",
        ],
    )
    url_col = _fcol_optional(
        our_df,
        [
            "رابط المنتج",
            "الرابط",
            "رابط",
            "product_url",
            "link",
            "url",
            "URL",
        ],
    )

    names_raw = our_df[name_col].fillna("").astype(str).str.strip()
    catalog_norms: List[str] = []
    exact_index: Dict[str, int] = {}
    url_index: Dict[str, int] = {}
    sku_index: Dict[str, int] = {}

    for idx, raw in enumerate(names_raw):
        n = normalize_text(_strip_marketing_noise(raw))
        catalog_norms.append(n)
        if n and n not in exact_index:
            exact_index[n] = idx
        if url_col and url_col in our_df.columns:
            u = _normalize_product_url(our_df.iloc[idx][url_col])
            if u and u not in url_index:
                url_index[u] = idx
        if sku_col and sku_col in our_df.columns:
            raw_cell = our_df.iloc[idx][sku_col]
            sk = _norm_sku_key(raw_cell)
            if sk and sk not in sku_index:
                sku_index[sk] = idx
            raw_s = str(raw_cell).strip()
            if raw_s and raw_s.lower() not in ("nan", "none", "") and raw_s not in sku_index:
                sku_index[raw_s] = idx

    return catalog_norms, exact_index, url_index, sku_index


def _fuzzy_best_indices(
    query_norms: List[str],
    catalog_norms: List[str],
    brands: List[str],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    لكل استعلام: أفضل درجة fuzzy (token_set_ratio) وفهرس الكتالوج، وعلم هيكل مقبول.
    يستخدم cdist على دفعات لتقليل الذروة في الذاكرة.
    """
    n_q = len(query_norms)
    best_scores = np.zeros(n_q, dtype=np.float64)
    best_idx = np.full(n_q, -1, dtype=np.int32)
    struct_ok = np.zeros(n_q, dtype=np.bool_)

    if not query_norms or not catalog_norms:
        return best_scores, best_idx, struct_ok

    cat_arr = np.array(catalog_norms, dtype=object)
    n_c = len(catalog_norms)

    for start in range(0, n_q, _CDIST_CHUNK):
        end = min(start + _CDIST_CHUNK, n_q)
        chunk = query_norms[start:end]
        mat = rf_process.cdist(
            chunk,
            cat_arr,
            scorer=fuzz.token_set_ratio,
            workers=-1,
        )
        # أفضل عمود لكل صف
        local_best = mat.argmax(axis=1)
        scores = mat[np.arange(mat.shape[0]), local_best]
        best_scores[start:end] = scores
        best_idx[start:end] = local_best.astype(np.int32)

    for i in range(n_q):
        j = int(best_idx[i])
        if j < 0:
            continue
        qn = query_norms[i]
        cn = catalog_norms[j]
        ok, _reason = _check_structural_constraints(cn, qn, brands)
        struct_ok[i] = ok

    return best_scores, best_idx, struct_ok


def reconcile_competitor_upload(
    our_df: pd.DataFrame,
    comp_dfs: Dict[str, pd.DataFrame],
) -> ReconciliationReport:
    """
    يحسب المحاسبة الكاملة لكل صف في ملفات المنافسين دون حذف صامت.
    """
    from engines.engine import (
        _extract_image_url_from_cell,
        _find_image_column,
        _find_product_name_column,
        _find_url_column,
        _fcol_optional,
        _first_product_page_url_from_row,
        _name_col_for_analysis,
        _norm_sku_barrier,
        _pid,
        _price,
        extract_brand,
        extract_gender,
        extract_product_line,
        extract_size,
        extract_type,
        favicon_url_for_site,
        fetch_og_image_url,
        is_sample,
        is_tester,
    )

    report = ReconciliationReport()
    brands = _known_brands_list()

    if not comp_dfs:
        return report

    catalog_norms, exact_index, url_index, sku_index = _build_our_indexes(our_df)
    has_catalog = bool(catalog_norms)

    failed_rows: List[Dict[str, Any]] = []
    new_rows: List[Dict[str, Any]] = []
    matched = new_r = corrupted = 0
    duplicate_skipped = 0
    total = 0

    for cname, cdf in comp_dfs.items():
        if cdf is None or getattr(cdf, "empty", True):
            continue

        n_file = len(cdf)
        total += n_file

        name_col = _name_col_for_analysis(cdf) or _find_product_name_column(cdf)
        url_col = _find_url_column(cdf)
        icol = _fcol_optional(
            cdf,
            [
                "رقم المنتج",
                "معرف المنتج",
                "المعرف",
                "معرف",
                "رقم_المنتج",
                "معرف_المنتج",
                "product_id",
                "Product ID",
                "Product_ID",
                "ID",
                "id",
                "SKU",
                "sku",
                "رمز المنتج",
                "رمز_المنتج",
                "الكود",
                "Barcode",
                "barcode",
            ],
        ) or ""
        img_col = _find_image_column(cdf) or ""

        if name_col and name_col in cdf.columns:
            names = cdf[name_col].fillna("").astype(str).str.strip()
        else:
            names = pd.Series([""] * n_file)
        urls_ser = (
            cdf[url_col].map(_normalize_product_url)
            if url_col
            else pd.Series([""] * n_file)
        )

        # صفوف تحتاج معالجة دفعية للـ fuzzy
        pending_fuzzy_idx: List[int] = []
        pending_fuzzy_norms: List[str] = []

        seen_keys: set = set()

        for i in range(n_file):
            row = cdf.iloc[i]
            cp = str(names.iloc[i]).strip()
            url_v = str(urls_ser.iloc[i]).strip() if i < len(urls_ser) else ""
            if not url_v and url_col:
                url_v = _normalize_product_url(_cell_str(row, url_col))
            if not url_v:
                url_v = _normalize_product_url(_first_product_page_url_from_row(row))

            comp_sku_raw = _pid(row, icol) if icol else ""
            comp_sku_key = _norm_sku_barrier(comp_sku_raw)

            c_agg = normalize_text(_strip_marketing_noise(cp))
            bare_ck = re.sub(r"\btester\b|تستر|tester", "", c_agg).strip()

            corrupt_reason = ""
            if (not cp or len(cp) < 2) and not url_v:
                corrupt_reason = "لا_اسم_ولا_رابط_منتج"
            elif is_sample(cp):
                corrupt_reason = "عينة_أو_مرفوض"
            elif not bare_ck or len(bare_ck) < 3:
                corrupt_reason = "اسم_غير_كافٍ_بعد_التطبيع"

            if corrupt_reason:
                corrupted += 1
                fr = row.to_dict()
                fr["__المنافس__"] = cname
                fr["__صف_الملف__"] = i
                fr["__سبب_التلف__"] = corrupt_reason
                failed_rows.append(fr)
                continue

            # FIX: Relaxed Constraints — مفتاح إزالة التكرار أقل عدوانية
            # لمنع دمج اختلافات حقيقية (مثل روائح/أحجام/أنواع مختلفة).
            _size_hint = str(extract_size(cp) or "").strip().lower()
            _type_hint = str(extract_type(cp) or "").strip().lower()
            _gender_hint = str(extract_gender(cp) or "").strip().lower()
            dedupe_key = (
                str(cname or "").strip().lower(),
                comp_sku_key or "",
                url_v or "",
                bare_ck,
                _size_hint,
                _type_hint,
                _gender_hint,
            )
            if dedupe_key in seen_keys:
                matched += 1
                duplicate_skipped += 1
                continue
            seen_keys.add(dedupe_key)

            in_catalog = False
            match_reason = ""

            raw_sku_st = str(comp_sku_raw).strip()
            if sku_index and (
                (comp_sku_key and comp_sku_key in sku_index)
                or (raw_sku_st and raw_sku_st in sku_index)
            ):
                in_catalog = True
                match_reason = "SKU/معرّف"

            if not in_catalog and url_v and url_index:
                hit = url_index.get(url_v)
                if hit is not None:
                    in_catalog = True
                    match_reason = "product_url"

            if not in_catalog and bare_ck and bare_ck in exact_index:
                in_catalog = True
                match_reason = "اسم_مطبّع_تام"

            if in_catalog:
                matched += 1
                continue

            if has_catalog and bare_ck:
                pending_fuzzy_idx.append(i)
                pending_fuzzy_norms.append(bare_ck)
            else:
                new_r += 1
                # لا كتالوج لمطابقة نصية — يُعتبر جديداً
                _append_new_row(
                    new_rows,
                    row,
                    cname,
                    cp,
                    comp_sku_raw,
                    icol,
                    img_col,
                    url_col,
                    _price,
                    _extract_image_url_from_cell,
                    _first_product_page_url_from_row,
                    fetch_og_image_url,
                    favicon_url_for_site,
                )

        # دفعة fuzzy لملف واحد
        if pending_fuzzy_norms and has_catalog:
            scores, cat_ix, st_ok = _fuzzy_best_indices(pending_fuzzy_norms, catalog_norms, brands)
            for k, orig_i in enumerate(pending_fuzzy_idx):
                row = cdf.iloc[orig_i]
                cp = str(names.iloc[orig_i]).strip()
                comp_sku_raw = _pid(row, icol) if icol else ""
                sc = float(scores[k])
                j = int(cat_ix[k])
                if sc >= _FUZZY_MIN and j >= 0 and bool(st_ok[k]):
                    matched += 1
                else:
                    new_r += 1
                    _append_new_row(
                        new_rows,
                        row,
                        cname,
                        cp,
                        comp_sku_raw,
                        icol,
                        img_col,
                        url_col,
                        _price,
                        _extract_image_url_from_cell,
                        _first_product_page_url_from_row,
                        fetch_og_image_url,
                        favicon_url_for_site,
                    )

    report.total_read = total
    report.matched = matched
    report.new_ready = new_r
    report.corrupted = corrupted
    report.failed_df = pd.DataFrame(failed_rows) if failed_rows else pd.DataFrame()
    report.new_products_df = pd.DataFrame(new_rows) if new_rows else pd.DataFrame()

    _sum = matched + new_r + corrupted
    report.balance_ok = _sum == total
    if not report.balance_ok:
        gap = total - _sum
        report.warning_message = (
            f"محاسبة الصفوف: المدخل={total} لا يساوي مجموع المخرجات={_sum} "
            f"(متطابق={matched} + جديد={new_r} + تالف={corrupted}) — فجوة={gap}"
        )
        warnings.warn(report.warning_message, UserWarning, stacklevel=2)
    if not report.balance_ok:
        _log.warning("⚠️ %s", report.warning_message or "انتهاك معادلة المحاسبة")

    report.diagnostics = {
        "fuzzy_min": _FUZZY_MIN,
        "cdist_chunk": _CDIST_CHUNK,
        "catalog_size": len(catalog_norms),
        "duplicate_skipped": duplicate_skipped,
    }
    return report


def _append_new_row(
    new_rows: List[Dict[str, Any]],
    row: pd.Series,
    cname: str,
    cp: str,
    comp_sku_raw: str,
    icol: str,
    img_col: str,
    url_col: Optional[str],
    _price,
    _extract_image_url_from_cell,
    _first_product_page_url_from_row,
    fetch_og_image_url,
    favicon_url_for_site,
) -> None:
    from engines.engine import (
        extract_brand,
        extract_gender,
        extract_product_line,
        extract_size,
        extract_type,
        is_tester,
    )

    c_brand = extract_brand(cp)
    c_pline = extract_product_line(cp, c_brand)
    c_size = extract_size(cp)
    c_type = extract_type(cp)
    c_gender = extract_gender(cp)
    c_is_t = is_tester(cp)

    _img_url = ""
    if img_col:
        _img_url = _extract_image_url_from_cell(row.get(img_col)) or ""
    _rlink = _cell_str(row, url_col) if url_col else ""
    if not (_rlink and _rlink.startswith("http")):
        _rlink = _first_product_page_url_from_row(row)
    if not _img_url and _rlink and _rlink.startswith("http"):
        _try_og = fetch_og_image_url(_rlink)
        if _try_og:
            _img_url = _try_og
    if not _img_url and _rlink and _rlink.startswith("http"):
        _img_url = favicon_url_for_site(_rlink)

    from datetime import datetime

    new_rows.append(
        {
            "منتج_المنافس": cp,
            "معرف_المنافس": comp_sku_raw,
            "سعر_المنافس": _price(row),
            "المنافس": cname,
            "الماركة": c_brand,
            "الحجم": f"{int(c_size)}ml" if c_size else "",
            "النوع": c_type,
            "الجنس": c_gender,
            "هو_تستر": c_is_t,
            "تاريخ_الرصد": datetime.now().strftime("%Y-%m-%d"),
            "ملاحظة": "",
            "درجة_التشابه": 0.0,
            "مستوى_الثقة": "green",
            "صورة_المنافس": _img_url,
            "رابط_المنافس": _rlink,
            "نوع_متاح": "",
            "منتج_متاح": "",
            "نسبة_التشابه": 0.0,
        }
    )


def failed_rows_to_csv_bytes(failed_df: pd.DataFrame) -> bytes:
    """تصدير سجل الصفوف التالفة باسم failed_rows.csv (UTF-8 BOM) — للتوافق الرجعي."""
    if failed_df is None or failed_df.empty:
        return b""
    buf = io.StringIO()
    failed_df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue().encode("utf-8-sig")


def failed_rows_to_xlsx_bytes(failed_df: pd.DataFrame) -> bytes:
    """
    تصدير سجل الصفوف التالفة إلى ملف Excel أصلي (.xlsx) عبر io.BytesIO.

    ▸ لا يُكتب أي ملف مؤقت على القرص.
    ▸ المنتجات التالفة لا تُحذف — تُرحَّل هنا (قاعدة: لا حذف صامت).
    ▸ يُمرَّر مباشرةً لـ st.download_button مع mime xlsx.
    """
    if failed_df is None or failed_df.empty:
        # إعادة xlsx فارغ بصف رأس واحد بدلاً من bytes فارغ — يتجنب استثناء Streamlit
        empty_buf = io.BytesIO()
        pd.DataFrame(columns=["__المنافس__", "__صف_الملف__", "__سبب_التلف__"]).to_excel(
            empty_buf, index=False, engine="openpyxl"
        )
        empty_buf.seek(0)
        return empty_buf.read()

    buf = io.BytesIO()
    failed_df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    return buf.read()


def merge_reconciliation_into_audit(
    audit_stats: Optional[Dict[str, Any]],
    rec: ReconciliationReport,
) -> Dict[str, Any]:
    """يضيف مفاتيح المحاسبة إلى audit_stats الحالية دون كسر المفاتيح القديمة."""
    out = dict(audit_stats) if audit_stats else {}
    out["reconciliation"] = rec.to_dict()
    out["competitor_rows_total"] = rec.total_read
    out["competitor_rows_matched"] = rec.matched
    out["competitor_rows_new"] = rec.new_ready
    out["competitor_rows_corrupted"] = rec.corrupted
    return out
