"""Pre-flight validation of tool arguments against a discovered JSON Schema.

The MCP server is the authority on whether arguments are valid -- it validates with
pydantic and rejects anything malformed. This module is a *pre-flight* check that
runs before dispatch, for three reasons:

1. **Better errors, sooner.** A missing required field is caught without a round
   trip, and the message names the tool and field so the agent can retry usefully.
2. **Nothing invalid reaches an approval.** Persisting an approval request for a
   call that cannot succeed wastes a human's attention.
3. **Unknown arguments are visible.** A model inventing a ``force_delete`` parameter
   is worth surfacing, even though the server would ignore it.

Deliberately a small, dependency-free subset of JSON Schema -- exactly what the MCP
SDK generates from Python type hints. It is a usability guard, never a security
control: rejecting a call here is a convenience, and the server re-validates
regardless.
"""

from __future__ import annotations

from typing import Any

#: JSON Schema type name -> acceptable Python types.
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
    "null": (type(None),),
}


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """Check *arguments* against *schema*, returning human-readable problems.

    An empty list means "no problems found". Unrecognised schema constructs are
    skipped rather than reported, so a richer schema than this subset understands
    can never produce a false rejection.
    """
    problems: list[str] = []
    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = schema.get("required") or []

    for name in required:
        if name not in arguments or arguments[name] is None:
            problems.append(f"missing required argument {name!r}")

    known = set(properties)
    for name in arguments:
        if known and name not in known:
            problems.append(f"unknown argument {name!r}")

    for name, value in arguments.items():
        definition = properties.get(name)
        if definition is None or value is None:
            continue
        problems.extend(_check_value(name, value, definition, schema))

    return problems


def _check_value(
    name: str, value: Any, definition: dict[str, Any], root: dict[str, Any]
) -> list[str]:
    """Validate one value against its property definition."""
    definition = _resolve_ref(definition, root)

    if "anyOf" in definition or "oneOf" in definition:
        branches = definition.get("anyOf") or definition.get("oneOf") or []
        # Valid if it satisfies any branch; report nothing when at least one fits.
        if any(
            not _check_value(name, value, _resolve_ref(branch, root), root) for branch in branches
        ):
            return []
        rendered = ", ".join(
            str(_resolve_ref(branch, root).get("type", "?")) for branch in branches
        )
        return [f"argument {name!r} does not match any accepted type ({rendered})"]

    problems: list[str] = []

    if (enum := definition.get("enum")) is not None and value not in enum:
        allowed = ", ".join(repr(option) for option in enum)
        return [f"argument {name!r} must be one of: {allowed}"]

    expected = definition.get("type")
    if isinstance(expected, str) and (accepted := _TYPE_MAP.get(expected)):
        # bool is a subclass of int in Python; a boolean is not an acceptable integer.
        if isinstance(value, bool) and expected in ("integer", "number"):
            return [f"argument {name!r} must be a {expected}, got a boolean"]
        if not isinstance(value, accepted):
            return [f"argument {name!r} must be a {expected}, got {type(value).__name__}"]

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if (minimum := definition.get("minimum")) is not None and value < minimum:
            problems.append(f"argument {name!r} must be >= {minimum}")
        if (maximum := definition.get("maximum")) is not None and value > maximum:
            problems.append(f"argument {name!r} must be <= {maximum}")

    if isinstance(value, str):
        if (min_length := definition.get("minLength")) is not None and len(value) < min_length:
            problems.append(f"argument {name!r} must be at least {min_length} characters")
        if (max_length := definition.get("maxLength")) is not None and len(value) > max_length:
            problems.append(f"argument {name!r} must be at most {max_length} characters")

    if isinstance(value, (list, tuple)):
        if (min_items := definition.get("minItems")) is not None and len(value) < min_items:
            problems.append(f"argument {name!r} must contain at least {min_items} item(s)")
        if (max_items := definition.get("maxItems")) is not None and len(value) > max_items:
            problems.append(f"argument {name!r} must contain at most {max_items} item(s)")

    return problems


def _resolve_ref(definition: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``$ref`` into ``$defs``.

    Pydantic emits ``$ref`` for enums and nested models. Only local references are
    followed; anything else is returned unchanged and therefore not validated.
    """
    ref = definition.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return definition
    resolved = (root.get("$defs") or {}).get(ref.removeprefix("#/$defs/"))
    return resolved if isinstance(resolved, dict) else definition
