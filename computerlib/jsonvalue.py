"""Narrowing helper for parsed JSON, shared so every module normalises untyped objects the same way."""

from __future__ import annotations

from typing import cast


def as_dict(value: object) -> dict[str, object]:
    """`value` as a str-keyed dict, or an empty dict when it is not an object."""
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast("dict[object, object]", value).items()}


def is_object(value: object) -> bool:
    """Whether `value` is a JSON object; a plain bool so type checkers do not narrow it to `dict[Unknown, Unknown]`."""
    return isinstance(value, dict)
