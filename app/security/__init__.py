"""Deterministic security layer: permission classification and policy decisions.

Nothing in this package consults the language model, and no model output can alter
its decisions.
"""

from app.security.permissions import (
    DEFAULT_PERMISSION,
    PERMISSION_DESCRIPTIONS,
    TOOL_PERMISSIONS,
    classify,
    describe,
    is_registered,
    risk_reason,
    tools_at_level,
)

__all__ = [
    "DEFAULT_PERMISSION",
    "PERMISSION_DESCRIPTIONS",
    "TOOL_PERMISSIONS",
    "classify",
    "describe",
    "is_registered",
    "risk_reason",
    "tools_at_level",
]
