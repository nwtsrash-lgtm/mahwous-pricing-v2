"""
Fast JSON: prefer orjson when installed; fall back to stdlib json.
"""
from __future__ import annotations

from typing import Any, BinaryIO, TextIO

try:
    import orjson

    def loads(s: str | bytes) -> Any:
        if isinstance(s, str):
            s = s.encode("utf-8")
        return orjson.loads(s)

    def _default_fn(default: Any):
        return default if callable(default) else str

    def dumps(obj: Any, *, ensure_ascii: bool = True, indent: int | None = None, default=str, **_: Any) -> str:
        opts = 0
        if indent is not None:
            opts |= orjson.OPT_INDENT_2
        raw = orjson.dumps(obj, option=opts, default=_default_fn(default))
        return raw.decode("utf-8")

    def load(fp: TextIO | BinaryIO) -> Any:
        b = fp.read()
        if isinstance(b, str):
            b = b.encode("utf-8")
        return orjson.loads(b)

    def dump(obj: Any, fp: TextIO, *, ensure_ascii: bool = True, indent: int | None = None, default=str, **_: Any) -> None:
        opts = 0
        if indent is not None:
            opts |= orjson.OPT_INDENT_2
        raw = orjson.dumps(obj, option=opts, default=_default_fn(default))
        if "b" in getattr(fp, "mode", "w"):
            fp.write(raw)
        else:
            fp.write(raw.decode("utf-8"))

except ImportError:
    import json as _json

    def loads(s: str | bytes) -> Any:
        if isinstance(s, bytes):
            s = s.decode("utf-8")
        return _json.loads(s)

    def dumps(obj: Any, **kwargs: Any) -> str:
        if "default" not in kwargs:
            kwargs["default"] = str
        return _json.dumps(obj, **kwargs)

    def load(fp: TextIO | BinaryIO) -> Any:
        return _json.load(fp)

    def dump(obj: Any, fp: TextIO, **kwargs: Any) -> None:
        if "default" not in kwargs:
            kwargs["default"] = str
        _json.dump(obj, fp, **kwargs)
