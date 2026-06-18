"""services/missing_service.py — كشف المنتجات المفقودة (نقل _compute_missing_from_store).

خط الأنابيب (#PRESERVED_LOGIC app.py:726-1050):
  1) مرشّحون من المخزن (يُحقَنون كـ ``candidates`` — مصدرهم CompetitorIntelligence).
  2) إزالة تكرار المتاجر بالاسم المجرّد (أرخص سعر).
  3) فلاتر الدقة: غير عطر/مجموعة/سعر متطرف/اسم قصير/بلا حجم/ميني<10مل.
  4) تحقّق ضبابي عبر ``MatchingService``: OWNED⇒إخفاء، REVIEW⇒محتمل، MISSING⇒green.
  5) تخزين قرصي بتوقيع ``F4v2|catalog_len|db_size`` (كتابة ذرّية).

خدمة نقية المنطق: المصدر والقاعدة محقونان، فتُختبر دون قاعدة بيانات حيّة.
"""
from __future__ import annotations

import os
import pickle
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

import pandas as pd

from conf.constants import (
    MISSING_CACHE_VERSION,
    MISSING_MAX_PRICE,
    MISSING_MIN_NAME_LEN,
    MISSING_MIN_PRICE,
    MISSING_MIN_SIZE_ML,
    PROJECT_ROOT,
)
from core.exceptions import MissingDetectionError
from services.matching_service import Ownership, MatchingService, miss_bare

# #PRESERVED_LOGIC: فئات وكلمات الإسقاط الحيّة (app.py:846-848).
_BAD_CLASSES = (
    "deodorant", "hair_mist", "body_mist", "body_lotion",
    "soap", "shower_gel", "after_shave", "rejected", "other",
)
_SET_WORDS = ("مجموعة", "مجموعه", "طقم", "gift set", "gift box", "set ")


@dataclass(frozen=True)
class ClassifyKernel:
    """دوال التصنيف من ``engines.engine`` (نقية، محقونة للاختبار)."""

    classify_product: Callable[[str], str]
    classify_category: Callable[[str], str]
    extract_size: Callable[[str], float]
    extract_brand: Callable[[str], str]
    is_sample: Callable[[str], bool]
    is_tester: Callable[[str], bool]


_CLASSIFY: Optional[ClassifyKernel] = None


def load_classify_kernel() -> ClassifyKernel:
    """يحمّل دوال التصنيف القانونية من ``engines.engine``."""
    global _CLASSIFY
    if _CLASSIFY is not None:
        return _CLASSIFY
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from engines.engine import (  # type: ignore
            classify_product,
            classify_product_category,
            extract_brand,
            extract_size,
            is_sample,
            is_tester,
        )
    except Exception as exc:  # pragma: no cover
        raise MissingDetectionError(
            "تعذّر تحميل دوال التصنيف من engines.engine", error=str(exc),
        ) from exc
    _CLASSIFY = ClassifyKernel(
        classify_product, classify_product_category, extract_size,
        extract_brand, is_sample, is_tester,
    )
    return _CLASSIFY


def is_non_perfume(name: str, price: float, kernel: ClassifyKernel) -> tuple[bool, str]:
    """يقرّر إسقاط المنتج + السبب. #PRESERVED_LOGIC app.py:854-878."""
    if kernel.classify_product(name) in _BAD_CLASSES:
        return True, "class"
    low = name.lower()
    if any(w in low for w in _SET_WORDS):
        return True, "set"
    if price > 0 and (price < MISSING_MIN_PRICE or price > MISSING_MAX_PRICE):
        return True, "price"
    if len(name.strip()) < MISSING_MIN_NAME_LEN:
        return True, "short"
    size = kernel.extract_size(name)
    if not size or size <= 0:
        return True, "nosize"
    if size < MISSING_MIN_SIZE_ML:
        return True, "mini"
    return False, ""


def item_type(name: str, kernel: ClassifyKernel) -> str:
    """يصنّف نوع السلعة. #PRESERVED_LOGIC app.py:880-886."""
    low = name.lower()
    if kernel.is_sample(name) or "ديكانت" in name or "تقسيم" in name:
        return "sample"
    if kernel.is_tester(name) or "تستر" in name or "tester" in low:
        return "tester"
    return "retail"


def missing_signature(catalog_len: int, db_size: int) -> str:
    """توقيع الكاش ``F4v2|catalog_len|db_size``. #PRESERVED_LOGIC app.py:770."""
    return f"{MISSING_CACHE_VERSION}|{catalog_len}|{db_size}"


def load_cache(path: str, signature: str) -> Optional[pd.DataFrame]:
    """يقرأ كاش المفقودات إن طابق التوقيع. #PRESERVED_LOGIC app.py:773-782."""
    if not signature or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as handle:
            cached = pickle.load(handle)
        if (isinstance(cached, dict) and cached.get("sig") == signature
                and isinstance(cached.get("df"), pd.DataFrame)):
            return cached["df"]
    except Exception:
        return None
    return None


def save_cache(path: str, signature: str, df: pd.DataFrame) -> None:
    """كتابة ذرّية: ملف مؤقت ثم استبدال. #PRESERVED_LOGIC app.py:1040-1049."""
    if not signature:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as handle:
            pickle.dump({"sig": signature, "df": df}, handle,
                        protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception:
        pass


class MissingService:
    """خدمة كشف المفقودات: تنقّي المرشّحين وتصنّفهم عبر المطابقة."""

    def __init__(
        self,
        matching: MatchingService,
        classify_kernel: Optional[ClassifyKernel] = None,
    ) -> None:
        self._match = matching
        self._ck = classify_kernel or load_classify_kernel()

    def _dedup(self, candidates: list[dict[str, Any]]) -> dict[str, tuple[dict, float]]:
        """دمج المرشّحين بالاسم المجرّد مع أرخص سعر. #PRESERVED_LOGIC app.py:833-842."""
        merged: dict[str, tuple[dict, float]] = {}
        for cand in candidates:
            bare = miss_bare(cand.get("product_name", ""), self._match.kernel)
            if not bare:
                continue
            price = float(cand.get("min_price", 0) or 0)
            existing = merged.get(bare)
            if existing is None or price < existing[1]:
                merged[bare] = (cand, price)
        return merged

    def compute(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """يُنتج صفوف المفقودات. OWNED يُسقَط، REVIEW/MISSING يبقيان."""
        rows: list[dict[str, Any]] = []
        for cand, price in self._dedup(candidates).values():
            name = str(cand.get("product_name", "") or "")
            dropped, _reason = is_non_perfume(name, float(price or 0), self._ck)
            if dropped:
                continue
            outcome = self._match.evaluate(name, str(cand.get("brand", "") or ""))
            if outcome.ownership is Ownership.OWNED:
                continue  # نملكه باسم مختلف ⇒ ليس مفقوداً
            rows.append(self._build_row(name, price, cand, outcome))
        return rows

    def _build_row(
        self, name: str, price: float, cand: dict[str, Any], outcome: Any,
    ) -> dict[str, Any]:
        """يبني صف مفقود واحد بمخطّط app.py الحرفي. #PRESERVED_LOGIC app.py:992-1016."""
        comp_list = cand.get("competitors_list") or []
        brand = str(cand.get("brand", "") or "").strip()
        if not brand or brand.lower() in ("nan", "none", "غير محدد"):
            brand = self._ck.extract_brand(name) or ""
        is_review = outcome.ownership is Ownership.REVIEW
        return {
            "منتج_المنافس": name,
            "سعر_المنافس": price,
            "الماركة": brand,
            "المنافس": (comp_list[0] if comp_list else "")
            or f"{cand.get('competitor_count', 1)} متجر",
            "المنافسون": "، ".join(str(x).strip() for x in comp_list if str(x).strip()),
            "تصنيف_المنتج": str(cand.get("category", "") or "").strip()
            or self._ck.classify_category(name),
            "صورة_المنافس": str(cand.get("image_url", "") or ""),
            "السعر_المقترح": float(cand.get("suggested_price", 0) or 0),
            "مستوى_الثقة": "review" if is_review else "green",
            "درجة_التشابه": outcome.score,
            "منتج_مطابق_محتمل": outcome.our_match or "",
            "حالة_المراجعة": outcome.reason,
            "هو_تستر": item_type(name, self._ck) == "tester",
            "نوع_السلعة": item_type(name, self._ck),
            "عدد_المنافسين": int(cand.get("competitor_count", 1) or 1),
        }

    @staticmethod
    def to_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
        """يحوّل الصفوف إلى DataFrame (فارغ آمن)."""
        return pd.DataFrame(rows)
