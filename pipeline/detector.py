"""
Detector — identifies the source type from a file path or URL string.
Returns one of: 'csv', 'json', 'github_url', 'linkedin_url', 'pdf', 'docx', 'txt', 'unknown'
"""
import os
import re
from pathlib import Path


def detect_source_type(source: str) -> str:
    """
    Given a file path or URL, return a source type string.
    This is deterministic — same input always returns same type.
    """
    source = source.strip()

    # URL-based detection
    if source.startswith("http://") or source.startswith("https://"):
        if re.search(r"github\.com/[^/]+/?$", source, re.IGNORECASE):
            return "github_url"
        if re.search(r"linkedin\.com/in/", source, re.IGNORECASE):
            return "linkedin_url"
        return "unknown_url"

    # File-based detection
    path = Path(source)
    ext = path.suffix.lower()

    ext_map = {
        ".csv": "csv",
        ".json": "json",
        ".pdf": "pdf",
        ".docx": "docx",
        ".txt": "txt",
    }

    return ext_map.get(ext, "unknown")
