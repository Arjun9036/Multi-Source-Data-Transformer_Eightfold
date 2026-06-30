"""
Eightfold Candidate Profile Normalizer — FastAPI Server

Endpoints:
    GET  /                    → redirect to /docs
    GET  /health              → health check
    GET  /configs             → list available built-in configs
    GET  /sample              → run pipeline on built-in sample inputs
    POST /normalize           → upload files + optional config → canonical profile
    POST /normalize/github    → normalize a GitHub profile URL
    POST /normalize/text      → paste raw recruiter notes as plain text

Swagger UI:  http://localhost:8000/docs
ReDoc:       http://localhost:8000/redoc

Run with:
    python3 api.py
    -- OR --
    uvicorn api:app --reload --port 8000
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from pipeline.detector import detect_source_type
from pipeline.extractors.csv_extractor import extract_csv
from pipeline.extractors.json_extractor import extract_json
from pipeline.extractors.github_extractor import extract_github
from pipeline.extractors.pdf_extractor import extract_pdf
from pipeline.extractors.txt_extractor import extract_txt
from pipeline.merger import merge_candidates
from pipeline.projector import project
from pipeline.validator import validate_output

logger = logging.getLogger(__name__)

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Eightfold Candidate Profile Normalizer",
    description="""
## What this does
Takes messy candidate data from **multiple sources** (CSV, recruiter notes, ATS JSON, GitHub, PDF)
and produces one clean, deduplicated, provenance-tracked **canonical profile**.

## Key features
- **Structured source**: Recruiter CSV — column alias mapping, skill normalization
- **Unstructured source**: Recruiter Notes (.txt) — regex + token scanning
- **Normalization**: phones → E.164 · dates → YYYY-MM · skills → canonical names
- **Conflict resolution**: ATS JSON > CSV > PDF > Notes (source priority tiers)
- **Configurable projection**: rename fields, select subsets, toggle confidence, set on_missing policy

## Quick start
1. Use **GET /sample** to see the pipeline run on built-in sample inputs
2. Use **POST /normalize** to upload your own files
3. Use **POST /normalize/github** to normalize a GitHub profile

## Design philosophy
> *"Wrong-but-confident is worse than honestly-empty."*  
> Every field is traceable. Unknown values become `null`, never invented.
    """,
    version="1.0.0",
    contact={
        "name": "Arjun Goyal",
        "email": "arjun.22bce9036@vitapstudent.ac.in",
    },
    license_info={"name": "MIT"},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
CONFIG_DIR = ROOT / "config"
SAMPLE_CSV = ROOT / "sample_inputs" / "recruiter_export.csv"
SAMPLE_TXT = ROOT / "sample_inputs" / "recruiter_notes.txt"
SAMPLE_JSON = ROOT / "sample_inputs" / "ats_blob.json"

BUILT_IN_CONFIGS = {
    "default": CONFIG_DIR / "default_config.json",
    "custom": CONFIG_DIR / "custom_config.json",
}

EXTRACTOR_MAP = {
    "csv": extract_csv,
    "json": extract_json,
    "github_url": extract_github,
    "pdf": extract_pdf,
    "txt": extract_txt,
}


# ── Core pipeline helper ──────────────────────────────────────────────────────

def _load_config(config_name: str = "default", config_json: Optional[str] = None) -> dict:
    """Load a config — from inline JSON string or by name (default/custom)."""
    # Swagger UI sends "" for empty optional form fields — treat as None
    if config_json and config_json.strip():
        try:
            return json.loads(config_json.strip())
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=422, detail=f"Invalid config JSON: {e}")


    path = BUILT_IN_CONFIGS.get(config_name)
    if not path or not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Config '{config_name}' not found. Available: {list(BUILT_IN_CONFIGS.keys())}",
        )
    return json.loads(path.read_text())


def _extract_from_path(file_path: str) -> tuple[list[dict], str]:
    """Detect type and extract records from a file path. Returns (records, source_type)."""
    source_type = detect_source_type(file_path)
    extractor = EXTRACTOR_MAP.get(source_type)
    if not extractor:
        logger.warning("No extractor for source type '%s': %s", source_type, file_path)
        return [], source_type
    try:
        records = extractor(file_path)
        return records, source_type
    except Exception as e:
        logger.warning("Extraction failed for %s: %s", file_path, e)
        return [], source_type


def _run_pipeline(
    file_paths: list[str],
    github_url: Optional[str],
    config: dict,
) -> dict:
    """
    Run the full pipeline on a list of file paths + optional GitHub URL.
    Returns the projected, validated output dict.
    """
    all_records: list[dict] = []
    source_log: list[dict] = []

    # Extract from files
    for path in file_paths:
        records, source_type = _extract_from_path(path)
        all_records.extend(records)
        source_log.append({
            "source": Path(path).name,
            "type": source_type,
            "records_extracted": len(records),
        })

    # Extract from GitHub URL
    if github_url:
        try:
            gh_records = extract_github(github_url)
            all_records.extend(gh_records)
            source_log.append({
                "source": github_url,
                "type": "github_url",
                "records_extracted": len(gh_records),
            })
        except Exception as e:
            logger.warning("GitHub extraction failed: %s", e)
            source_log.append({"source": github_url, "type": "github_url", "error": str(e)})

    if not all_records:
        raise HTTPException(
            status_code=422,
            detail="No candidate records could be extracted from the provided sources.",
        )

    # Merge
    profile = merge_candidates(all_records)
    profile_dict = profile.model_dump()

    # Project
    try:
        output = project(profile_dict, config)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Projection error: {e}")

    # Validate
    validation = validate_output(output, config)
    if not validation.is_valid:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Output failed validation",
                "violations": validation.violations,
            },
        )

    return {"pipeline_log": source_log, "profile": output}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    """Redirect root to Swagger UI."""
    return RedirectResponse(url="/docs")


@app.get(
    "/health",
    tags=["System"],
    summary="Health check",
    response_description="Service status",
)
def health():
    """Returns 200 OK if the service is running."""
    return {"status": "ok", "service": "eightfold-normalizer", "version": "1.0.0"}


@app.get(
    "/configs",
    tags=["System"],
    summary="List available built-in configs",
    response_description="Config names and their full JSON",
)
def list_configs():
    """
    Returns all built-in runtime configs.

    - **default** — full canonical schema with confidence + provenance
    - **custom** — renamed fields, no confidence metadata, uses `from` path syntax
    """
    result = {}
    for name, path in BUILT_IN_CONFIGS.items():
        result[name] = json.loads(path.read_text())
    return result


@app.get(
    "/sample",
    tags=["Pipeline"],
    summary="Run pipeline on built-in sample inputs",
    response_description="Canonical profile from sample CSV + TXT inputs",
)
def run_sample(
    config: str = Query(
        default="default",
        description="Which built-in config to use: **default** or **custom**",
        enum=["default", "custom"],
    )
):
    """
    Runs the pipeline on the **built-in sample inputs** (no file upload needed).

    Sources used:
    - `sample_inputs/recruiter_export.csv` — structured (Recruiter CSV)
    - `sample_inputs/recruiter_notes.txt` — unstructured (Recruiter Notes)

    Great for quickly seeing what the pipeline does without uploading anything.
    """
    if not SAMPLE_CSV.exists() or not SAMPLE_TXT.exists():
        raise HTTPException(status_code=500, detail="Sample input files not found on server.")

    loaded_config = _load_config(config_name=config)
    result = _run_pipeline(
        file_paths=[str(SAMPLE_CSV), str(SAMPLE_TXT)],
        github_url=None,
        config=loaded_config,
    )
    return result


@app.post(
    "/normalize",
    tags=["Pipeline"],
    summary="Normalize candidate profile from uploaded files",
    response_description="Merged, normalized canonical profile",
)
async def normalize(
    file: UploadFile = File(
        ...,
        description="**Required.** Source file — `.csv`, `.txt`, `.json`, or `.pdf`",
    ),
    config_name: str = Form(
        default="default",
        description="Built-in config: **default** (full schema) or **custom** (renamed fields, no confidence)",
    ),
):
    """
    **Main endpoint.** Upload a candidate source file and get back a normalized profile.

    ### Supported file types
    | Extension | Source type | What it extracts |
    |-----------|------------|-----------------|
    | `.csv` | Recruiter CSV | name, email, phone, company, title, skills, location |
    | `.txt` | Recruiter Notes | same fields via regex + token scan |
    | `.json` | ATS JSON blob | any schema via path-based mapping |
    | `.pdf` | Resume PDF | name, email, phone, GitHub, skills, education |

    ### How to use in Swagger UI
    1. Click the **Choose File** button next to `file` → select your file
    2. Set `config_name` = **default** or **custom**
    3. Click **Execute**

    ### Response
    Returns `pipeline_log` (records extracted per source) and the final `profile`.
    """
    loaded_config = _load_config(config_name=config_name, config_json=None)

    uploads = [file]
    tmp_paths: list[str] = []
    try:
        for upload in uploads:
            suffix = Path(upload.filename or "file").suffix or ".tmp"
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=suffix, prefix="ef_upload_"
            ) as tmp:
                content = await upload.read()
                tmp.write(content)
                tmp_paths.append(tmp.name)

        result = _run_pipeline(
            file_paths=tmp_paths,
            github_url=None,
            config=loaded_config,
        )
        return result

    finally:

        # Always clean up temp files
        for path in tmp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass


@app.post(
    "/normalize/github",
    tags=["Pipeline"],
    summary="Normalize a GitHub profile URL",
    response_description="Canonical profile extracted from GitHub",
)
def normalize_github(
    github_url: str = Form(
        ...,
        description="Public GitHub profile URL, e.g. `https://github.com/torvalds`",
    ),
    config_name: str = Form(
        default="default",
        description="Built-in config to use: **default** or **custom**",
    ),
):
    """
    Fetch a GitHub profile via the **GitHub REST API** and normalize it into
    a canonical profile.

    Extracts: name, email (if public), bio (headline), repos → skills,
    languages → skills, location, GitHub URL.

    > Note: GitHub API is rate-limited to 60 requests/hour without auth.
    """
    loaded_config = _load_config(config_name=config_name)
    result = _run_pipeline(file_paths=[], github_url=github_url, config=loaded_config)
    return result


@app.post(
    "/normalize/text",
    tags=["Pipeline"],
    summary="Normalize pasted recruiter notes (plain text)",
    response_description="Canonical profile extracted from plain text notes",
)
async def normalize_text(
    notes: str = Form(
        ...,
        description="Paste recruiter notes as plain text. "
                    "Include name, email, phone, skills, company, etc. in any format.",
    ),
    config_name: str = Form(
        default="default",
        description="Built-in config to use: **default** or **custom**",
    ),
):
    """
    Paste **raw recruiter notes** as plain text and get a normalized profile back.

    The text parser uses regex + token scanning to extract:
    - Name (from `Candidate:` label or first capitalized name line)
    - Email, phone (robust patterns)
    - LinkedIn / GitHub URLs
    - Company + title (from `currently at X as Y` patterns)
    - Skills (from `Skills:` label or known tech token scan)
    - Location, years of experience, headline

    **Example input:**
    ```
    Candidate: Jordan Lee
    Currently at Stripe as Senior SWE
    Skills: Python, Go, Kubernetes
    jordan.lee@email.com | +1 (415) 555-0192
    Based in San Francisco. 7+ years experience.
    ```
    """
    loaded_config = _load_config(config_name=config_name)

    # Write text to a temp .txt file and run the extractor
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="ef_notes_", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(notes)
        tmp_path = tmp.name

    try:
        result = _run_pipeline(
            file_paths=[tmp_path],
            github_url=None,
            config=loaded_config,
        )
        return result
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
