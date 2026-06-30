"""
Validator — validates the projected output dict against the config schema.

Checks:
  1. Required fields are present and non-null (per field spec + on_missing policy)
  2. Field types match declared type in config
  3. E.164 phone format (if phones present)
  4. YYYY-MM date format in experience entries (if present)

Returns a ValidationResult with is_valid flag and list of violations.
Never crashes — all checks return violations, not exceptions.
"""
from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_YYYY_MM_RE = re.compile(r"^\d{4}-\d{2}$")


@dataclass
class ValidationResult:
    is_valid: bool
    violations: list[str] = field(default_factory=list)

    def add_violation(self, msg: str) -> None:
        self.violations.append(msg)
        self.is_valid = False


def _check_type(value, expected_type: str, field_name: str, result: ValidationResult) -> None:
    if value is None:
        return  # Null is handled by required check
    t = expected_type.lower()
    if t == "string" and not isinstance(value, str):
        result.add_violation(f"Field '{field_name}': expected string, got {type(value).__name__}")
    elif t == "string[]":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            result.add_violation(f"Field '{field_name}': expected string[], got {type(value).__name__}")
    elif t == "number" and not isinstance(value, (int, float)):
        result.add_violation(f"Field '{field_name}': expected number, got {type(value).__name__}")
    elif t == "boolean" and not isinstance(value, bool):
        result.add_violation(f"Field '{field_name}': expected boolean, got {type(value).__name__}")


def validate_output(output: dict, config: dict) -> ValidationResult:
    """
    Validate a projected output dict against the config schema.

    Args:
        output: The projected output dict from the projector.
        config: The runtime config dict.

    Returns:
        ValidationResult with is_valid and list of violations.
    """
    result = ValidationResult(is_valid=True)
    fields = config.get("fields", [])
    on_missing = config.get("on_missing", "null")

    for field_spec in fields:
        path = field_spec.get("path", "")
        expected_type = field_spec.get("type")
        required = field_spec.get("required", False)
        normalize = field_spec.get("normalize", "")

        value = output.get(path)

        # Required check
        if required and (value is None):
            if on_missing != "omit":  # omit means it won't be there, that's OK
                result.add_violation(f"Required field '{path}' is null in output")

        # Type check
        if expected_type and value is not None:
            _check_type(value, expected_type, path, result)

        # E.164 phone check
        if normalize and normalize.upper() == "E164" and value is not None:
            phones = value if isinstance(value, list) else [value]
            for p in phones:
                if p and not _E164_RE.match(str(p)):
                    result.add_violation(
                        f"Field '{path}': value '{p}' is not valid E.164 format"
                    )

    # Check experience dates if present in output
    experience = output.get("experience", [])
    if isinstance(experience, list):
        for i, exp in enumerate(experience):
            if not isinstance(exp, dict):
                continue
            for date_field in ("start", "end"):
                date_val = exp.get(date_field)
                if date_val and not _YYYY_MM_RE.match(str(date_val)):
                    result.add_violation(
                        f"experience[{i}].{date_field}: '{date_val}' is not YYYY-MM format"
                    )

    return result
