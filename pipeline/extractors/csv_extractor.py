"""
CSV Extractor — parses a Recruiter CSV export.

Expected columns (flexible — we map multiple aliases):
  name / full_name, email, phone, current_company / company, title / job_title,
  location / city, linkedin, github

Any source may be missing or malformed; we never crash.
Returns a list of raw candidate dicts (one per CSV row).
"""
from __future__ import annotations
import csv
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Column name aliases → canonical internal key
_COLUMN_ALIASES: dict[str, str] = {
    "name": "full_name",
    "full_name": "full_name",
    "full name": "full_name",
    "candidate_name": "full_name",
    "email": "email",
    "email_address": "email",
    "e-mail": "email",
    "phone": "phone",
    "phone_number": "phone",
    "mobile": "phone",
    "tel": "phone",
    "current_company": "company",
    "company": "company",
    "employer": "company",
    "organization": "company",
    "title": "title",
    "job_title": "title",
    "position": "title",
    "role": "title",
    "location": "location",
    "city": "location",
    "address": "location",
    "linkedin": "linkedin",
    "linkedin_url": "linkedin",
    "linkedin_profile": "linkedin",
    "github": "github",
    "github_url": "github",
    "github_profile": "github",
    "skills": "skills",
    "skill_set": "skills",
    "tags": "skills",
    "years_experience": "years_experience",
    "experience_years": "years_experience",
    "yoe": "years_experience",
}


def _normalize_header(header: str) -> str:
    return header.strip().lower().replace(" ", "_").replace("-", "_")


def extract_csv(file_path: str) -> list[dict]:
    """
    Parse a CSV file and return a list of raw candidate dicts.
    Gracefully skips malformed rows or files.
    """
    results = []
    try:
        with open(file_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                logger.warning("CSV file has no headers: %s", file_path)
                return []

            # Build header mapping
            header_map: dict[str, str] = {}
            for raw_col in reader.fieldnames:
                norm = _normalize_header(raw_col)
                canonical = _COLUMN_ALIASES.get(norm)
                if canonical:
                    header_map[raw_col] = canonical

            for i, row in enumerate(reader):
                try:
                    candidate: dict = {"_source": "csv", "_source_file": file_path}
                    for raw_col, canon_key in header_map.items():
                        raw_val = row.get(raw_col)
                        val = (raw_val or "").strip()
                        if val:
                            # If we already have this key, keep the first non-empty
                            if canon_key not in candidate:
                                candidate[canon_key] = val
                    results.append(candidate)
                except Exception as e:
                    logger.warning("Skipping malformed CSV row %d: %s", i, e)

    except FileNotFoundError:
        logger.warning("CSV file not found: %s", file_path)
    except Exception as e:
        logger.error("Failed to read CSV file %s: %s", file_path, e)

    return results
