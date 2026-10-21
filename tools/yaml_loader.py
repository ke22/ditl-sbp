"""Safe YAML parsing with LibYAML acceleration when PyYAML provides it.

There is no document cache: each caller still reads its current file, and
each parse returns independent objects that its caller may safely edit.
"""
from __future__ import annotations

import yaml

_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load(stream):
    """Preserve safe_load's data types and safe constructors, without Python scanning."""
    return yaml.load(stream, Loader=_SAFE_LOADER)
