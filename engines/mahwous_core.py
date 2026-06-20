"""
mahwous_core — فلاتر مسار صارمة، استخراج المكونات، وتنسيق "مهووس" الاحترافي.
متوافق 100% مع منصة سلة و Make.
v28.0 - النسخة الكاملة المدمجة.
"""
from __future__ import annotations

import re
import html
from typing import Any, Dict, List, Tuple

import pandas as pd

try:
    from config import REJECT_KEYWORDS
except ImportError:
    REJECT_KEYWORDS = [
        "sample", "عينة", "عينه", "decant", "تقسيم", "تقسيمة",
        "split", "miniature", "0.5ml", "1ml", "2ml", "3ml",
    ]

# تعبيرات نمطية لتنظيف النصوص لمنصة سلة
_HTML_TAG_RE = re.compile(r"<[^>]+>")

def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or str(val).strip() in ("", "nan", "None", "NaN"):
            return default
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return default

def _is_sample_strict(name: str) -> bool:
    if not isinstance(name, str) or not name.strip():
        return True
    nl = name.lower()
    # v31.6: word boundary matching لمنع false positives مثل "Crystal Split"
    for k in REJECT_KEYWORDS:
        try:
            if re.search(r'\b' + re.escape(k.lower()) + r'\b', nl):
                return True
        except re.error:
            if k.lower() in nl:
                return True
    return False

def _extract_ml(name: str) -> float:
    if not isinstance(name, str):
        return -1.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ml|مل|ملي)\b", name, re.I)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return -1.0
    return -1.0

def _classify_rejected(name: str) -> bool:
    if not isinstance(name, str):
        return True
    nl = name.lower()
    rejects = ["sample", "عينة", "عينه", "miniature", "مينياتشر", "travel size", "decant", "تقسيم"]
    # v31.6: لا نرفض "split" ككلمة منفردة — فقط كلمات العينات المؤكدة
    for w in rejects:
        try:
            if re.search(r'\b' + re.escape(w) + r'\b', nl):
                return True
        except re.error:
            if w in nl:
                return True
    return False

def apply_strict_pipeline_filters(
    df: pd.DataFrame,
    name_col: str = "منتج_المنافس",
    min_ml: float = 2.0,
    keep_excluded: bool = True,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """فلاتر استبعاد المنتجات مع تسجيل الأسباب بدقة."""
    if df is None or df.empty:
        return df, {"dropped": 0}

    actual_col = name_col
    if name_col not in df.columns:
        alt_cols = ["المنتج", "اسم المنتج", "Product", "Name", "أسم المنتج"]
        for c in alt_cols:
            if c in df.columns:
                actual_col = c
                break
        else:
            return df.copy(), {"dropped": 0, "warning": f"عمود غير موجود: {name_col}"}

    stats: Dict[str, Any] = {
        "dropped_sample_kw": 0,
        "dropped_small_ml": 0,
        "dropped_class_rejected": 0,
        "dropped_empty_name": 0,
        "excluded_rows": []
    }
    keep_idx: List[Any] = []
    excluded_reasons: Dict[int, str] = {}

    for idx, row in df.iterrows():
        name = str(row.get(actual_col, "")).strip()
        
        # 1. الأسماء الفارغة
        if not name or name.lower() in ("nan", "none", "<na>"):
            stats["dropped_empty_name"] += 1
            excluded_reasons[idx] = "اسم فارغ"
            continue
            
        # 2. كلمات استبعاد العينات
        if _is_sample_strict(name):
            stats["dropped_sample_kw"] += 1
            stats["excluded_rows"].append({"name": name, "reason": "كلمة عينة محظورة"})
            excluded_reasons[idx] = "كلمة عينة محظورة"
            continue
            
        if _classify_rejected(name):
            stats["dropped_class_rejected"] += 1
            stats["excluded_rows"].append({"name": name, "reason": "تصنيف مستبعد (عينة/تقسيم)"})
            excluded_reasons[idx] = "تصنيف مستبعد (عينة/تقسيم)"
            continue
            
        # 3. الأحجام الصغيرة جداً (أقل من 2 مل افتراضياً بدل 5 مل)
        ml = _extract_ml(name)
        if 0 < ml < min_ml:
            stats["dropped_small_ml"] += 1
            stats["excluded_rows"].append({"name": name, "reason": f"حجم صغير جداً ({ml} مل)"})
            excluded_reasons[idx] = f"حجم صغير جداً ({ml} مل)"
            continue

        keep_idx.append(idx)

    # FIX: Relaxed Constraints — Zero Data Loss: لا نحذف الصفوف افتراضياً، نضيف سبب الاستبعاد فقط.
    if keep_excluded:
        out = df.copy()
        out["سبب_الاستبعاد"] = out.index.map(lambda i: excluded_reasons.get(i, ""))
    else:
        out = df.loc[keep_idx].reset_index(drop=True) if keep_idx else pd.DataFrame()
        if not out.empty:
            out["سبب_الاستبعاد"] = ""
    stats["dropped"] = len(excluded_reasons)
    stats["kept"] = len(df) - len(excluded_reasons)
    return out, stats

def sanitize_salla_text(text: str) -> str:
    """تنظيف النصوص من الرموز البرمجية والأحرف الخاصة المعيقة للرفع لسلة."""
    if not text: return ""
    text = _HTML_TAG_RE.sub(" ", str(text))
    text = html.unescape(text)
    # تنظيف المسافات الزائدة
    return re.sub(r"\s+", " ", text).strip()

def format_mahwous_description(product_data: dict) -> str:
    """تنسيق الوصف بأسلوب مهووس الاحترافي (Mahwous Format)."""
    name = sanitize_salla_text(product_data.get("name", "عطر فاخر"))
    brand = sanitize_salla_text(product_data.get("brand", "ماركة عالمية"))
    desc = product_data.get("description", "")
    notes = product_data.get("notes", {}) 
    
    # بناء الهيكل الاحترافي
    lines = [
        f"<h2>{name} من {brand}</h2>",
        f"<p>اكتشف سحر <strong>{name}</strong> من <strong>{brand}</strong> — عطر فاخر يجمع بين الأصالة والتميز. متوفر الآن في متجر مهووس، وجهتك الأولى لأرقى العطور العالمية.</p>",
        "<h3>مميزات المنتج</h3>",
        "<ul>",
        "<li><strong>الأصالة:</strong> عطر أصلي 100% بضمان متجر مهووس.</li>",
        "<li><strong>الأداء:</strong> ثبات عالي وفوحان يأسر الحواس طوال اليوم.</li>",
        "<li><strong>التصميم:</strong> زجاجة أنيقة تعكس فخامة المحتوى.</li>",
        "</ul>"
    ]
    
    if notes and any(notes.values()):
        lines.append("<h3>الهرم العطري (المكونات العطرية)</h3>")
        lines.append("<ul>")
        if notes.get("top"): lines.append(f"<li><strong>الافتتاحية (Top Notes):</strong> {sanitize_salla_text(notes['top'])}</li>")
        if notes.get("heart"): lines.append(f"<li><strong>القلب (Heart Notes):</strong> {sanitize_salla_text(notes['heart'])}</li>")
        if notes.get("base"): lines.append(f"<li><strong>القاعدة (Base Notes):</strong> {sanitize_salla_text(notes['base'])}</li>")
        lines.append("</ul>")
    elif desc:
        lines.append("<h3>📝 وصف العطر</h3>")
        lines.append(f"<p>{sanitize_salla_text(desc)}</p>")

    lines.append("<h3>لمسة خبير من مهووس</h3>")
    lines.append("<p>هذا العطر يمثل التوازن المثالي بين القوة والنعومة. ننصح برشه على نقاط النبض للحصول على أفضل أداء وفوحان.</p>")
    lines.append("<p><strong>عالمك العطري يبدأ من مهووس.</strong> أصلي 100% | شحن سريع داخل السعودية.</p>")
    
    return "\n".join(lines)

def validate_export_product_dataframe(df: pd.DataFrame) -> Tuple[bool, List[str]]:
    issues: List[str] = []
    if df is None or df.empty:
        return False, ["لا توجد بيانات للتحقق أو التصدير."]

    for i, (_, row) in enumerate(df.iterrows()):
        name = (
            str(row.get("منتج_المنافس", "")).strip()
            or str(row.get("المنتج", "")).strip()
            or str(row.get("أسم المنتج", "")).strip()
            or str(row.get("اسم المنتج", "")).strip()
        )
        price = _safe_float(
            row.get("سعر_المنافس", row.get("سعر المنافس", row.get("السعر", 0)))
        )
        if not name or name.lower() in ("nan", "none"):
            issues.append(f"صف {i + 1}: اسم المنتج فارغ")
        if price <= 0:
            issues.append(f"صف {i + 1}: السعر غير صالح")

    return (len(issues) == 0, issues)
