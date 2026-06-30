"""
PDF Extractor — extracts candidate data from a resume PDF using pdfplumber.

Approach:
  1. Extract all text from the PDF
  2. Apply regex patterns to find structured fields (email, phone, LinkedIn, GitHub)
  3. Use section-header heuristics to identify Experience, Education, Skills blocks
  4. Return a raw candidate dict

Design decision: We use deterministic regex/heuristic extraction only — no LLM.
This keeps results reproducible and explainable.
If pdfplumber is unavailable, we log a warning and return empty.

Edge cases handled:
  - Password-protected PDFs → caught and skipped
  - Scanned image PDFs (no text layer) → empty text → return empty dict
  - Malformed PDFs → caught and skipped
"""
from __future__ import annotations
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Regex patterns ───────────────────────────────────────────
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s\-.]?)?"
    r"(?:\(?\d{3}\)?[\s\-.]?)?"
    r"\d{3}[\s\-.]?\d{4}"
)
_LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w\-]+", re.IGNORECASE)
_GITHUB_RE = re.compile(r"github\.com/[\w\-]+", re.IGNORECASE)
_PORTFOLIO_RE = re.compile(
    r"https?://(?!(?:www\.)?(?:linkedin|github)\.com)[\w\-\.]+\.[\w]{2,}(?:/[\S]*)?",
    re.IGNORECASE,
)

# Section header patterns (order matters — more specific first)
_SECTION_HEADERS = {
    "experience": re.compile(
        r"^(work experience|experience|employment|professional experience|career history)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "education": re.compile(
        r"^(education|academic background|qualifications|degrees)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "skills": re.compile(
        r"^(skills|technical skills|competencies|technologies|core skills|skill set)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "summary": re.compile(
        r"^(summary|professional summary|profile|about me|objective)",
        re.IGNORECASE | re.MULTILINE,
    ),
}

_DATE_RANGE_RE = re.compile(
    r"(\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}|\d{4})"
    r"\s*[–—\-–to]+\s*"
    r"(\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}|\d{4}|present|current)",
    re.IGNORECASE,
)


def _extract_text(file_path: str) -> str:
    """Extract all text from a PDF using pdfplumber. Returns empty string on failure."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed. Cannot extract PDF text.")
        return ""

    text_parts = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
    except Exception as e:
        logger.warning("Failed to extract text from PDF %s: %s", file_path, e)

    return "\n".join(text_parts)


def _split_sections(text: str) -> dict[str, str]:
    """
    Split resume text into named sections based on header detection.
    Returns a dict of section_name → section_text.
    """
    lines = text.split("\n")
    sections: dict[str, list[str]] = {"_header": []}
    current_section = "_header"

    for line in lines:
        stripped = line.strip()
        matched_section = None
        for section_name, pattern in _SECTION_HEADERS.items():
            if pattern.match(stripped):
                matched_section = section_name
                break
        if matched_section:
            current_section = matched_section
            sections.setdefault(current_section, [])
        else:
            sections.setdefault(current_section, []).append(line)

    return {k: "\n".join(v) for k, v in sections.items()}


def _parse_name_from_header(header_text: str) -> Optional[str]:
    """
    Heuristic: the candidate name is usually one of the first 3 non-empty lines,
    title-cased, not an email/phone, and reasonably short (< 60 chars).
    """
    for line in header_text.split("\n")[:6]:
        stripped = line.strip()
        if not stripped:
            continue
        if _EMAIL_RE.search(stripped) or _PHONE_RE.search(stripped):
            continue
        if len(stripped) < 4 or len(stripped) > 60:
            continue
        # Likely a name if it looks like "First Last" or "First Middle Last"
        words = stripped.split()
        if 1 <= len(words) <= 4 and all(w[0].isupper() if w else True for w in words):
            return stripped
    return None


def _parse_skills(skills_text: str) -> list[str]:
    """
    Extract skills from a skills section.
    Handles comma-separated, bullet-point, and newline-separated formats.
    """
    # Remove bullet characters
    cleaned = re.sub(r"[•·▪▸►‣▷◦➤➢➔]", ",", skills_text)
    # Split on commas, pipes, semicolons, newlines
    tokens = re.split(r"[,|;\n]+", cleaned)
    skills = []
    for token in tokens:
        t = token.strip().strip("•-–—").strip()
        if t and 1 < len(t) < 50:
            skills.append(t)
    return skills


def _parse_experience(exp_text: str) -> list[dict]:
    """
    Heuristic experience parser.
    Looks for company/title pairs near date ranges.
    """
    entries = []
    date_matches = list(_DATE_RANGE_RE.finditer(exp_text))

    for i, dm in enumerate(date_matches):
        # Look at text just before the date match for company/title
        start_pos = dm.start()
        preceding = exp_text[max(0, start_pos - 200): start_pos]
        lines = [l.strip() for l in preceding.split("\n") if l.strip()]

        entry: dict = {
            "start": dm.group(1),
            "end": dm.group(2).lower() if dm.group(2).lower() != "present" else None,
        }

        if lines:
            entry["company"] = lines[-1] if len(lines) >= 1 else None
            entry["title"] = lines[-2] if len(lines) >= 2 else None

        # Get summary from text between this date and next date
        end_pos = date_matches[i + 1].start() if i + 1 < len(date_matches) else len(exp_text)
        summary_text = exp_text[dm.end(): end_pos].strip()
        if summary_text and len(summary_text) < 500:
            entry["summary"] = summary_text

        entries.append(entry)

    return entries


def _parse_education(edu_text: str) -> list[dict]:
    """Parse education section — looks for institution + degree patterns."""
    entries = []
    lines = [l.strip() for l in edu_text.split("\n") if l.strip()]

    degree_keywords = re.compile(
        r"\b(b\.?s\.?|b\.?a\.?|m\.?s\.?|m\.?a\.?|ph\.?d\.?|bachelor|master|"
        r"associate|diploma|mba|btech|mtech|b\.?e\.?)\b",
        re.IGNORECASE,
    )

    year_re = re.compile(r"\b(19|20)\d{2}\b")

    for i, line in enumerate(lines):
        if degree_keywords.search(line):
            entry: dict = {"degree": line}
            # Look adjacent lines for institution and year
            for adj in lines[max(0, i - 2): i] + lines[i + 1: i + 3]:
                if year_re.search(adj):
                    match = year_re.search(adj)
                    if match:
                        entry["end_year"] = int(match.group())
                elif len(adj) > 5 and "institution" not in entry:
                    entry["institution"] = adj
            entries.append(entry)

    return entries


def extract_pdf(file_path: str) -> list[dict]:
    """
    Extract candidate data from a resume PDF.
    Returns a list with one candidate dict, or empty list on failure.
    """
    text = _extract_text(file_path)
    if not text.strip():
        logger.warning("No text extracted from PDF (possibly scanned or empty): %s", file_path)
        return []

    sections = _split_sections(text)
    header_text = sections.get("_header", "")

    candidate: dict = {"_source": "pdf", "_source_file": file_path}

    # Name
    name = _parse_name_from_header(header_text)
    if name:
        candidate["full_name"] = name

    # Email
    email_match = _EMAIL_RE.search(text)
    if email_match:
        candidate["email"] = email_match.group()

    # Phone
    phone_match = _PHONE_RE.search(text)
    if phone_match:
        candidate["phone"] = phone_match.group()

    # LinkedIn
    li_match = _LINKEDIN_RE.search(text)
    if li_match:
        candidate["linkedin"] = "https://" + li_match.group()

    # GitHub
    gh_match = _GITHUB_RE.search(text)
    if gh_match:
        candidate["github"] = "https://" + gh_match.group()

    # Headline / summary
    summary_text = sections.get("summary", "").strip()
    if summary_text and len(summary_text) < 400:
        candidate["headline"] = summary_text[:200]

    # Skills
    skills_text = sections.get("skills", "")
    if skills_text:
        skills = _parse_skills(skills_text)
        if skills:
            candidate["skills"] = skills

    # Experience
    exp_text = sections.get("experience", "")
    if exp_text:
        experience = _parse_experience(exp_text)
        if experience:
            candidate["experience"] = experience

    # Education
    edu_text = sections.get("education", "")
    if edu_text:
        education = _parse_education(edu_text)
        if education:
            candidate["education"] = education

    return [candidate]
