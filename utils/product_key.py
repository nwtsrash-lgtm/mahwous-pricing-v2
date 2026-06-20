"""
utils/product_key.py
Stable identity key for any product across stores and sources.
Used as the single source of truth to prevent duplicates from the root.
"""
import hashlib
import re
import unicodedata
from urllib.parse import urlparse, urlunparse

_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_NON_ALNUM = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)
_MULTI_SPACE = re.compile(r"\s+")

_ARABIC_NORMALIZE = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه",
})


def normalize_text(s) -> str:
    if not s:
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFKC", s)
    s = _ARABIC_DIACRITICS.sub("", s)
    s = s.translate(_ARABIC_NORMALIZE)
    s = _NON_ALNUM.sub(" ", s)
    s = _MULTI_SPACE.sub(" ", s).strip()
    return s


def normalize_url(u) -> str:
    if not u:
        return ""
    try:
        p = urlparse(str(u).strip().lower())
        netloc = p.netloc.replace("www.", "")
        path = p.path.rstrip("/")
        return urlunparse((p.scheme or "https", netloc, path, "", "", ""))
    except Exception:
        return str(u).strip().lower()


def make_product_key(name="", store="", url="") -> str:
    """Deterministic SHA1 key — same product → same key, always."""
    n = normalize_text(name)
    s = normalize_text(store)
    u = normalize_url(url)
    raw = f"{n}|{s}|{u}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
