"""
utils/product_analyzer.py — التحليل الموضعي الكامل مع أزرار الاقتراح الذكي (الوحدة الرابعة)
══════════════════════════════════════════════════════════════════════════════════════════════
✅ تحليل شامل: سعر + مطابقة + قسم صحيح + توصية
✅ أزرار اقتراح ذكية قابلة للتنفيذ الفوري من الواجهة
✅ ربط مباشر بـ DB عبر save_processed / save_hidden_product
✅ طبقة التنظيف الشاملة (sanitize_full_description) مدمجة
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import pandas as pd


# ══════════════════════════════════════════════════════════════════════════════
#  التحليل الموضعي الرئيسي
# ══════════════════════════════════════════════════════════════════════════════

def analyze_product_inline(
    our_name: str,
    our_price: float,
    comp_name: str,
    comp_price: float,
    comp_source: str,
    match_pct: float,
    brand: str = "",
    section: str = "general",
    product_id: str = "",
    results_df: Optional[pd.DataFrame] = None,
) -> dict:
    """
    تحليل شامل لمنتج بعينه — يُستدعى من زر [📊 تحليل] في بطاقة المنتج.

    الخطوات:
    1. ai_deep_analysis  → تحليل عميق بالسياق
    2. verify_match      → هل المطابقة صحيحة؟ وما القسم الصحيح؟
    3. suggest_price     → السعر الأمثل بالرقم
    4. توليد أزرار الاقتراح بناءً على النتائج

    Returns dict:
        success, match_valid, correct_section, suggested_price,
        analysis, price_verdict, recommendation, confidence,
        actions: List[dict]  ← الأزرار المقترحة
        error
    """
    result: dict = {
        "success":         False,
        "match_valid":     None,
        "correct_section": "",
        "suggested_price": 0.0,
        "analysis":        "",
        "price_verdict":   "",
        "recommendation":  "",
        "confidence":      int(match_pct),
        "section_mismatch": "",
        "actions":         [],      # ← قائمة الأزرار المقترحة
        "error":           None,
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M"),
        # metadata لاستخدام الأزرار
        "_our_name":    our_name,
        "_our_price":   our_price,
        "_comp_name":   comp_name,
        "_comp_price":  comp_price,
        "_comp_source": comp_source,
        "_product_id":  product_id,
        "_section":     section,
        "_brand":       brand,
    }

    try:
        from engines.ai_engine import ai_deep_analysis, suggest_price, verify_match
    except ImportError as e:
        result["error"] = f"تعذّر تحميل AI: {e}"
        return result

    # ── 1. التحليل العميق ────────────────────────────────────────────────────
    try:
        raw = ai_deep_analysis(
            our_product=our_name, our_price=our_price,
            comp_product=comp_name, comp_price=comp_price,
            section=section, brand=brand,
        )
        if isinstance(raw, dict):
            result["analysis"]   = str(raw.get("response", raw.get("analysis", "")))
            result["confidence"] = int(raw.get("confidence", match_pct))
        else:
            result["analysis"] = str(raw)
    except Exception as e:
        result["analysis"] = f"[تعذّر التحليل: {e}]"

    # ── 2. التحقق من صحة المطابقة والقسم ─────────────────────────────────────
    try:
        vr = verify_match(our_name, comp_name, our_price, comp_price)
        if vr.get("success"):
            result["match_valid"]     = bool(vr.get("match", True))
            result["correct_section"] = str(vr.get("correct_section", ""))
            result["confidence"]      = int(vr.get("confidence", match_pct))
    except Exception:
        pass

    # ── 3. اقتراح السعر ──────────────────────────────────────────────────────
    try:
        pr = suggest_price(our_name, comp_price)
        if isinstance(pr, dict) and pr.get("success"):
            result["suggested_price"] = float(pr.get("suggested_price", 0) or 0)
            result["recommendation"]  = str(pr.get("recommendation", ""))
        elif isinstance(pr, (int, float)):
            result["suggested_price"] = float(pr)
    except Exception:
        pass

    # إذا لم نحصل على سعر مقترح → احسب تلقائياً
    if not result["suggested_price"] and comp_price > 0:
        result["suggested_price"] = round(comp_price - 1, 0)

    # ── 4. حكم السعر ─────────────────────────────────────────────────────────
    if our_price > 0 and comp_price > 0:
        diff    = our_price - comp_price
        diff_pc = diff / comp_price * 100
        if diff > 5:
            result["price_verdict"] = (
                f"🔴 سعرنا أعلى بـ {diff:.0f} ر.س ({diff_pc:.1f}%)"
            )
        elif diff < -5:
            result["price_verdict"] = (
                f"🟢 سعرنا أقل بـ {abs(diff):.0f} ر.س ({abs(diff_pc):.1f}%) — فرصة رفع"
            )
        else:
            result["price_verdict"] = f"✅ سعر تنافسي (فرق {diff:+.0f} ر.س)"

    # ── 5. تحذير القسم ───────────────────────────────────────────────────────
    _section_labels = {
        "raise":    "🔴 سعر أعلى",
        "lower":    "🟢 سعر أقل",
        "approved": "✅ موافق عليها",
        "missing":  "🔍 منتجات مفقودة",
        "review":   "⚠️ تحت المراجعة",
        "excluded": "⚪ مستبعد",
        "price_raise": "🔴 سعر أعلى",
        "price_lower": "🟢 سعر أقل",
        "general": "",
    }
    cur_ar  = _section_labels.get(section, section)
    corr_ar = result["correct_section"]
    if corr_ar and cur_ar and corr_ar != cur_ar:
        result["section_mismatch"] = (
            f"⚠️ المنتج في **{cur_ar}** لكن AI يرى أنه ينتمي لـ **{corr_ar}**"
        )

    # ── 6. توليد أزرار الاقتراح الذكية ──────────────────────────────────────
    result["actions"] = _build_action_buttons(result)

    result["success"] = True
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  توليد أزرار الاقتراح
# ══════════════════════════════════════════════════════════════════════════════

def _build_action_buttons(result: dict) -> list[dict]:
    """
    يُولّد قائمة أزرار اقتراح بناءً على نتيجة التحليل.
    كل زر: {"key", "label", "action", "params", "style"}
    """
    actions = []
    uid = str(uuid.uuid4())[:6]

    our_name   = result["_our_name"]
    our_price  = result["_our_price"]
    comp_name  = result["_comp_name"]
    comp_price = result["_comp_price"]
    comp_src   = result["_comp_source"]
    prod_id    = result["_product_id"]
    section    = result["_section"]
    sugg_price = result.get("suggested_price", 0) or (
        round(comp_price - 1, 0) if comp_price > 0 else our_price
    )

    # — زر تحديث السعر المقترح —
    if sugg_price > 0 and abs(sugg_price - our_price) > 1:
        verb = "خفض" if sugg_price < our_price else "رفع"
        actions.append({
            "key":    f"act_price_{uid}",
            "label":  f"🤖 {verb} السعر → {sugg_price:.0f} ر.س",
            "action": "update_price",
            "params": {
                "product_name": our_name,
                "product_id":   prod_id,
                "competitor":   comp_src,
                "old_price":    our_price,
                "new_price":    sugg_price,
                "notes":        f"[AI] {verb} السعر من {our_price:.0f} → {sugg_price:.0f}",
            },
            "style": "primary",
        })

    # — زر نقل للمنتجات المفقودة (إذا كانت المطابقة خاطئة) —
    if result.get("match_valid") is False:
        actions.append({
            "key":    f"act_missing_{uid}",
            "label":  "🤖 نقل → منتجات مفقودة",
            "action": "move_to_missing",
            "params": {
                "product_name": our_name,
                "product_id":   prod_id,
                "competitor":   comp_src,
                "old_price":    our_price,
                "new_price":    comp_price,
                "notes":        "[AI] مطابقة خاطئة — نُقل للمفقودين",
            },
            "style": "secondary",
        })

    # — زر موافقة (إذا كانت المطابقة صحيحة وفرق السعر ضئيل) —
    if result.get("match_valid") is True and abs(our_price - comp_price) <= 10:
        actions.append({
            "key":    f"act_approve_{uid}",
            "label":  "🤖 موافق — السعر تنافسي",
            "action": "approve",
            "params": {
                "product_name": our_name,
                "product_id":   prod_id,
                "competitor":   comp_src,
                "old_price":    our_price,
                "new_price":    our_price,
                "notes":        "[AI] موافقة تلقائية — السعر ضمن النطاق التنافسي",
            },
            "style": "primary",
        })

    # — زر نقل للقسم الصحيح —
    if result.get("section_mismatch"):
        correct = result.get("correct_section", "")
        section_action_map = {
            "🔴 سعر أعلى":        "move_to_raise",
            "سعر اعلى":           "move_to_raise",
            "🟢 سعر أقل":         "move_to_lower",
            "سعر اقل":            "move_to_lower",
            "🔍 منتجات مفقودة":  "move_to_missing",
            "مفقود":              "move_to_missing",
            "✅ موافق":           "approve",
            "موافق":              "approve",
        }
        act = section_action_map.get(correct, "")
        if act:
            actions.append({
                "key":    f"act_move_{uid}",
                "label":  f"🤖 نقل → {correct}",
                "action": act,
                "params": {
                    "product_name": our_name,
                    "product_id":   prod_id,
                    "competitor":   comp_src,
                    "old_price":    our_price,
                    "new_price":    sugg_price if "price" in act else our_price,
                    "notes":        f"[AI] نُقل إلى {correct}",
                },
                "style": "secondary",
            })

    # — زر تجاهل (دائماً متاح) —
    actions.append({
        "key":    f"act_ignore_{uid}",
        "label":  "⏸️ تجاهل التوصية",
        "action": "ignore",
        "params": {"product_name": our_name, "notes": "[AI] تجاهل التوصية"},
        "style":  "tertiary",
    })

    return actions


# ══════════════════════════════════════════════════════════════════════════════
#  تنفيذ إجراء الزر (مرتبط بـ DB)
# ══════════════════════════════════════════════════════════════════════════════

def execute_action(action: str, params: dict) -> dict:
    """
    يُنفّذ إجراء الزر ويحفظه في قاعدة البيانات.

    Args:
        action: اسم الإجراء (update_price / approve / move_to_missing / ...)
        params: معاملات الإجراء

    Returns:
        {"success": bool, "message": str}
    """
    try:
        from utils.db_manager import save_processed, save_hidden_product
    except ImportError as e:
        return {"success": False, "message": f"تعذّر تحميل DB: {e}"}

    pname   = str(params.get("product_name", ""))
    pid     = str(params.get("product_id", ""))
    comp    = str(params.get("competitor", ""))
    old_p   = float(params.get("old_price", 0))
    new_p   = float(params.get("new_price", 0))
    notes   = str(params.get("notes", ""))

    try:
        if action == "ignore":
            return {"success": True, "message": "تم تجاهل التوصية بنجاح"}

        # تحويل اسم الإجراء إلى الحالة المناسبة في DB
        status_map = {
            "update_price":    "send_price",
            "approve":         "approved",
            "move_to_missing": "missing",
            "move_to_raise":   "price_raise",
            "move_to_lower":   "price_lower",
        }
        status = status_map.get(action, action)

        # 1. حفظ في جدول المعالجة
        # FIX: use keyword args to match save_processed() signature correctly
        save_processed(
            product_key=pid or pname,
            product_name=pname,
            competitor=comp,
            action=status,
            old_price=old_p,
            new_price=new_p,
            product_id=pid,
            notes=notes,
        )
        # 2. إخفاء من الواجهة
        save_hidden_product(pname, pid, comp)

        return {"success": True, "message": f"تم تنفيذ الإجراء: {status}"}

    except Exception as e:
        return {"success": False, "message": f"خطأ في التنفيذ: {e}"}


# ══════════════════════════════════════════════════════════════════════════════
#  عرض نتيجة التحليل في Streamlit
# ══════════════════════════════════════════════════════════════════════════════

def render_analysis_result(result: dict, container=None) -> None:
    """
    يعرض نتيجة analyze_product_inline() داخل Streamlit مع أزرار الأكشن.
    """
    import streamlit as st
    c = container or st

    if not result.get("success"):
        c.error(f"❌ {result.get('error', 'فشل التحليل')}")
        return

    # حكم السعر
    if result.get("price_verdict"):
        c.markdown(
            f'<div style="background:#0d1b2a;border-radius:8px;padding:10px 14px;'
            f'font-size:.88rem;margin-bottom:6px">'
            f'{result["price_verdict"]}</div>',
            unsafe_allow_html=True,
        )

    # تحذير القسم
    if result.get("section_mismatch"):
        c.warning(result["section_mismatch"])

    # تحليل AI
    if result.get("analysis"):
        c.markdown(
            f'<div style="background:#091929;border:1px solid #1e3a5f;'
            f'border-radius:8px;padding:12px 14px;font-size:.85rem;'
            f'line-height:1.7;white-space:pre-wrap">'
            f'🤖 <b>تحليل AI:</b><br>{result["analysis"][:800]}</div>',
            unsafe_allow_html=True,
        )

    # السعر المقترح
    sp = result.get("suggested_price", 0)
    if sp and sp > 0:
        c.success(f"💰 **السعر المقترح: {sp:,.0f} ر.س**")
        if result.get("recommendation"):
            c.caption(result["recommendation"][:300])

    # مصداقية المطابقة
    if result.get("match_valid") is False:
        c.error("🔵 AI: المطابقة خاطئة — يجب نقل المنتج للمنتجات المفقودة")
    elif result.get("match_valid") is True:
        c.success(f"✅ AI: مطابقة صحيحة ({result.get('confidence', 0)}%)")

    # ── أزرار الاقتراح الذكية ───────────────────────────────────────────
    if result.get("actions"):
        st.markdown("---")
        st.caption("🚀 إجراءات مقترحة:")
        # توزيع الأزرار في أعمدة
        cols = st.columns(len(result["actions"]))
        for idx, act in enumerate(result["actions"]):
            with cols[idx]:
                if st.button(
                    act["label"],
                    key=act["key"],
                    use_container_width=True,
                    type="primary" if act["style"] == "primary" else "secondary",
                ):
                    res = execute_action(act["action"], act["params"])
                    if res["success"]:
                        st.success(res["message"])
                        st.rerun()
                    else:
                        st.error(res["message"])
