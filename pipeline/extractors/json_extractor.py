"""
ATS JSON Extractor — maps semi-structured ATS blobs to our internal format.

The ATS blob uses non-standard field names (e.g. "applicant_name" instead of "full_name").
This extractor uses a flexible path-based mapping to handle multiple ATS schemas.

Design decision: we try a priority list of candidate paths for each canonical field
and use the first non-null value. Unknown keys are preserved under `_extras` for
potential future mapping without data loss.
"""
from __future__ import annotations
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Maps our canonical field → ordered list of ATS field paths to try
# Paths support simple dot-notation (a.b.c) and list index [0]
_FIELD_PATHS: dict[str, list[str]] = {
    "full_name": [
        "applicant_name", "candidate.name", "person.full_name",
        "name", "contact.name", "firstName+lastName",
    ],
    "email": [
        "applicant_email", "contact.email", "candidate.email_address",
        "email", "emails[0]", "contact_info.email",
    ],
    "phone": [
        "applicant_phone", "contact.phone", "candidate.phone_number",
        "phone", "phones[0]", "contact_info.phone",
    ],
    "company": [
        "current_employer", "work_history[0].company", "employment[0].organization",
        "current_company", "employer",
    ],
    "title": [
        "current_title", "work_history[0].title", "employment[0].position",
        "job_title", "position", "current_role",
    ],
    "location": [
        "candidate.location", "contact.city", "address.city",
        "location", "city",
    ],
    "linkedin": [
        "social_profiles.linkedin", "links.linkedin", "linkedin_url", "linkedin",
    ],
    "github": [
        "social_profiles.github", "links.github", "github_url", "github",
    ],
    "years_experience": [
        "experience_years", "years_of_experience", "yoe",
    ],
    "skills": [
        "skills", "skill_tags", "technologies", "tech_stack",
    ],
    "experience": [
        "work_history", "employment_history", "experience", "jobs",
    ],
    "education": [
        "education_history", "education", "academic_background",
    ],
    "headline": [
        "headline", "professional_summary", "bio", "summary",
    ],
}


def _get_nested(data: dict, path: str) -> Any:
    """
    Safely extract a value from a nested dict using dot-notation path.
    Supports: 'a.b.c', 'a[0].b', 'a[0]'
    Returns None if any segment is missing.
    """
    if not path or not data:
        return None

    # Handle special compound: firstName+lastName → join
    if "+" in path:
        parts = path.split("+")
        values = [_get_nested(data, p) for p in parts]
        values = [v for v in values if v]
        return " ".join(str(v) for v in values) if values else None

    segments = []
    import re
    for seg in re.split(r"\.", path):
        m = re.match(r"^(\w+)\[(\d+)\]$", seg)
        if m:
            segments.append(m.group(1))
            segments.append(int(m.group(2)))
        else:
            segments.append(seg)

    current: Any = data
    for seg in segments:
        try:
            if isinstance(current, dict):
                current = current.get(seg)
            elif isinstance(current, list) and isinstance(seg, int):
                current = current[seg] if seg < len(current) else None
            else:
                return None
            if current is None:
                return None
        except Exception:
            return None
    return current


def _extract_experience(raw: Any) -> list[dict]:
    """Parse experience list from ATS format."""
    if not isinstance(raw, list):
        return []
    results = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        exp = {}
        for our_key, ats_keys in [
            ("company", ["company", "organization", "employer"]),
            ("title", ["title", "position", "role", "job_title"]),
            ("start", ["start_date", "start", "from", "begin_date"]),
            ("end", ["end_date", "end", "to", "end_date"]),
            ("summary", ["description", "summary", "responsibilities", "duties"]),
        ]:
            for k in ats_keys:
                val = entry.get(k)
                if val:
                    exp[our_key] = str(val)
                    break
        if exp:
            results.append(exp)
    return results


def _extract_education(raw: Any) -> list[dict]:
    """Parse education list from ATS format."""
    if not isinstance(raw, list):
        return []
    results = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        edu = {}
        for our_key, ats_keys in [
            ("institution", ["school", "institution", "university", "college"]),
            ("degree", ["degree", "qualification", "credential"]),
            ("field", ["field_of_study", "major", "subject", "field"]),
            ("end_year", ["graduation_year", "end_year", "year", "grad_year"]),
        ]:
            for k in ats_keys:
                val = entry.get(k)
                if val is not None:
                    edu[our_key] = val
                    break
        if edu:
            results.append(edu)
    return results


def extract_json(file_path: str) -> list[dict]:
    """
    Parse an ATS JSON blob and return a list of raw candidate dicts.
    Handles single-candidate and multi-candidate (list) JSON.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning("JSON file not found: %s", file_path)
        return []
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON in %s: %s", file_path, e)
        return []
    except Exception as e:
        logger.error("Failed to read JSON file %s: %s", file_path, e)
        return []

    # Normalize: always work with a list
    if isinstance(data, dict):
        # Could be a single candidate OR a wrapper {"candidates": [...]}
        candidates_raw = data.get("candidates") or data.get("applicants") or data.get("data")
        if isinstance(candidates_raw, list):
            records = candidates_raw
        else:
            records = [data]
    elif isinstance(data, list):
        records = data
    else:
        logger.warning("Unexpected JSON top-level structure in %s", file_path)
        return []

    results = []
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            logger.warning("Skipping non-dict record at index %d in %s", i, file_path)
            continue
        try:
            candidate: dict = {"_source": "json_ats", "_source_file": file_path}
            for canon_key, paths in _FIELD_PATHS.items():
                for path in paths:
                    val = _get_nested(record, path)
                    if val is not None and val != "":
                        if canon_key == "experience":
                            candidate[canon_key] = _extract_experience(val)
                        elif canon_key == "education":
                            candidate[canon_key] = _extract_education(val)
                        elif canon_key == "skills" and isinstance(val, list):
                            candidate[canon_key] = [
                                str(s) for s in val if s
                            ]
                        else:
                            candidate[canon_key] = val
                        break  # Use first match

            results.append(candidate)
        except Exception as e:
            logger.warning("Skipping malformed ATS record at index %d: %s", i, e)

    return results
