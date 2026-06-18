"""services/matching_service.py — منطق المطابقة الضبابية (نقل حرفي من app.py).

نقل دقيق لنواة المطابقة المستخدمة في كشف المفقودات:
- التطبيع: ``miss_bare`` / ``miss_toks`` / ``ar_skeleton`` / ``skel_toks``.
- الفهرس المقلوب: حجب بالكلمة + بالهيكل العظمي + بالماركة (token-blocking).
- الحُرّاس: الماركة (F1)، الحجم (تسامح 8.0مل)، تعارض الجنس.
- قرار الملكية الثلاثي: OWNED (≥82) / REVIEW (65-82) / MISSING (<65).

خدمة نقية: لا تستورد Streamlit. تعيد استخدام مطبّعات ``engines.engine``
القانونية (نقية بدورها) عبر نواة قابلة للحقن لتفادي أي ازدواج منطق.

#PRESERVED_LOGIC: مأخوذ حرفياً من _compute_missing_from_store (app.py:813-982)
ومن _miss_bare/_miss_toks/_ar_skeleton/_skel_toks (app.py:624-660).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Optional

from rapidfuzz import fuzz, process

from conf.constants import (
    AR_WEAK_CHARS,
    MATCH_CONFIRMED_THRESHOLD,
    MATCH_REVIEW_THRESHOLD,
    MISS_STOPWORDS,
    PROJECT_ROOT,
    SIZE_TOLERANCE_ML,
)
from core.exceptions import MatchingError

# #PRESERVED_LOGIC: جدول إزالة الحروف الضعيفة (app.py:642).
_AR_WEAK_TABLE = str.maketrans("", "", AR_WEAK_CHARS)
_DIGITS_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class EngineKernel:
    """مطبّعات المطابقة القانونية المحقونة (من ``engines.engine`` افتراضياً)."""

    normalize: Callable[[str], str]
    normalize_name: Callable[[str], str]
    extract_size: Callable[[str], float]
    extract_brand_fast: Callable[[str], str]
    extract_gender: Callable[[str], str]


_KERNEL: Optional[EngineKernel] = None


def load_engine_kernel() -> EngineKernel:
    """يحمّل مطبّعات ``engines.engine`` (نقية، بلا Streamlit) ويخزّنها."""
    global _KERNEL
    if _KERNEL is not None:
        return _KERNEL
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from engines.engine import (  # type: ignore
            extract_brand_fast,
            extract_gender,
            extract_size,
            normalize,
            normalize_name,
        )
    except Exception as exc:  # pragma: no cover - بيئة بلا engines
        raise MatchingError(
            "تعذّر تحميل نواة المطابقة من engines.engine", error=str(exc),
        ) from exc
    _KERNEL = EngineKernel(
        normalize, normalize_name, extract_size, extract_brand_fast, extract_gender,
    )
    return _KERNEL


# ════════════════════════════════════════════════════════════════════
#  بدائيات التطبيع (#PRESERVED_LOGIC — app.py:624-660)
# ════════════════════════════════════════════════════════════════════
def miss_bare(name: str, kernel: EngineKernel) -> str:
    """اسم مجرّد: تطبيع ثم إسقاط الكلمات الشائعة/الأرقام/القصيرة (<2)."""
    return " ".join(
        t
        for t in kernel.normalize_name(str(name)).split()
        if t not in MISS_STOPWORDS and not _DIGITS_RE.fullmatch(t) and len(t) >= 2
    )


def miss_toks(bare: str) -> list[str]:
    """أهم 4 كلمات دالّة (≥4 أحرف) للحجب."""
    return [t for t in bare.split() if len(t) >= 4][:4]


def ar_skeleton(tok: str) -> str:
    """هيكل عظمي بإزالة الحروف العربية الضعيفة (اللاتيني يبقى كما هو)."""
    sk = str(tok).translate(_AR_WEAK_TABLE)
    return sk if len(sk) >= 2 else str(tok)


def skel_toks(bare: str) -> list[str]:
    """كلمات الحجب بالهيكل العظمي (≥3 أحرف، أعلى 6)."""
    out: list[str] = []
    for tok in bare.split():
        sk = ar_skeleton(tok)
        if len(sk) >= 3 and sk not in out:
            out.append(sk)
        if len(out) >= 6:
            break
    return out


# ════════════════════════════════════════════════════════════════════
#  الحُرّاس (#PRESERVED_LOGIC — app.py:947,957)
# ════════════════════════════════════════════════════════════════════
def size_ok(c_size: float, o_size: float, tol: float = SIZE_TOLERANCE_ML) -> bool:
    """متوافقان حجماً إن غاب أحد الحجمين أو كان الفرق ضمن التسامح (8.0مل)."""
    return (not c_size) or (not o_size) or abs(c_size - o_size) <= tol


def gender_conflict(c_gender: str, o_gender: str) -> bool:
    """تعارض جنس فقط حين يكون الجنسان محدَّدين صراحةً ومختلفين."""
    return bool(c_gender) and bool(o_gender) and c_gender != o_gender


class Ownership(str, Enum):
    """نتيجة قرار الملكية الثلاثي."""

    OWNED = "owned"      # ≥82 + حجم متوافق + لا تعارض جنس ⇒ نملكه (إخفاء)
    REVIEW = "review"    # 65-82 (وجود مرشّح) ⇒ محتمل موجود (يبقى للمراجعة)
    MISSING = "missing"  # <65 ⇒ مفقود مؤكد


@dataclass(frozen=True)
class OurItem:
    """عنصر من كتالوجنا داخل الفهرس."""

    bare: str
    brand_n: str
    size: float
    raw: str


@dataclass(frozen=True)
class MatchOutcome:
    """مخرجات تقييم منتج منافس مقابل كتالوجنا."""

    ownership: Ownership
    score: float
    our_match: Optional[str]
    reason: str


def decide_ownership(
    score: float,
    has_item: bool,
    size_okay: bool,
    gender_clash: bool,
    confirm: float = MATCH_CONFIRMED_THRESHOLD,
    review_min: float = MATCH_REVIEW_THRESHOLD,
) -> tuple[Ownership, str]:
    """يقرّر الملكية + سبب المراجعة. #PRESERVED_LOGIC app.py:958-982."""
    if score >= confirm and size_okay and not gender_clash:
        return Ownership.OWNED, ""
    if has_item and score >= review_min:
        if gender_clash:
            reason = "جنس مختلف — تأكيد بشري"
        elif score >= confirm and not size_okay:
            reason = "متوفّر بحجم مختلف"
        else:
            reason = "بانتظار التحقق"
        return Ownership.REVIEW, reason
    return Ownership.MISSING, ""


class OurCatalogIndex:
    """فهرس كتالوجنا الغني: مقلوب بالكلمة + بالهيكل + بالماركة.

    #PRESERVED_LOGIC: بناء الفهرس والحجب الموسّع (app.py:813-832, 906-943).
    """

    def __init__(self, kernel: EngineKernel) -> None:
        self._k = kernel
        self.items: list[OurItem] = []
        self._inv: dict[str, set[int]] = {}
        self._skel: dict[str, set[int]] = {}
        self._brand: dict[str, list[int]] = {}

    @classmethod
    def build(cls, names: Iterable[str], kernel: EngineKernel) -> "OurCatalogIndex":
        index = cls(kernel)
        for name in names:
            index._add(str(name))
        return index

    def _add(self, our_name: str) -> None:
        bare = miss_bare(our_name, self._k)
        if not bare:
            return
        idx = len(self.items)
        brand_n = self._k.normalize(self._k.extract_brand_fast(our_name) or "")
        self.items.append(
            OurItem(bare, brand_n, self._k.extract_size(our_name), our_name)
        )
        for tok in miss_toks(bare):
            self._inv.setdefault(tok, set()).add(idx)
        for tok in skel_toks(bare):
            self._skel.setdefault(tok, set()).add(idx)
        if brand_n:
            self._brand.setdefault(brand_n, []).append(idx)

    def candidate_indices(self, bare: str, brand_n: str) -> set[int]:
        """حجب موسّع بالكلمة (≤200) ثم بالهيكل (≤300) ثم بالماركة (≤200)."""
        cidx: set[int] = set()
        for tok in miss_toks(bare):
            block = self._inv.get(tok)
            if block:
                cidx |= block
            if len(cidx) > 200:
                break
        for tok in skel_toks(bare):
            block = self._skel.get(tok)
            if block:
                cidx |= block
            if len(cidx) > 300:
                break
        if brand_n:
            cidx.update(self._brand.get(brand_n, [])[:200])
        return cidx

    def best_match(
        self, bare: str, cidx: set[int], brand_n: str,
    ) -> tuple[float, Optional[OurItem]]:
        """أفضل تطابق ضبابي مع حارس الماركة F1 (token_set_ratio)."""
        if not cidx:
            return 0.0, None
        cidx_list = list(cidx)
        if brand_n:  # حارس الماركة: قصر المقارنة على نفس الماركة أو مجهولها
            cidx_list = [
                i for i in cidx_list
                if (not self.items[i].brand_n) or self.items[i].brand_n == brand_n
            ]
        if not cidx_list:
            return 0.0, None
        bares = [self.items[i].bare for i in cidx_list]
        match = process.extractOne(bare, bares, scorer=fuzz.token_set_ratio)
        if not match:
            return 0.0, None
        return float(match[1]), self.items[cidx_list[match[2]]]


class MatchingService:
    """خدمة المطابقة: تبني فهرس كتالوجنا وتقرّر ملكيتنا لمنتج منافس."""

    def __init__(
        self,
        our_names: Iterable[str],
        *,
        kernel: Optional[EngineKernel] = None,
        confirm: float = MATCH_CONFIRMED_THRESHOLD,
        review_min: float = MATCH_REVIEW_THRESHOLD,
        size_tol: float = SIZE_TOLERANCE_ML,
    ) -> None:
        self._k = kernel or load_engine_kernel()
        self._index = OurCatalogIndex.build(our_names, self._k)
        self._confirm = confirm
        self._review_min = review_min
        self._size_tol = size_tol

    @property
    def index(self) -> OurCatalogIndex:
        return self._index

    @property
    def kernel(self) -> EngineKernel:
        """نواة التطبيع المستخدمة (لإعادة الاستخدام في missing_service)."""
        return self._k

    def evaluate(self, comp_name: str, comp_brand: str = "") -> MatchOutcome:
        """يقيّم منتج منافس → OWNED/REVIEW/MISSING. #PRESERVED_LOGIC app.py:898-982."""
        k = self._k
        bare = miss_bare(comp_name, k)
        if not bare:
            return MatchOutcome(Ownership.MISSING, 0.0, None, "")
        brand_n = k.normalize(
            k.extract_brand_fast(comp_name) or k.extract_brand_fast(comp_brand) or ""
        )
        c_size = k.extract_size(comp_name)
        cidx = self._index.candidate_indices(bare, brand_n)
        score, item = self._index.best_match(bare, cidx, brand_n)
        o_size = item.size if item else 0.0
        s_ok = size_ok(c_size, o_size, self._size_tol)
        o_gender = k.extract_gender(item.raw) if item else ""
        g_clash = gender_conflict(k.extract_gender(comp_name), o_gender)
        ownership, reason = decide_ownership(
            score, item is not None, s_ok, g_clash, self._confirm, self._review_min,
        )
        return MatchOutcome(ownership, round(score, 1), item.raw if item else None, reason)
