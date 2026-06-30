"""
Normalizer — converts raw extracted values into canonical formats.

All functions are pure / deterministic:
  - phones    → E.164 format  (e.g. +14155552671)
  - dates     → YYYY-MM       (e.g. 2021-03)
  - countries → ISO-3166 alpha-2 (e.g. "US")
  - skills    → canonical name via fuzzy match against a reference list
"""
from __future__ import annotations
import re
import logging
from typing import Optional
from datetime import datetime

import phonenumbers
from dateutil import parser as dateutil_parser
from rapidfuzz import process, fuzz

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Skill canonicalization — reference dictionary
# Maps aliases / abbreviations → canonical name
# ─────────────────────────────────────────────────────────────
SKILL_ALIASES: dict[str, str] = {
    # Python ecosystem
    "python": "Python",
    "python3": "Python",
    "py": "Python",
    # JavaScript / TypeScript
    "javascript": "JavaScript",
    "js": "JavaScript",
    "typescript": "TypeScript",
    "ts": "TypeScript",
    # Web frameworks
    "react": "React",
    "reactjs": "React",
    "react.js": "React",
    "vue": "Vue.js",
    "vuejs": "Vue.js",
    "vue.js": "Vue.js",
    "angular": "Angular",
    "angularjs": "Angular",
    "node": "Node.js",
    "nodejs": "Node.js",
    "node.js": "Node.js",
    "express": "Express.js",
    "expressjs": "Express.js",
    "nextjs": "Next.js",
    "next.js": "Next.js",
    # Data / ML
    "machine learning": "Machine Learning",
    "ml": "Machine Learning",
    "deep learning": "Deep Learning",
    "dl": "Deep Learning",
    "tensorflow": "TensorFlow",
    "tf": "TensorFlow",
    "pytorch": "PyTorch",
    "torch": "PyTorch",
    "scikit-learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "pandas": "pandas",
    "numpy": "NumPy",
    "np": "NumPy",
    # Databases
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "mongo": "MongoDB",
    "redis": "Redis",
    "elasticsearch": "Elasticsearch",
    # Cloud / DevOps
    "aws": "AWS",
    "amazon web services": "AWS",
    "gcp": "GCP",
    "google cloud": "GCP",
    "azure": "Azure",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "terraform": "Terraform",
    "ci/cd": "CI/CD",
    "github actions": "GitHub Actions",
    # Languages
    "java": "Java",
    "c++": "C++",
    "cpp": "C++",
    "c#": "C#",
    "csharp": "C#",
    "go": "Go",
    "golang": "Go",
    "rust": "Rust",
    "ruby": "Ruby",
    "rails": "Ruby on Rails",
    "ruby on rails": "Ruby on Rails",
    "php": "PHP",
    "swift": "Swift",
    "kotlin": "Kotlin",
    # Design
    "figma": "Figma",
    "sketch": "Sketch",
    "adobe xd": "Adobe XD",
    "photoshop": "Adobe Photoshop",
    # General
    "git": "Git",
    "github": "GitHub",
    "gitlab": "GitLab",
    "rest api": "REST API",
    "restful": "REST API",
    "graphql": "GraphQL",
    "sql": "SQL",
    "html": "HTML",
    "css": "CSS",
    "linux": "Linux",
    "bash": "Bash",
    "shell": "Bash",
    "agile": "Agile",
    "scrum": "Scrum",
}

_CANONICAL_SKILLS = list(set(SKILL_ALIASES.values()))


# ─────────────────────────────────────────────────────────────
# Phone normalization
# ─────────────────────────────────────────────────────────────
def normalize_phone(raw: str, default_region: str = "US") -> Optional[str]:
    """
    Parse a raw phone string and return E.164 format.
    Returns None if the number cannot be parsed/validated.
    Never raises.
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = phonenumbers.parse(raw.strip(), default_region)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        logger.debug("Phone number is syntactically parsed but not valid: %s", raw)
        return None
    except phonenumbers.NumberParseException:
        logger.debug("Could not parse phone number: %s", raw)
        return None


# ─────────────────────────────────────────────────────────────
# Date normalization
# ─────────────────────────────────────────────────────────────
def normalize_date(raw: str) -> Optional[str]:
    """
    Parse a raw date string and return YYYY-MM format.
    Handles: "2020-03", "March 2020", "03/2020", "2020", "Jan 2021", etc.
    Returns None on failure. Never raises.
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    # Already in YYYY-MM
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return raw
    # Year only
    if re.fullmatch(r"\d{4}", raw):
        return f"{raw}-01"
    # Try dateutil
    try:
        dt = dateutil_parser.parse(raw, default=datetime(2000, 1, 1))
        return dt.strftime("%Y-%m")
    except (ValueError, OverflowError):
        logger.debug("Could not parse date: %s", raw)
        return None


# ─────────────────────────────────────────────────────────────
# Country normalization
# ─────────────────────────────────────────────────────────────
_COUNTRY_MAP: dict[str, str] = {
    "united states": "US", "usa": "US", "u.s.a.": "US", "us": "US",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "canada": "CA", "ca": "CA",
    "india": "IN", "in": "IN",
    "germany": "DE", "de": "DE",
    "france": "FR", "fr": "FR",
    "australia": "AU", "au": "AU",
    "singapore": "SG", "sg": "SG",
    "netherlands": "NL", "nl": "NL",
    "sweden": "SE", "se": "SE",
    "israel": "IL", "il": "IL",
    "japan": "JP", "jp": "JP",
    "china": "CN", "cn": "CN",
    "brazil": "BR", "br": "BR",
    "ireland": "IE", "ie": "IE",
    "spain": "ES", "es": "ES",
    "italy": "IT", "it": "IT",
    "poland": "PL", "pl": "PL",
    "ukraine": "UA", "ua": "UA",
    "mexico": "MX", "mx": "MX",
    "new zealand": "NZ", "nz": "NZ",
    "switzerland": "CH", "ch": "CH",
    "norway": "NO", "no": "NO",
    "denmark": "DK", "dk": "DK",
    "finland": "FI", "fi": "FI",
    "belgium": "BE", "be": "BE",
    "portugal": "PT", "pt": "PT",
    "south korea": "KR", "korea": "KR", "kr": "KR",
    "remote": "REMOTE",
}


def normalize_country(raw: str) -> Optional[str]:
    """
    Return ISO-3166 alpha-2 country code from a raw country/location string.
    Returns None if unrecognized.
    """
    if not raw or not raw.strip():
        return None
    key = raw.strip().lower()
    return _COUNTRY_MAP.get(key)


# ─────────────────────────────────────────────────────────────
# Skill canonicalization
# ─────────────────────────────────────────────────────────────
def canonicalize_skill(raw: str, threshold: int = 80) -> str:
    """
    Map a raw skill name to its canonical form.
    1. Exact match in SKILL_ALIASES (case-insensitive)
    2. Fuzzy match against canonical skill list (rapidfuzz)
    3. Title-case fallback
    """
    if not raw or not raw.strip():
        return raw
    key = raw.strip().lower()
    # Exact alias match
    if key in SKILL_ALIASES:
        return SKILL_ALIASES[key]
    # Fuzzy match
    result = process.extractOne(raw, _CANONICAL_SKILLS, scorer=fuzz.WRatio)
    if result and result[1] >= threshold:
        return result[0]
    # Fallback: title case
    return raw.strip().title()


def normalize_email(raw: str) -> Optional[str]:
    """Lowercase + strip whitespace. Returns None if empty."""
    if not raw or not raw.strip():
        return None
    return raw.strip().lower()
