"""Narrowing of untrusted parsed JSON: the shim reads bodies it does not control, so every access goes through these."""

from __future__ import annotations

import json
from typing import cast


def as_object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def parse_object(body: bytes) -> dict[str, object]:
    try:
        return as_object(json.loads(body))
    except ValueError:
        return {}
