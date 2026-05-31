"""Request validation helpers shared by API normalizers."""

from __future__ import annotations

import math
from typing import Any


def validate_number_range(
    name: str,
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    numeric = float(value)
    if minimum is not None and numeric < minimum:
        raise ValueError(f"{name} must be greater than or equal to {minimum:g}")
    if maximum is not None and numeric > maximum:
        raise ValueError(f"{name} must be less than or equal to {maximum:g}")