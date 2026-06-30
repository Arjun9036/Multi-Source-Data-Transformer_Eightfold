"""
Projector — applies a runtime config to a CanonicalProfile to produce the final output dict.

The config can:
  - Select a subset of fields to include
  - Rename / remap a field (via the "from" key with path syntax)
  - Set per-field normalization (E164, canonical)
  - Toggle provenance and confidence on or off
  - Control on_missing behavior: "null", "omit", or "error"

Design: Clean separation between internal canonical record (CanonicalProfile)
and the projection layer (what the caller sees). The canonical record is never mutated.
"""
from __future__ import annotations
import logging
import re
from typing import Any, Optional

from pipeline.normalizer import normalize_phone, canonicalize_skill

logger = logging.getLogger(__name__)


def _get_path(data: dict, path: str) -> Any:
    """
    Resolve a dot-notation path with array indexing and mapping from a dict.

    Supported syntax:
      - "emails[0]"       → first email
      - "skills[].name"   → list of all skill names
      - "location.city"   → nested field
      - "full_name"       → top-level field
    """
    if not path or not data:
        return None

    # Handle mapping syntax: "skills[].name" → map over list
    map_match = re.match(r"^(\w+)\[\]\.([\w.]+)$", path)
    if map_match:
        list_key = map_match.group(1)
        sub_path = map_match.group(2)
        items = data.get(list_key, [])
        if isinstance(items, list):
            result = []
            for item in items:
                if isinstance(item, dict):
                    val = _get_path(item, sub_path)
                    if val is not None:
                        result.append(val)
            return result or None
        return None

    # Handle indexed access: "emails[0]"
    idx_match = re.match(r"^(\w+)\[(\d+)\]$", path)
    if idx_match:
        key = idx_match.group(1)
        idx = int(idx_match.group(2))
        items = data.get(key)
        if isinstance(items, list) and idx < len(items):
            return items[idx]
        return None

    # Handle nested dot-notation: "location.city"
    if "." in path:
        parts = path.split(".", 1)
        nested = data.get(parts[0])
        if isinstance(nested, dict):
            return _get_path(nested, parts[1])
        return None

    # Simple key
    return data.get(path)


def _apply_normalization(value: Any, normalize: Optional[str]) -> Any:
    """Apply a named normalization to a value."""
    if normalize is None or value is None:
        return value
    norm = normalize.upper()
    if norm == "E164":
        if isinstance(value, list):
            return [normalize_phone(str(v)) for v in value if normalize_phone(str(v))]
        return normalize_phone(str(value))
    elif norm == "CANONICAL":
        if isinstance(value, list):
            return [canonicalize_skill(str(v)) for v in value]
        return canonicalize_skill(str(value))
    elif norm == "LOWERCASE":
        if isinstance(value, list):
            return [str(v).lower() for v in value]
        return str(value).lower()
    return value


def _validate_type(value: Any, expected_type: Optional[str], path: str) -> Any:
    """
    Coerce / validate value to expected type.
    Supported types: "string", "string[]", "number", "boolean"
    """
    if value is None or expected_type is None:
        return value
    t = expected_type.lower()
    try:
        if t == "string":
            if isinstance(value, list):
                return str(value[0]) if value else None
            return str(value)
        elif t == "string[]":
            if isinstance(value, list):
                return [str(v) for v in value]
            return [str(value)]
        elif t == "number":
            return float(value)
        elif t == "boolean":
            return bool(value)
    except (ValueError, TypeError, IndexError) as e:
        logger.debug("Type coercion failed for %s (%s → %s): %s", path, type(value).__name__, t, e)
        return None
    return value


def project(profile_dict: dict, config: dict) -> dict:
    """
    Apply a runtime config to a canonical profile dict and return the projected output.

    Args:
        profile_dict: The full canonical profile as a plain dict (from profile.model_dump()).
        config: The runtime config dict.

    Returns:
        Projected output dict.

    Raises:
        ValueError: If on_missing == "error" and a required field is missing.
    """
    fields = config.get("fields")
    include_confidence = config.get("include_confidence", True)
    on_missing = config.get("on_missing", "null")  # "null" | "omit" | "error"

    # If no fields config → return full profile (with or without confidence)
    if not fields:
        result = dict(profile_dict)
        if not include_confidence:
            result.pop("provenance", None)
            result.pop("overall_confidence", None)
        return result

    output: dict = {}

    for field_spec in fields:
        out_path = field_spec.get("path")
        from_path = field_spec.get("from", out_path)  # default: same as path
        expected_type = field_spec.get("type")
        normalize = field_spec.get("normalize")
        required = field_spec.get("required", False)

        if not out_path:
            logger.warning("Field spec missing 'path': %s", field_spec)
            continue

        # Resolve value from canonical profile
        value = _get_path(profile_dict, from_path)

        # Apply normalization
        if normalize:
            value = _apply_normalization(value, normalize)

        # Apply type coercion
        if expected_type:
            value = _validate_type(value, expected_type, from_path)

        # Handle missing
        if value is None:
            if required and on_missing == "error":
                raise ValueError(
                    f"Required field '{out_path}' (from '{from_path}') is missing in canonical profile."
                )
            elif on_missing == "omit":
                continue  # Don't include this field at all
            else:  # "null"
                output[out_path] = None
        else:
            output[out_path] = value

    # Confidence toggle
    if include_confidence:
        output["_confidence"] = profile_dict.get("overall_confidence")
        output["_provenance"] = profile_dict.get("provenance", [])

    return output
