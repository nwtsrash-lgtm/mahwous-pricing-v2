"""
utils/helpers.py - دوال مساعدة v17.3
الملف الذي كان مفقوداً - يحتوي على جميع الدوال المستوردة في app.py
"""
import html as html_std
import io
import logging
import re
from functools import lru_cache
from typing import Dict, Optional
from urllib.parse import urlparse

import pandas as pd
import requests

_logger = logging.getLogger(__name__)


# ===== safe_float =====
def safe_float(val, default=0.0) -> float:
    """تحويل قيمة إلى float بأمان"""
    try:
        if val is None or val == "" or (isinstance(val, float) and pd.isna(val)):
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


# ===== format_price =====
def format_price(price, currency="ر.س") -> str:
    """تنسيق عرض السعر"""
    try:
        return f"{float(price):,.0f} {currency}"
    except (ValueError, TypeError):
        return f"0 {currency}"


# ===== format_diff =====
def format_diff(diff) -> str:
    """تنسيق عرض فرق السعر"""
    try:
        d = float(diff)
        sign = "+" if d > 0 else ""
        return f"{sign}{d:,.0f} ر.س"
    except (ValueError, TypeError):
        return "0 ر.س"


# ===== get_filter_options =====
def get_filter_options(df: pd.DataFrame) -> dict:
    """
    Extract all filter option lists from a DataFrame.
    Extended (Task 3.1): now includes gender and size options.
    """
    opts = {
        "brands":      ["الكل"],
        "competitors": ["الكل"],
        "types":       ["الكل"],
        "genders":     ["الكل"],   # Task 3.1 — gender (الجنس column)
        "sizes":       ["الكل"],   # Task 3.1 — perfume size (الحجم column)
    }
    if df is None or df.empty:
        return opts

    if "الماركة" in df.columns:
        brands = df["الماركة"].dropna().unique().tolist()
        brands = sorted([str(b) for b in brands if str(b).strip() and str(b) != "nan"])
        opts["brands"] = ["الكل"] + brands

    if "المنافس" in df.columns:
        comps = df["المنافس"].dropna().unique().tolist()
        comps = sorted([str(c) for c in comps if str(c).strip() and str(c) != "nan"])
        opts["competitors"] = ["الكل"] + comps

    if "النوع" in df.columns:
        types = df["النوع"].dropna().unique().tolist()
        types = sorted([str(t) for t in types if str(t).strip() and str(t) != "nan"])
        opts["types"] = ["الكل"] + types

    # Task 3.1: gender options — NEVER treat gender markers as stopwords
    if "الجنس" in df.columns:
        genders = df["الجنس"].dropna().unique().tolist()
        genders = sorted([str(g) for g in genders if str(g).strip() and str(g) not in ("nan", "None")])
        if genders:
            opts["genders"] = ["الكل"] + genders

    # Task 3.1: size options (e.g. "100ml", "50ml") — only if column present
    if "الحجم" in df.columns:
        sizes = df["الحجم"].dropna().unique().tolist()
        sizes = sorted([str(s) for s in sizes if str(s).strip() and str(s) not in ("nan", "None", "")])
        if sizes:
            opts["sizes"] = ["الكل"] + sizes

    return opts


# ===== apply_filters =====
def apply_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """
    Apply all active filter values to a DataFrame.
    Extended (Task 3.1): supports gender and size filters.
    All filters are additive (AND logic). Safe: never modifies the original df.
    """
    if df is None or df.empty:
        return df

    result = df.copy()

    # Text search across multiple product name columns
    search = filters.get("search", "").strip()
    if search:
        mask = pd.Series([False] * len(result), index=result.index)
        for col in ["المنتج", "منتج_المنافس", "الماركة"]:
            if col in result.columns:
                mask = mask | result[col].astype(str).str.contains(search, case=False, na=False)
        result = result[mask]

    brand = filters.get("brand", "الكل")
    if brand and brand != "الكل" and "الماركة" in result.columns:
        result = result[result["الماركة"].astype(str) == brand]

    competitor = filters.get("competitor", "الكل")
    if competitor and competitor != "الكل" and "المنافس" in result.columns:
        result = result[result["المنافس"].astype(str) == competitor]

    ptype = filters.get("type", "الكل")
    if ptype and ptype != "الكل" and "النوع" in result.columns:
        result = result[result["النوع"].astype(str) == ptype]

    match_min = filters.get("match_min")
    if match_min and "نسبة_التطابق" in result.columns:
        result = result[result["نسبة_التطابق"] >= float(match_min)]

    price_min = filters.get("price_min", 0.0)
    if price_min and price_min > 0 and "السعر" in result.columns:
        result = result[result["السعر"] >= float(price_min)]

    price_max = filters.get("price_max")
    if price_max and price_max > 0 and "السعر" in result.columns:
        result = result[result["السعر"] <= float(price_max)]

    # Task 3.1 — gender filter (never treat gender markers as stopwords)
    gender_f = filters.get("gender", "الكل")
    if gender_f and gender_f != "الكل" and "الجنس" in result.columns:
        result = result[result["الجنس"].astype(str) == gender_f]

    # Task 3.1 — perfume size filter (exact match on الحجم column)
    size_f = filters.get("size", "الكل")
    if size_f and size_f != "الكل" and "الحجم" in result.columns:
        result = result[result["الحجم"].astype(str) == size_f]

    return result.reset_index(drop=True)


# ===== export_to_excel =====
def export_to_excel(df: pd.DataFrame, sheet_name: str = "النتائج") -> bytes:
    """تصدير DataFrame إلى Excel — عبر io.BytesIO (لا disk I/O)"""
    output = io.BytesIO()
    export_df = df.copy()

    for col in ["جميع المنافسين", "جميع_المنافسين"]:
        if col in export_df.columns:
            export_df = export_df.drop(columns=[col])

    safe_name = sheet_name[:31]
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_df.to_excel(writer, sheet_name=safe_name, index=False)
        ws = writer.sheets[safe_name]
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    return output.getvalue()


# ===== export_multiple_sheets =====
def export_multiple_sheets(sheets: Dict[str, pd.DataFrame]) -> bytes:
    """تصدير عدة DataFrames في ملف Excel متعدد الأوراق — عبر io.BytesIO"""
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            export_df = df.copy()
            for col in ["جميع المنافسين", "جميع_المنافسين"]:
                if col in export_df.columns:
                    export_df = export_df.drop(columns=[col])

            safe_name = str(sheet_name)[:31]
            export_df.to_excel(writer, sheet_name=safe_name, index=False)
            ws = writer.sheets[safe_name]
            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    return output.getvalue()


# ===== parse_pasted_text =====
def parse_pasted_text(text: str):
    """
    تحليل نص ملصوق وتحويله إلى DataFrame.
    يدعم: CSV، TSV، جداول مفصولة بـ |
    """
    if not text or not text.strip():
        return None, "النص فارغ"

    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]

    if not lines:
        return None, "لا توجد بيانات"

    # محاولة 1: مفصول بـ |
    if "|" in lines[0]:
        rows = []
        for line in lines:
            if set(line.replace(" ", "").replace("-", "")) == {"|"}:
                continue  # تخطي خطوط الفاصل
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if cells:
                rows.append(cells)

        if len(rows) >= 2:
            try:
                df = pd.DataFrame(rows[1:], columns=rows[0])
                return df, f"✅ تم تحليل {len(df)} صف"
            except (ValueError, IndexError) as e:
                _logger.debug("parse pipe table error: %s", e)

    # محاولة 2: TSV
    if "\t" in lines[0]:
        try:
            df = pd.read_csv(io.StringIO(text), sep="\t")
            return df, f"✅ تم تحليل {len(df)} صف (TSV)"
        except (pd.errors.ParserError, ValueError) as e:
            _logger.debug("parse TSV error: %s", e)

    # محاولة 3: CSV
    try:
        df = pd.read_csv(io.StringIO(text))
        return df, f"✅ تم تحليل {len(df)} صف (CSV)"
    except (pd.errors.ParserError, ValueError) as e:
        _logger.debug("parse CSV error: %s", e)

    # محاولة 4: كل سطر منتج
    if len(lines) >= 2:
        df = pd.DataFrame({"البيانات": lines})
        return df, f"✅ تم تحليل {len(df)} سطر"

    return None, "❌ لا يمكن تحليل الصيغة. جرب CSV أو جدول مفصول بـ |"


# ===== fetch_og_image_url =====
@lru_cache(maxsize=256)
def fetch_og_image_url(url: str, timeout: float = 6.0) -> str:
    """يجلب og:image (أو twitter:image) من HTML صفحة المنتج."""
    u = (url or "").strip()
    if not u.startswith("http"):
        return ""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        }
        r = requests.get(u, timeout=timeout, headers=headers, allow_redirects=True)
        if r.status_code != 200:
            return ""
        text = r.text[:900_000]
        patterns = (
            re.compile(
                r'<meta[^>]+property=["\'](og:image)["\'][^>]+content=["\']([^"\']+)["\']',
                re.I,
            ),
            re.compile(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                re.I,
            ),
            re.compile(
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
                re.I,
            ),
        )
        for pat in patterns:
            m = pat.search(text)
            if m:
                img = (m.group(m.lastindex) or "").strip()
                if img.startswith(("https://", "http://")):
                    return img
                if img.startswith("//"):
                    return "https:" + img
    except (requests.RequestException, OSError) as e:
        _logger.debug("fetch_og_image_url %s: %s", u, e)
    return ""


def fetch_page_title_from_url(url: str, timeout: float = 8.0) -> str:
    """
    يجلب عنواناً مقروءاً من صفحة المنتج: og:title ثم twitter:title ثم <title>.
    يُنظّف لاحقة المتجر الشائعة ( | Site — متجر).
    """
    u = (url or "").strip()
    if not u.startswith("http"):
        return ""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        }
        r = requests.get(u, timeout=timeout, headers=headers, allow_redirects=True)
        if r.status_code != 200:
            return ""
        text = r.text[:900_000]
        raw = ""
        for pat in (
            re.compile(
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                re.I,
            ),
            re.compile(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
                re.I,
            ),
            re.compile(
                r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
                re.I,
            ),
            re.compile(r"<title[^>]*>([^<]{4,500})</title>", re.I | re.DOTALL),
        ):
            m = pat.search(text)
            if m:
                raw = (m.group(1) or "").strip()
                if raw:
                    break
        if not raw:
            return ""
        title = html_std.unescape(raw).strip()
        _lines = []
        for ln in re.split(r"[\r\n]+", title):
            ln = ln.strip()
            if not ln:
                continue
            ln = re.sub(r"^محلي\s*[-–—:،]\s*", "", ln)
            ln = re.sub(r"^محلي\s+", "", ln).strip()
            ln = re.sub(r"\s+", " ", ln)
            if ln:
                _lines.append(ln)
        if _lines:
            title = max(_lines, key=len)
        else:
            title = re.sub(r"\s+", " ", title).strip()
        for sep in (" | ", " – ", " — ", " - ", " :: "):
            if sep in title:
                left = title.split(sep)[0].strip()
                if len(left) >= 6:
                    title = left
                    break
        title = re.sub(r"^(buy|shop|تسوق|اشتري)\s+", "", title, flags=re.I).strip()
        title = re.sub(r"^محلي\s*[-–—:،]\s*", "", title).strip()
        title = re.sub(r"^محلي\s+", "", title).strip()
        return title[:220] if title else ""
    except (requests.RequestException, OSError) as e:
        _logger.debug("fetch_page_title_from_url %s: %s", u, e)
        return ""


def favicon_url_for_site(page_url: str) -> str:
    """أيقونة موجّهة من خدمة عامة — احتياط عند فشل og:image."""
    u = (page_url or "").strip()
    if not u.startswith("http"):
        return ""
    try:
        netloc = urlparse(u).netloc
        if not netloc:
            return ""
        return f"https://www.google.com/s2/favicons?domain={netloc}&sz=128"
    except (ValueError, AttributeError) as e:
        _logger.debug("favicon_url_for_site error: %s", e)
        return ""


# ===== BackgroundTask (stub) =====
class BackgroundTask:
    """
    محاكاة معالجة في الخلفية.
    Streamlit لا يدعم true background threads بشكل كامل — هذا placeholder وظيفي.
    """
    def __init__(self, func, *args, **kwargs):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.result = None
        self.done = False
        self.error = None

    def run(self):
        """تشغيل المهمة مباشرة (synchronous)"""
        try:
            self.result = self.func(*self.args, **self.kwargs)
            self.done = True
        except Exception as e:
            self.error = str(e)
            self.done = True
        return self.result

    def is_done(self):
        return self.done
