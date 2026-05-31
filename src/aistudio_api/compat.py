"""Helpers for backward-compatible root-level scripts."""

from __future__ import annotations

import sys
from pathlib import Path


def bootstrap_src_path():
    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

