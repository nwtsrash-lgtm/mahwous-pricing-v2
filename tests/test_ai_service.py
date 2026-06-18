"""tests/test_ai_service.py — اختبارات تنسيق AI (P3) بمزودات وهمية."""
from services.ai_service import AIResult, AIService, _Provider, parse_json


def _provider(name: str, value):
    """مزوّد ثابت يُعيد ``value`` (أو None)، مع عدّاد نداءات."""
    calls = {"n": 0}

    def fn(prompt: str, system: str = ""):
        calls["n"] += 1
        return value

    return _Provider(name, fn), calls


def test_fallback_chain_uses_first_success() -> None:
    p1, c1 = _provider("P1", None)
    p2, c2 = _provider("P2", "answer")
    p3, c3 = _provider("P3", "unused")
    svc = AIService(providers=[p1, p2, p3])
    result = svc.call("hi")
    assert result == AIResult(True, "answer", "P2")
    assert c3["n"] == 0  # لم نصل للمزوّد الثالث


def test_cache_prevents_second_call() -> None:
    p1, c1 = _provider("P1", "cached-value")
    svc = AIService(providers=[p1])
    first = svc.call("same prompt")
    second = svc.call("same prompt")
    assert first.source == "P1" and second.source == "cache"
    assert c1["n"] == 1  # نُودِي مرة واحدة فقط


def test_all_providers_fail() -> None:
    p1, _ = _provider("P1", None)
    p2, _ = _provider("P2", "")
    result = AIService(providers=[p1, p2]).call("x")
    assert result.success is False and result.source == "none"


def test_retries_within_provider() -> None:
    state = {"n": 0}

    def flaky(prompt, system=""):
        state["n"] += 1
        return "ok" if state["n"] >= 3 else None

    svc = AIService(providers=[_Provider("Flaky", flaky)], max_retries=3)
    assert svc.call("x").success is True and state["n"] == 3


def test_parse_json_strips_markdown_fences() -> None:
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json("noise [1,2,3] tail") == [1, 2, 3]
    assert parse_json("not json at all") is None


def test_batch_chunks_and_caps() -> None:
    p, calls = _provider("P", "r")
    svc = AIService(providers=[p], batch_size=8, max_items=150)
    results = svc.batch(range(20), build_prompt=lambda chunk: str(chunk))
    assert len(results) == 3 and calls["n"] == 3   # 8+8+4 ⇒ 3 دفعات

    p2, calls2 = _provider("P", "r")
    svc2 = AIService(providers=[p2], batch_size=8, max_items=5)
    assert len(svc2.batch(range(20), build_prompt=lambda c: str(c))) == 1
