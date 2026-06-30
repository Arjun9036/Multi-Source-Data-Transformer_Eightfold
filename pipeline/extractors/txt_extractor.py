"""
TXT Extractor — parses free-text recruiter notes using targeted regex & token scanning.

This is the recommended unstructured source per the spec.
Approach: rule-based regex only (deterministic, explainable, no LLM).

Extracts:
  - full_name     → "Candidate:" label or first capitalized name-looking line
  - email         → standard email regex
  - phone         → phone pattern regex
  - linkedin      → linkedin.com/in/... URL
  - github        → github.com/... URL
  - company       → "currently at X" / "at X as" patterns
  - title         → "as a <title>" / "position: <title>" patterns
  - skills        → comma-separated after "skills:", "experience in", "with X and Y"
  - location      → "located in", "based in", "location:" patterns
  - headline      → first long sentence that looks like a summary
  - years_experience → "X+ years" / "X years of experience" pattern

Edge cases:
  - File not found        → warning + empty list
  - Non-UTF8 encoding     → falls back to latin-1
  - No extractable fields → returns empty list (no invented data)
"""
from __future__ import annotations
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Regex Patterns ───────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_PHONE_RE = re.compile(
    r"(?<!\d)"                        # not preceded by digit
    r"(?!\d{4}-\d{2}-\d{2})"          # not a date like 2024-01-15
    r"(\+?\d[\d\s.\-()]{7,17}\d)"    # flexible phone pattern
    r"(?!\d)"                         # not followed by digit
)

_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-]+", re.IGNORECASE)
_GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[\w\-]+", re.IGNORECASE)

# Name: "Candidate: Jordan Lee" / "Name: Jordan Lee" — stops at newline or sentence end
_NAME_LABEL_RE = re.compile(
    r"(?:candidate|name|applicant)\s*:\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*(?:\n|$)",
    re.IGNORECASE,
)

# Years of experience: "7+ years", "7 years of experience", "seven years"
_YOE_RE = re.compile(
    r"(\d+)\+?\s*years?\s*(?:of\s+)?(?:experience|exp\.?)?",
    re.IGNORECASE,
)

# Company: "currently at Stripe", "working at Google", "at Dropbox as"
_COMPANY_RE = re.compile(
    r"(?:currently at|working at|employed at|at)\s+([A-Z][A-Za-z0-9&.,\s]{1,40}?)(?:\s+as|\s+in|\.|,|$)",
    re.IGNORECASE,
)

# Title: "as a Senior SWE", "as Senior Engineer", "position: X", "role: X", "title: X"
_TITLE_RE = re.compile(
    r"(?:"
    r"as\s+(?:a\s+|an\s+)?([A-Z][A-Za-z\s/]{3,50})"
    r"|(?:position|role|title)\s*:\s*([A-Za-z\s/]{3,50})"
    r")",
    re.IGNORECASE,
)

# Location: "based in San Francisco", "located in NYC", "location: X"
_LOCATION_RE = re.compile(
    r"(?:based in|located in|location\s*:|living in|resides in)\s+([A-Za-z,\s]{3,50}?)(?:\.|,|$|\n)",
    re.IGNORECASE,
)

# Skills block: "skills: Python, Go, Kubernetes" / "experience in Python and Go"
_SKILLS_LABEL_RE = re.compile(
    r"(?:skills?|technologies|tech stack|proficient in|experience (?:in|with)|comfortable with)\s*[:\-]?\s*"
    r"([A-Za-z0-9+#.,/\s]{3,200}?)(?:\.|$|\n)",
    re.IGNORECASE,
)

# Individual skill mentions after common keywords
_SKILL_TOKEN_RE = re.compile(
    r"\b(Python|Go(?:lang)?|JavaScript|TypeScript|Java|C\+\+|C#|Rust|Ruby|PHP|Swift|Kotlin"
    r"|React|Vue|Angular|Node\.?js|Next\.?js|Django|Flask|FastAPI|Spring"
    r"|PostgreSQL|MySQL|MongoDB|Redis|Elasticsearch|BigQuery"
    r"|AWS|GCP|Azure|Docker|Kubernetes|Terraform|CI/?CD"
    r"|TensorFlow|PyTorch|scikit-learn|pandas|NumPy|Spark"
    r"|GraphQL|REST|SQL|HTML|CSS|Git|Linux|Bash)\b",
    re.IGNORECASE,
)

# Headline/summary: first sentence over 40 chars that reads like a description
_SUMMARY_RE = re.compile(
    r"(?:summary|profile|about|note)\s*[:\-]\s*(.{40,300}?)(?:\n|$)",
    re.IGNORECASE,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _read_file(file_path: str) -> Optional[str]:
    """Read file with UTF-8 fallback to latin-1."""
    for encoding in ("utf-8", "latin-1"):
        try:
            with open(file_path, encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
        except FileNotFoundError:
            logger.warning("TXT file not found: %s", file_path)
            return None
        except Exception as e:
            logger.warning("Failed to read TXT file %s: %s", file_path, e)
            return None
    return None


def _extract_name(text: str) -> Optional[str]:
    """
    Try labelled match first ("Candidate: Jordan Lee"),
    then fall back to scanning for a standalone capitalized name line
    in the first 5 non-empty lines.
    """
    # Labelled
    m = _NAME_LABEL_RE.search(text)
    if m:
        return m.group(1).strip()

    # Heuristic: scan first 8 lines for a "First Last" pattern
    for line in text.split("\n")[:8]:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip lines with @ (email), digits (phone), colons (label), URLs
        if any(c in stripped for c in ("@", ":", "http", "/")):
            continue
        # Check for 2-4 words, all title-cased
        words = stripped.split()
        if 2 <= len(words) <= 4 and all(w[0].isupper() for w in words if w.isalpha()):
            return stripped

    return None


def _extract_skills(text: str) -> list[str]:
    """
    Two-pass skill extraction:
    1. Look for a labeled skills block and parse comma-separated values
    2. Scan for known skill tokens across the full text
    """
    skills: list[str] = []

    # Pass 1: labeled block
    for m in _SKILLS_LABEL_RE.finditer(text):
        raw_block = m.group(1)
        tokens = re.split(r"[,;/\n]+", raw_block)
        for tok in tokens:
            tok = tok.strip().strip(".").strip()
            if 1 < len(tok) < 40:
                skills.append(tok)

    # Pass 2: known tech token scan (deduplicate)
    seen_lower = {s.lower() for s in skills}
    for m in _SKILL_TOKEN_RE.finditer(text):
        tok = m.group(0).strip()
        if tok.lower() not in seen_lower:
            skills.append(tok)
            seen_lower.add(tok.lower())

    return skills


def _extract_title(text: str) -> Optional[str]:
    """Extract job title from common patterns."""
    for m in _TITLE_RE.finditer(text):
        # Group 1 = "as a X", Group 2 = "title: X"
        val = (m.group(1) or m.group(2) or "").strip()
        # Reject if it looks like a pronoun/connector word
        if len(val) > 4 and not val.lower().startswith(("the ", "a ", "an ")):
            return val
    return None


def _extract_company(text: str) -> Optional[str]:
    """Extract current company from common patterns."""
    m = _COMPANY_RE.search(text)
    if m:
        return m.group(1).strip().rstrip(".,")
    return None


def _extract_location(text: str) -> Optional[str]:
    """Extract location string from common patterns."""
    m = _LOCATION_RE.search(text)
    if m:
        return m.group(1).strip().rstrip(".,")
    return None


def _extract_yoe(text: str) -> Optional[float]:
    """Extract years of experience as a float."""
    m = _YOE_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except (ValueError, TypeError):
            pass
    return None


def _extract_headline(text: str) -> Optional[str]:
    """Extract a summary/headline sentence from labeled block or long opening sentence."""
    # Labeled
    m = _SUMMARY_RE.search(text)
    if m:
        val = m.group(1).strip()
        if len(val) > 20:
            return val[:300]

    # Fallback: first line > 60 chars that looks descriptive
    for line in text.split("\n"):
        stripped = line.strip()
        if len(stripped) > 60 and not stripped.startswith(("#", "-", "*")):
            return stripped[:300]

    return None


# ─── Main Extractor ───────────────────────────────────────────────────────────

def extract_txt(file_path: str) -> list[dict]:
    """
    Parse a free-text recruiter notes file and return a list with one candidate dict.
    Returns empty list if file is unreadable or yields no meaningful data.
    Never raises.
    """
    text = _read_file(file_path)
    if not text or not text.strip():
        logger.warning("Empty or unreadable TXT file: %s", file_path)
        return []

    candidate: dict = {"_source": "txt", "_source_file": file_path}

    # Name
    name = _extract_name(text)
    if name:
        candidate["full_name"] = name

    # Email
    email_m = _EMAIL_RE.search(text)
    if email_m:
        candidate["email"] = email_m.group().strip()

    # Phone
    phone_m = _PHONE_RE.search(text)
    if phone_m:
        candidate["phone"] = phone_m.group(1).strip()

    # LinkedIn
    li_m = _LINKEDIN_RE.search(text)
    if li_m:
        url = li_m.group()
        candidate["linkedin"] = url if url.startswith("http") else f"https://{url}"

    # GitHub
    gh_m = _GITHUB_RE.search(text)
    if gh_m:
        url = gh_m.group()
        candidate["github"] = url if url.startswith("http") else f"https://{url}"

    # Company
    company = _extract_company(text)
    if company:
        candidate["company"] = company

    # Title
    title = _extract_title(text)
    if title:
        candidate["title"] = title

    # Location
    location = _extract_location(text)
    if location:
        candidate["location"] = location

    # Years of experience
    yoe = _extract_yoe(text)
    if yoe is not None:
        candidate["years_experience"] = yoe

    # Skills
    skills = _extract_skills(text)
    if skills:
        candidate["skills"] = skills

    # Headline
    headline = _extract_headline(text)
    if headline:
        candidate["headline"] = headline

    # Only return if we extracted at least one meaningful field
    meaningful = {k for k in candidate if not k.startswith("_")}
    if not meaningful:
        logger.warning("No extractable fields found in TXT file: %s", file_path)
        return []

    return [candidate]
