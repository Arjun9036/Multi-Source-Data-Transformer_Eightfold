# Eightfold Candidate Profile Normalizer

A deterministic, provenance-tracked pipeline that ingests candidate data from multiple sources, normalizes formats, deduplicates, merges, and emits a clean canonical JSON profile — with a runtime config to reshape the output without code changes.

## Architecture

```
Input Sources (CSV / ATS JSON / GitHub URL / Resume PDF)
     │
     ▼
┌─────────────┐
│  Detector   │  identifies source type
└──────┬──────┘
       ▼
┌─────────────┐
│  Extractors │  source-specific parsers (4 extractors)
└──────┬──────┘
       ▼
┌─────────────┐
│  Normalizer │  phones→E.164, dates→YYYY-MM, country→ISO, skills→canonical
└──────┬──────┘
       ▼
┌─────────────┐
│   Merger    │  dedup by email, conflict resolution by source weight,
└──────┬──────┘  confidence scoring, provenance tracking
       ▼
┌─────────────┐
│  Projector  │  applies runtime config (field selection, renaming, on_missing)
└──────┬──────┘
       ▼
┌─────────────┐
│  Validator  │  schema + type + format validation
└──────┬──────┘
       ▼
  JSON Output
```

## Requirements

- Python 3.11+
- pip packages: see `requirements.txt`

## Setup

```bash
cd eightfold-candidate-normalizer

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows use: venv\Scripts\activate

# Install dependencies
python3 -m pip install -r requirements.txt
```

## Running the API Server (FastAPI)

The project includes a FastAPI backend with interactive Swagger UI documentation.

```bash
# Make sure your virtual environment is active
source venv/bin/activate

# Start the server
python3 api.py
```

Then open **http://localhost:8000/docs** in your browser to access the Swagger UI.

- **`POST /normalize`**: Upload a candidate file (CSV, PDF, JSON, TXT), select a config, and get back the canonical profile.
- **`GET /sample`**: Zero-click demo that runs the pipeline on built-in sample files.

## How to Run (CLI)

### Basic — CSV + ATS JSON (default schema)
```bash
python main.py \
  -i sample_inputs/recruiter_export.csv \
  -i sample_inputs/ats_blob.json
```

### With GitHub source (live API call)
```bash
python main.py \
  -i sample_inputs/ats_blob.json \
  -g https://github.com/torvalds
```

### Custom config (field renaming, subset, confidence off)
```bash
python main.py \
  -i sample_inputs/recruiter_export.csv \
  -i sample_inputs/ats_blob.json \
  -c config/custom_config.json \
  -o sample_outputs/custom_output.json
```

### Write default output to file
```bash
python main.py \
  -i sample_inputs/recruiter_export.csv \
  -i sample_inputs/ats_blob.json \
  -o sample_outputs/default_output.json
```

### Verbose mode
```bash
python main.py -i sample_inputs/ats_blob.json -v
```

### Help
```bash
python main.py --help
```

## Runtime Config

The pipeline accepts a JSON config that reshapes the output **without code changes**:

```json
{
  "fields": [
    { "path": "full_name", "type": "string", "required": true },
    { "path": "primary_email", "from": "emails[0]", "type": "string", "required": true },
    { "path": "phone", "from": "phones[0]", "type": "string", "normalize": "E164" },
    { "path": "skills_list", "from": "skills[].name", "type": "string[]", "normalize": "canonical" }
  ],
  "include_confidence": true,
  "on_missing": "null"
}
```

**Config options:**
| Key | Description |
|-----|-------------|
| `path` | Output field name |
| `from` | Source path in canonical record (supports dot-notation, `[0]` indexing, `[].field` mapping) |
| `type` | `string`, `string[]`, `number`, `boolean` |
| `normalize` | `E164` (phones), `canonical` (skills), `lowercase` |
| `required` | If `true` + missing → controlled by `on_missing` |
| `include_confidence` | Toggle `_confidence` + `_provenance` in output |
| `on_missing` | `"null"` (default), `"omit"`, or `"error"` |

## Running Tests

```bash
pytest tests/ -v
```

Expected output: all tests green across `test_normalizer.py`, `test_merger.py`, `test_edge_cases.py`.

## Source Types Supported

| Source | Type | Notes |
|--------|------|-------|
| Recruiter CSV | Structured | Flexible column alias mapping |
| ATS JSON blob | Structured | Path-based field mapping, multi-schema |
| GitHub profile URL | Unstructured | REST API, rate-limit safe |
| Resume PDF | Unstructured | pdfplumber + heuristic regex |

## Design Decisions

### Conflict Resolution
Sources are ranked by tier (ATS JSON=0.95 → CSV=0.85 → PDF=0.75 → GitHub=0.70 → Notes=0.55). For scalar conflicts, the higher-weight source wins. For lists (emails, phones, skills), we take the union and deduplicate.

### Determinism
All extraction, normalization, and merging is rule-based. Same inputs always produce identical output. No LLM in the pipeline core.

### Provenance
Every field in the canonical profile tracks its source and extraction method in the `provenance` array. This is the core of the "honest" design — we prefer null over wrong.

### Confidence Scoring
`overall_confidence` is the weighted average of contributing sources. Per-skill confidence is boosted when the same skill appears across multiple sources.

### Robustness
- Missing files → empty list, warning logged, pipeline continues
- Invalid JSON → warning, pipeline continues  
- Invalid phone → excluded from output (never invented)
- GitHub rate limit → warning, source skipped
- Scanned PDF (no text) → warning, source skipped

## Assumptions & Descoped Items

- **LinkedIn**: Not implemented. LinkedIn blocks scraping. Mentioned in spec as optional; GitHub covers the unstructured source requirement.
- **Resume PDF**: Uses heuristic regex parsing, not LLM-based. Accurate for well-structured resumes; may miss complex layouts.
- **Multi-candidate merging**: The current grouping strategy merges all records from all inputs together. A production system would cluster by identity (email cluster, name fuzzy match) before merging per candidate.
- **Candidate deduplication across runs**: Not implemented (no persistent store). Each run is stateless.
- **DOCX parsing**: Extractor scaffolded; falls back gracefully if python-docx is not installed.
