"""Tolerant JSON extraction from model output."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


class ParseError(ValueError):
    pass


def _balanced_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def parse_json_object(text: str) -> dict[str, Any]:
    if not text or not text.strip():
        raise ParseError("empty response")
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE_RE.findall(text)]
    obj = _balanced_object(text)
    if obj:
        candidates.append(obj)
    for cand in candidates:
        for variant in (cand, _TRAILING_COMMA_RE.sub(r"\1", cand)):
            try:
                value = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ParseError(f"no JSON object found in: {text[:300]!r}")
