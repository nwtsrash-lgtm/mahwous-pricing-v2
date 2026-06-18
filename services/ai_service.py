"""services/ai_service.py — تنسيق مزودات الذكاء الاصطناعي (تدوير/دفعات/كاش).

ينسّق نداءات AI مع:
- سلسلة تجاوز فشل: OpenRouter → Gemini → Cohere (الترتيب الحيّ v33، app/ai_engine).
- إعادة محاولة (افتراضي 3) لكل مزوّد، مهلة منطقية، حجم دفعة 8، حد 150 عنصراً.
- تخزين مؤقت بمفتاح تجزئة (نظام+سؤال) لتفادي إعادة النداء.

⚠️ عند فشل كل المزودات: تُعيد نتيجة فاشلة (لا حذف صامت) — يقرّر المُستدعي
الإبقاء في المراجعة. المزودات والكاش محقونة ⇒ الخدمة تُختبر دون شبكة.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Protocol

from conf.constants import PROJECT_ROOT
from conf.settings import Settings
from core.exceptions import AIServiceError


class AICache(Protocol):
    """واجهة كاش بسيطة (get/set نصّي)."""

    def get(self, key: str) -> Optional[str]: ...
    def set(self, key: str, value: str) -> None: ...


class InMemoryCache:
    """كاش ذاكرة افتراضي (يكفي ضمن الجلسة الواحدة)."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value


@dataclass(frozen=True)
class _Provider:
    """مزوّد AI: اسم + دالة نداء تُعيد نصاً أو None عند الفشل."""

    name: str
    fn: Callable[[str, str], Optional[str]]

    def __call__(self, prompt: str, system: str = "") -> Optional[str]:
        return self.fn(prompt, system)


@dataclass(frozen=True)
class AIResult:
    """نتيجة نداء AI: نجاح + النص + المصدر (المزوّد أو cache)."""

    success: bool
    response: str
    source: str


def _hash_key(system: str, prompt: str) -> str:
    """مفتاح كاش ثابت من النظام + السؤال."""
    return hashlib.sha256(f"{system}\x00{prompt}".encode("utf-8")).hexdigest()


def parse_json(text: str) -> Any:
    """يستخرج JSON من نص قد يحوي أسوار ```` ```json ````. #PRESERVED_LOGIC _parse_json."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        match = re.search(r"[\[{].*[\]}]", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
    return None


def default_providers() -> list[_Provider]:
    """مزودات تلتفّ على ``engines.ai_engine`` بالترتيب الحيّ (OpenRouter→Gemini→Cohere)."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from engines.ai_engine import (  # type: ignore
            _call_cohere,
            _call_gemini,
            _call_openrouter,
        )
    except Exception as exc:  # pragma: no cover
        raise AIServiceError(
            "تعذّر تحميل مزودات AI من engines.ai_engine", error=str(exc),
        ) from exc
    return [
        _Provider("OpenRouter", lambda p, s: _call_openrouter(p, s)),
        _Provider("Gemini", lambda p, s: _call_gemini(p, s)),
        _Provider("Cohere", lambda p, s: _call_cohere(p, s)),
    ]


class AIService:
    """خدمة AI منسّقة: تدوير مزودات + إعادة محاولة + كاش + دفعات."""

    def __init__(
        self,
        providers: Optional[list[_Provider]] = None,
        cache: Optional[AICache] = None,
        settings: Optional[Settings] = None,
        *,
        max_retries: int = 3,
        timeout_s: int = 30,
        batch_size: int = 8,
        max_items: int = 150,
    ) -> None:
        self._providers = providers
        self._cache: AICache = cache if cache is not None else InMemoryCache()
        self._settings = settings
        self._max_retries = max_retries
        self._timeout_s = timeout_s
        self._batch_size = batch_size
        self._max_items = max_items

    def _chain(self) -> list[_Provider]:
        if self._providers is None:
            self._providers = default_providers()
        return self._providers

    def call(
        self, prompt: str, system: str = "", *, use_cache: bool = True,
    ) -> AIResult:
        """ينادي السلسلة مع إعادة محاولة وكاش. أول استجابة غير فارغة تفوز."""
        key = _hash_key(system, prompt)
        if use_cache:
            cached = self._cache.get(key)
            if cached is not None:
                return AIResult(True, cached, "cache")
        for provider in self._chain():
            for _ in range(self._max_retries):
                try:
                    response = provider(prompt, system)
                except Exception:
                    response = None
                if response:
                    if use_cache:
                        self._cache.set(key, response)
                    return AIResult(True, response, provider.name)
        return AIResult(False, "", "none")

    def call_json(self, prompt: str, system: str = "") -> Any:
        """ينادي ويحلّل JSON؛ ``None`` عند الفشل (لا تعطّل)."""
        result = self.call(prompt, system)
        return parse_json(result.response) if result.success else None

    def batch(
        self,
        items: Iterable[Any],
        build_prompt: Callable[[list[Any]], str],
        parse: Optional[Callable[[str], Any]] = None,
        system: str = "",
    ) -> list[Any]:
        """يعالج العناصر دفعات (≤8، حتى 150). نتيجة لكل دفعة؛ None عند فشل AI."""
        capped = list(items)[: self._max_items]
        results: list[Any] = []
        for start in range(0, len(capped), self._batch_size):
            chunk = capped[start:start + self._batch_size]
            result = self.call(build_prompt(chunk), system)
            if not result.success:
                results.append(None)
            else:
                results.append(parse(result.response) if parse else result.response)
        return results

    @property
    def any_configured(self) -> bool:
        """هل أيّ مزوّد مُهيّأ؟ (يحمّل الإعدادات كسولاً)."""
        settings = self._settings or Settings.load()
        return settings.any_ai_configured
