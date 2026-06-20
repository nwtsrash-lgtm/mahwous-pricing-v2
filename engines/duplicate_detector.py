"""
engines/duplicate_detector.py
3-layer duplicate detection:
  1. Exact key match (instant, deterministic)
  2. Fuzzy similarity via RapidFuzz (>=95 → duplicate, 85-94 → review)
  3. Semantic AI verification (only for borderline cases)

Returns a structured Verdict so the smart router can act without ambiguity.
"""
from dataclasses import dataclass
from typing import Iterable, Optional, Callable

from utils.product_key import make_product_key, normalize_text

try:
    from rapidfuzz import fuzz, process
    _HAS_FUZZ = True
except Exception:
    _HAS_FUZZ = False


DUPLICATE_THRESHOLD = 95.0   # >= → DUPLICATE
REVIEW_THRESHOLD = 85.0      # 85-94 → REVIEW
MATCH_THRESHOLD = 70.0       # 70-84 → possibly matched, ask AI
SEMANTIC_THRESHOLD = 60.0    # < 60 → NEW (genuinely missing)


@dataclass
class Verdict:
    decision: str            # DUPLICATE | REVIEW | MATCHED | NEW
    confidence: float        # 0..100
    duplicate_of: Optional[str] = None   # product_key of original
    matched_with: Optional[str] = None   # name of catalog item
    layer: str = "exact"     # exact | fuzzy | semantic
    reason: str = ""

    def to_dict(self):
        return {
            "decision": self.decision, "confidence": self.confidence,
            "duplicate_of": self.duplicate_of, "matched_with": self.matched_with,
            "layer": self.layer, "reason": self.reason,
        }


def _fuzzy_best(name: str, candidates: list) -> tuple:
    """Returns (best_name, score, idx) using RapidFuzz, or (None, 0, -1)."""
    if not _HAS_FUZZ or not candidates:
        return (None, 0.0, -1)
    norm_name = normalize_text(name)
    norm_pool = [normalize_text(c.get("name", "")) for c in candidates]
    best = process.extractOne(norm_name, norm_pool, scorer=fuzz.token_set_ratio)
    if not best:
        return (None, 0.0, -1)
    matched_str, score, idx = best
    return (candidates[idx].get("name", ""), float(score), idx)


def detect(product: dict,
           catalog: list,
           existing_keys: Optional[set] = None,
           ai_verify: Optional[Callable[[str, str], bool]] = None) -> Verdict:
    """
    product:        {name, store, url, ...}
    catalog:        [{name, key?, ...}, ...] — our products / known items
    existing_keys:  set of product_keys already stored (Layer 1)
    ai_verify:      optional callable(name_a, name_b) -> bool, for Layer 3
    """
    name = product.get("name", "") or product.get("product_name", "")
    store = product.get("store", "") or product.get("competitor", "")
    url = product.get("url", "") or product.get("link", "")
    key = make_product_key(name, store, url)

    # Layer 1: exact key duplicate
    if existing_keys and key in existing_keys:
        return Verdict("DUPLICATE", 100.0, duplicate_of=key,
                       layer="exact", reason="نفس مفتاح المنتج موجود مسبقاً")

    # Layer 2: fuzzy match against catalog
    matched_name, score, idx = _fuzzy_best(name, catalog or [])

    if score >= DUPLICATE_THRESHOLD:
        dup_key = (catalog[idx].get("key")
                   or make_product_key(matched_name,
                                       catalog[idx].get("store", ""),
                                       catalog[idx].get("url", "")))
        return Verdict("DUPLICATE", score, duplicate_of=dup_key,
                       matched_with=matched_name, layer="fuzzy",
                       reason=f"تشابه نصي {score:.1f}%")

    if score >= REVIEW_THRESHOLD:
        return Verdict("REVIEW", score, matched_with=matched_name, layer="fuzzy",
                       reason=f"تشابه متوسط {score:.1f}% — يحتاج تحقق")

    # Layer 3: semantic AI verify for borderline (70-84)
    if score >= MATCH_THRESHOLD and ai_verify and matched_name:
        try:
            same = bool(ai_verify(name, matched_name))
            if same:
                dup_key = (catalog[idx].get("key")
                           or make_product_key(matched_name,
                                               catalog[idx].get("store", ""),
                                               catalog[idx].get("url", "")))
                return Verdict("DUPLICATE", max(score, 90.0),
                               duplicate_of=dup_key, matched_with=matched_name,
                               layer="semantic", reason="AI أكّد أنه نفس المنتج")
            return Verdict("REVIEW", score, matched_with=matched_name,
                           layer="semantic", reason="AI غير متأكد")
        except Exception as e:
            return Verdict("REVIEW", score, matched_with=matched_name,
                           layer="semantic", reason=f"خطأ AI: {e}")

    if score >= MATCH_THRESHOLD:
        return Verdict("REVIEW", score, matched_with=matched_name, layer="fuzzy",
                       reason=f"تشابه ضعيف {score:.1f}% — يحتاج مراجعة")

    return Verdict("NEW", score, layer="fuzzy",
                   reason="منتج جديد — لا يوجد تشابه كافٍ")
