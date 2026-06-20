"""
دوال مساعدة خالصة (بدون واجهة ولا session_state) — تجهيز البيانات والنصوص.
"""
import json
import re
from datetime import datetime
from typing import Optional

import pandas as pd

# أول رابط صورة http(s) — يتوقف عند الفاصلة (إنجليزي/عربي) حتى لا يُلتقط رابطان في src واحد
_FIRST_HTTP_IMAGE_URL = re.compile(
    r"https?://[^\s<>\"\'\,\u060c؛;]+?"
    r"\.(?:webp|jpg|jpeg|png|gif|avif)"
    r"(?:\?[^\s<>\"\'\,\u060c؛;]*)?",
    re.I,
)
# تسلسل شائع في سلة/إكسيل: ...jpg,https://...
_AFTER_EXT_COMMA_HTTP = re.compile(
    r"\.(?:webp|jpg|jpeg|png|gif|avif)\s*[,،]\s*https?://",
    re.I,
)


def _looks_like_several_image_urls(s: str) -> bool:
    """True فقط عندما يُرجّح أن النص يضم أكثر من رابط (لا نلمس رابط المنافس بفاصلة داخل ?query)."""
    if not s or ("http://" not in s and "https://" not in s):
        return False
    n = s.count("http://") + s.count("https://")
    if n > 1:
        return True
    return bool(_AFTER_EXT_COMMA_HTTP.search(s))

# حقول وسائط قد تُحفظ كـ NaN — لا تُستبدل بالصفر
_MEDIA_KEYS_EMPTY_ON_NA = frozenset({
    "صورة_منتجنا", "رابط_منتجنا", "صورة_المنتج", "رابط_المنتج",
    "رابط_المنافس",
    "صورة المنتج", "رابط المنتج", "صوره المنتج", "الرابط", "رابط",
})


def first_image_url_string(s: str) -> str:
    """
    عندما تُخزّن عدة روابط صور في خلية واحدة مفصولة بفاصلة (شائع في تصدير سلة/إكسيل)،
    أرجع أول رابط http يبدو ملف صورة — حتى يعمل <img src> والمتصفح.

    إن كان الرابط واحداً (مثل صور المنافسين مع فاصلة داخل ?query)، يُعاد كما هو دون تقسيم.
    """
    s = (s or "").strip()
    if not s:
        return ""
    if "http://" not in s and "https://" not in s:
        return s.split()[0]
    if not _looks_like_several_image_urls(s):
        return s.split()[0]
    m = _FIRST_HTTP_IMAGE_URL.search(s)
    if m:
        return m.group(0).strip().rstrip(".,;)]\"'")
    # احتياط: فواصل غير إنجليزية أو نص بدون امتداد واضح في الـ regex
    norm = s.replace("\u060c", ",").replace("،", ",").replace("؛", ";").replace("\n", ",")
    for sep in (",", ";"):
        if sep not in norm:
            continue
        for part in norm.split(sep):
            part = part.strip()
            if not part.startswith("http"):
                continue
            base = part.split("?")[0].lower()
            if any(base.endswith(ext) for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif")):
                return part.split()[0]
        for part in norm.split(sep):
            part = part.strip()
            if part.startswith("http"):
                return part.split()[0]
    return s.split()[0]


def _strip_media_val(v):
    if v is None:
        return ""
    try:
        if isinstance(v, float) and pd.isna(v):
            return ""
        if pd.isna(v) and not isinstance(v, (list, dict, str)):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "0", "<na>"):
        return ""
    return s


def normalize_result_media_keys(row: dict) -> None:
    """يوحّد صورة/رابط منتجنا تحت المفتاحين المعتمدين في الواجهة والمحرك."""
    if not row:
        return
    if not _strip_media_val(row.get("صورة_منتجنا")):
        for alt in ("صورة_المنتج", "صورة المنتج", "صوره المنتج"):
            if alt in row:
                v = _strip_media_val(row.get(alt))
                if v:
                    row["صورة_منتجنا"] = v
                    break
    if not _strip_media_val(row.get("رابط_منتجنا")):
        for alt in ("رابط_المنتج", "رابط المنتج", "الرابط", "رابط"):
            if alt in row:
                v = _strip_media_val(row.get(alt))
                if v:
                    row["رابط_منتجنا"] = v
                    break


def row_media_urls_from_analysis(row) -> tuple:
    """
    صورة منتجنا + صورة المنافس الرئيسي من صف نتيجة (Series أو dict).
    يعتمد على مفتاحي صورة_منتجنا وجميع_المنافسين بعد التطبيع.
    """
    if row is None:
        return ("", "")
    d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    normalize_result_media_keys(d)
    our_img = first_image_url_string(str(d.get("صورة_منتجنا", "") or "").strip())
    comp_img = ""
    all_c = d.get("جميع_المنافسين", d.get("جميع المنافسين", [])) or []
    if isinstance(all_c, str):
        try:
            all_c = json.loads(all_c)
        except Exception:
            all_c = []
    if not isinstance(all_c, list):
        all_c = []
    comp_name = str(d.get("منتج_المنافس", "—"))
    for c in all_c:
        if str(c.get("name", "")).strip() == str(comp_name).strip():
            comp_img = first_image_url_string(str(c.get("image_url") or c.get("thumb") or "").strip())
            break
    if not comp_img and all_c:
        comp_img = first_image_url_string(str(all_c[0].get("image_url") or all_c[0].get("thumb") or "").strip())
    return (our_img, comp_img)


def our_product_url_from_row(row) -> str:
    """رابط صفحة منتجنا — بعد تطبيع أسماء الأعمدة (رابط_منتجنا / رابط_المنتج / …)."""
    if row is None:
        return ""
    d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    normalize_result_media_keys(d)
    u = _strip_media_val(d.get("رابط_منتجنا"))
    if not u.startswith("http"):
        return ""
    return u.split()[0]


def competitor_product_url_from_row(row) -> str:
    """رابط صفحة المنتج عند المنافس — أعمدة النتيجة أو جميع_المنافسين أو أسماء مثل abs-size href."""
    if row is None:
        return ""
    d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    for k in ("رابط_المنافس", "رابط المنافس", "competitor_url"):
        v = _strip_media_val(d.get(k))
        if v.startswith("http"):
            return v.split()[0]
    comp_name = str(d.get("منتج_المنافس", "—"))
    all_c = d.get("جميع_المنافسين", d.get("جميع المنافسين", [])) or []
    if isinstance(all_c, str):
        try:
            all_c = json.loads(all_c)
        except Exception:
            all_c = []
    if isinstance(all_c, list):
        for c in all_c:
            if str(c.get("name", "")).strip() == str(comp_name).strip():
                u = str(c.get("product_url") or c.get("url") or "").strip()
                if u.startswith("http"):
                    return u.split()[0]
        if all_c:
            u = str(all_c[0].get("product_url") or all_c[0].get("url") or "").strip()
            if u.startswith("http"):
                return u.split()[0]
    for k, v in d.items():
        sk = str(k).lower()
        if k in ("رابط_منتجنا", "رابط منتجنا") or "منتجنا" in sk:
            continue
        if "صورة" in str(k) and "وصف" not in str(k) and "href" not in sk:
            continue
        if any(x in sk for x in ("href", "رابط", "link", "url")):
            s = _strip_media_val(v)
            if s.startswith("http"):
                return s.split()[0]
    # أحياناً يُخزَّن رابط صفحة المنتج بالخطأ في عمود الاسم (مثل تصدير المنافس)
    vnm = _strip_media_val(d.get("منتج_المنافس"))
    if vnm.startswith("http"):
        return vnm.split()[0]
    return ""


def safe_results_for_json(results_list):
    """تحويل النتائج لصيغة آمنة للحفظ في JSON/SQLite — يحول القوائم المتداخلة."""
    safe = []
    for r in results_list:
        row = {}
        # دعم كل من dict و pandas Series
        items = r.items() if hasattr(r, "items") else (r.to_dict().items() if hasattr(r, "to_dict") else [])
        for k, v in items:
            if isinstance(v, list):
                try:
                    row[k] = json.dumps(v, ensure_ascii=False, default=str)
                except Exception:
                    row[k] = str(v)
            else:
                try:
                    # التحقق من NaN بشكل آمن
                    if v is not None and not isinstance(v, (list, dict, str)) and pd.isna(v):
                        # الحفاظ على الحقول النصية فارغة والحقول الرقمية كـ None أو 0
                        if k in _MEDIA_KEYS_EMPTY_ON_NA or k in ("المنتج", "الماركة", "اسم المنتج"):
                            row[k] = ""
                        elif "سعر" in str(k) or "diff" in str(k).lower():
                            row[k] = 0.0
                        else:
                            row[k] = None # السماح بـ null في JSON للحفاظ على النوع
                        continue
                except (TypeError, ValueError):
                    pass
                row[k] = v
        # ⚡ perf/قرص: لا تكتب وصف HTML الميت في results_json (يُضخّمه ~71MB→~3MB)
        _drop_nonurl_desc_blob(row)
        safe.append(row)
    return safe


# أعمدة «رابط منتجنا» — يُفترض أن تحمل URL. في كتالوج محوس تحمل وصف HTML
# كامل (≈6.9KB/صف × 7.8K صف ≈ 206MB) لا رابطاً. لا أحد يستهلك هذا الوصف من هنا
# (our_product_url_from_row يرفض أي قيمة لا تبدأ بـ http؛ الأوصاف تأتي من
# load_our_descriptions المنفصل)، فنُفرّغه قبل أن يدخل session_state.
_OUR_URL_KEYS = ("رابط_منتجنا", "رابط منتجنا", "رابط_المنتج", "رابط المنتج")


def _drop_nonurl_desc_blob(row: dict) -> None:
    """يُفرّغ قيم «رابط منتجنا» التي ليست URL (وصف HTML) — توفير ذاكرة ضخم."""
    for k in _OUR_URL_KEYS:
        v = row.get(k)
        if isinstance(v, str) and v and not v.lstrip().lower().startswith("http"):
            row[k] = ""


def restore_results_from_json(results_list):
    """استعادة النتائج من JSON — يحول نصوص القوائم لقوائم فعلية."""
    if not results_list: return []
    restored = []
    for r in results_list:
        row = dict(r) if isinstance(r, dict) else {}
        # استعادة القوائم من نصوص JSON
        for k, v in row.items():
            if isinstance(v, str) and v.strip() and (v.startswith("[") or v.startswith("{")):
                try:
                    row[k] = json.loads(v)
                except Exception:
                    pass
        # ضمان وجود مفاتيح المنافسين كقوائم حتى لو كانت مفقودة
        for k in ["جميع_المنافسين", "جميع المنافسين"]:
            if k not in row or row[k] is None:
                row[k] = []
        # ⚡ perf/ذاكرة: تجريد وصف HTML الميت من أعمدة الرابط قبل التطبيع
        _drop_nonurl_desc_blob(row)
        normalize_result_media_keys(row)
        _drop_nonurl_desc_blob(row)  # طبّع قد يكون أعاد سحبه من بديل — أفرغه ثانية
        restored.append(row)
    return restored


def ts_badge(ts_str=""):
    """شارة تاريخ مصغرة (HTML)."""
    if not ts_str:
        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f'<span style="font-size:.65rem;color:#555;background:#1a1a2e;'
        f'padding:1px 6px;border-radius:8px;margin-right:4px">🕐 {ts_str}</span>'
    )


def decision_badge(action):
    """شارة قرار معلّق (HTML)."""
    colors = {
        "approved": ("#00C853", "✅ موافق"),
        "deferred": ("#FFD600", "⏸️ مؤجل"),
        "removed": ("#FF1744", "🗑️ محذوف"),
    }
    c, label = colors.get(action, ("#666", action))
    return f'<span style="font-size:.7rem;color:{c};font-weight:700">{label}</span>'


def pid_from_row(row, col):
    """استخراج معرف المنتج من صف pandas بشكل آمن."""
    if not col or col not in row.index:
        return ""
    v = row.get(col, "")
    if v is None or str(v) in ("nan", "None", "", "NaN"):
        return ""
    try:
        fv = float(v)
        if fv == int(fv):
            return str(int(fv))
    except (ValueError, TypeError):
        pass
    return str(v).strip()


def _analysis_dedupe_columns(df: pd.DataFrame) -> list[str]:
    """أعمدة تمييز قوية فقط (معرّفات/روابط) لتجنب حذف منتجات متقاربة الاسم."""
    if df is None or df.empty:
        return []
    strong = ["معرف_المنتج", "معرف_المنافس", "المنافس"]
    if all(c in df.columns for c in strong):
        return strong
    strong_url = ["رابط_منتجنا", "رابط_المنافس", "المنافس"]
    if all(c in df.columns for c in strong_url):
        return strong_url
    strong_mix = ["معرف_المنتج", "رابط_المنافس", "المنافس"]
    if all(c in df.columns for c in strong_mix):
        return strong_mix
    return []


def merge_price_analysis_dataframes(
    prev: Optional[pd.DataFrame],
    new: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """
    يدمج جدول نتائج تحليل جديد مع جدول سابق: نفس المفتاح → يُبقى الصف من التشغيل الأحدث.
    """
    if new is None or (isinstance(new, pd.DataFrame) and new.empty):
        return prev.copy() if prev is not None and not prev.empty else pd.DataFrame()
    if prev is None or prev.empty:
        return new.copy()

    all_cols: list[str] = list(dict.fromkeys(list(prev.columns) + list(new.columns)))
    prev2 = prev.reindex(columns=all_cols)
    new2 = new.reindex(columns=all_cols)
    out = pd.concat([prev2, new2], ignore_index=True)
    subset = _analysis_dedupe_columns(out)
    if subset:
        # FIX: Relaxed Constraints — عدم استخدام الاسم كمفتاح دمج لتفادي فقدان نتائج متشابهة.
        out = out.drop_duplicates(subset=subset, keep="last")
    # FIX: Relaxed Constraints — عند غياب مفاتيح قوية نبقي كل الصفوف (Zero Data Loss).
    return out.reset_index(drop=True)


def _missing_dedupe_columns(df: pd.DataFrame) -> list[str]:
    if df is None or df.empty:
        return []
    cols = [c for c in ("المنافس", "معرف_المنافس", "منتج_المنافس") if c in df.columns]
    if len(cols) >= 2:
        return cols
    if "منتج_المنافس" in df.columns and "المنافس" in df.columns:
        return ["المنافس", "منتج_المنافس"]
    return []


def merge_missing_products_dataframes(
    prev: Optional[pd.DataFrame],
    new: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """دمج جداول المنتجات المفقودة بين تشغيلين."""
    if new is None or (isinstance(new, pd.DataFrame) and new.empty):
        return prev.copy() if prev is not None and not prev.empty else pd.DataFrame()
    if prev is None or prev.empty:
        return new.copy()

    all_cols = list(dict.fromkeys(list(prev.columns) + list(new.columns)))
    p2 = prev.reindex(columns=all_cols)
    n2 = new.reindex(columns=all_cols)
    out = pd.concat([p2, n2], ignore_index=True)
    subset = _missing_dedupe_columns(out)
    if subset:
        out = out.drop_duplicates(subset=subset, keep="last")
    else:
        out = out.drop_duplicates(keep="last")
    return out.reset_index(drop=True)
