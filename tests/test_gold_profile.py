"""
Gold Profile Test — end-to-end integration test.

Runs the full pipeline on:
  - sample_inputs/recruiter_export.csv  (structured)
  - sample_inputs/recruiter_notes.txt  (unstructured)

Then asserts that the output matches the expected "gold profile" fixture
at sample_outputs/gold_profile.json EXACTLY.

This test:
  1. Proves the pipeline is deterministic (same inputs → same output every time)
  2. Catches any regression in any pipeline stage
  3. Demonstrates the CSV + TXT combination required by the spec

Run with: pytest tests/test_gold_profile.py -v
"""
import sys
import json
import os
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from pipeline.detector import detect_source_type
from pipeline.extractors.csv_extractor import extract_csv
from pipeline.extractors.txt_extractor import extract_txt
from pipeline.merger import merge_candidates
from pipeline.projector import project
from pipeline.validator import validate_output

# ─── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
CSV_INPUT = str(ROOT / "sample_inputs" / "recruiter_export.csv")
TXT_INPUT = str(ROOT / "sample_inputs" / "recruiter_notes.txt")
GOLD_FILE = str(ROOT / "sample_outputs" / "gold_profile.json")
DEFAULT_CONFIG = str(ROOT / "config" / "default_config.json")


# ─── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def gold_profile() -> dict:
    """Load the expected gold profile from the fixture file."""
    assert os.path.exists(GOLD_FILE), (
        f"Gold profile fixture not found at {GOLD_FILE}. "
        "Run: python main.py -i sample_inputs/recruiter_export.csv "
        "-i sample_inputs/recruiter_notes.txt -o sample_outputs/gold_profile.json"
    )
    with open(GOLD_FILE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def default_config() -> dict:
    with open(DEFAULT_CONFIG) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def pipeline_output(default_config) -> dict:
    """Run the full pipeline end-to-end and return the projected output."""
    # Step 1: Extract
    csv_records = extract_csv(CSV_INPUT)
    txt_records = extract_txt(TXT_INPUT)
    all_records = csv_records + txt_records

    assert len(all_records) > 0, "No records extracted — check sample inputs"

    # Step 2: Merge
    profile = merge_candidates(all_records)

    # Step 3: Project
    profile_dict = profile.model_dump()
    output = project(profile_dict, default_config)

    return output


# ─── Gold Profile Exact Match Tests ────────────────────────────────────────────

class TestGoldProfileExactMatch:
    """Assert the pipeline output matches the gold fixture exactly."""

    def test_candidate_id_matches(self, pipeline_output, gold_profile):
        assert pipeline_output["candidate_id"] == gold_profile["candidate_id"], (
            f"candidate_id mismatch: got {pipeline_output['candidate_id']!r}, "
            f"expected {gold_profile['candidate_id']!r}"
        )

    def test_full_name_matches(self, pipeline_output, gold_profile):
        assert pipeline_output["full_name"] == gold_profile["full_name"]

    def test_emails_match(self, pipeline_output, gold_profile):
        # Order matters — dedup + union is deterministic
        assert pipeline_output["emails"] == gold_profile["emails"]

    def test_phones_match(self, pipeline_output, gold_profile):
        assert pipeline_output["phones"] == gold_profile["phones"]

    def test_location_matches(self, pipeline_output, gold_profile):
        assert pipeline_output["location"] == gold_profile["location"]

    def test_links_matches(self, pipeline_output, gold_profile):
        assert pipeline_output["links"] == gold_profile["links"]

    def test_headline_matches(self, pipeline_output, gold_profile):
        assert pipeline_output["headline"] == gold_profile["headline"]

    def test_years_experience_matches(self, pipeline_output, gold_profile):
        assert pipeline_output["years_experience"] == gold_profile["years_experience"]

    def test_skills_count_matches(self, pipeline_output, gold_profile):
        assert len(pipeline_output["skills"]) == len(gold_profile["skills"]), (
            f"Skills count mismatch: got {len(pipeline_output['skills'])}, "
            f"expected {len(gold_profile['skills'])}"
        )

    def test_skills_names_match(self, pipeline_output, gold_profile):
        got_names = [s["name"] for s in pipeline_output["skills"]]
        expected_names = [s["name"] for s in gold_profile["skills"]]
        assert got_names == expected_names

    def test_skills_sources_match(self, pipeline_output, gold_profile):
        got_sources = [s["sources"] for s in pipeline_output["skills"]]
        expected_sources = [s["sources"] for s in gold_profile["skills"]]
        assert got_sources == expected_sources

    def test_confidence_score_matches(self, pipeline_output, gold_profile):
        assert pipeline_output["_confidence"] == gold_profile["_confidence"]

    def test_provenance_fields_match(self, pipeline_output, gold_profile):
        got_fields = [p["field"] for p in pipeline_output["_provenance"]]
        expected_fields = [p["field"] for p in gold_profile["_provenance"]]
        assert got_fields == expected_fields

    def test_provenance_sources_match(self, pipeline_output, gold_profile):
        got_sources = [p["source"] for p in pipeline_output["_provenance"]]
        expected_sources = [p["source"] for p in gold_profile["_provenance"]]
        assert got_sources == expected_sources

    def test_full_output_is_deterministic(self, pipeline_output, gold_profile):
        """Run the pipeline a second time and assert identical output."""
        csv_records = extract_csv(CSV_INPUT)
        txt_records = extract_txt(TXT_INPUT)
        profile2 = merge_candidates(csv_records + txt_records)
        with open(DEFAULT_CONFIG) as f:
            import json as _json
            config = _json.load(f)
        output2 = project(profile2.model_dump(), config)
        assert output2 == pipeline_output, "Pipeline is NOT deterministic — two runs produced different output"


# ─── Source-Specific Field Tests ───────────────────────────────────────────────

class TestSourceCoverage:
    """Verify that each source type actually contributed something."""

    def test_csv_contributed_emails(self, pipeline_output):
        """CSV is the highest-weight source — emails must come from it."""
        assert len(pipeline_output["emails"]) > 0
        provenance = {p["field"]: p["source"] for p in pipeline_output["_provenance"]}
        assert provenance.get("emails") == "csv"

    def test_txt_contributed_headline(self, pipeline_output):
        """TXT recruiter notes should supply the headline."""
        assert pipeline_output["headline"] is not None
        provenance = {p["field"]: p["source"] for p in pipeline_output["_provenance"]}
        assert provenance.get("headline") == "txt"

    def test_skills_come_from_both_sources(self, pipeline_output):
        """Skills from both CSV and TXT should appear in the merged output."""
        all_sources = set()
        for skill in pipeline_output["skills"]:
            all_sources.update(skill["sources"])
        assert "csv" in all_sources, "CSV skills not found in merged output"
        assert "txt" in all_sources, "TXT skills not found in merged output"

    def test_cross_source_skills_have_higher_confidence(self, pipeline_output):
        """Skills confirmed by both CSV and TXT should have higher confidence."""
        single_source_skills = [
            s for s in pipeline_output["skills"] if len(s["sources"]) == 1
        ]
        multi_source_skills = [
            s for s in pipeline_output["skills"] if len(s["sources"]) > 1
        ]
        if single_source_skills and multi_source_skills:
            avg_single = sum(s["confidence"] for s in single_source_skills) / len(single_source_skills)
            avg_multi = sum(s["confidence"] for s in multi_source_skills) / len(multi_source_skills)
            assert avg_multi > avg_single, (
                "Multi-source skills should have higher average confidence than single-source"
            )

    def test_phones_are_e164(self, pipeline_output):
        """All phones in output must be E.164 format."""
        import re
        e164_re = re.compile(r"^\+[1-9]\d{6,14}$")
        for phone in pipeline_output.get("phones", []):
            assert e164_re.match(phone), f"Phone '{phone}' is not E.164 format"

    def test_emails_are_lowercase(self, pipeline_output):
        """All emails must be normalized to lowercase."""
        for email in pipeline_output.get("emails", []):
            assert email == email.lower(), f"Email '{email}' is not lowercase"


# ─── Validation Tests ──────────────────────────────────────────────────────────

class TestValidation:
    """Verify the validator correctly accepts the gold output."""

    def test_gold_output_passes_validation(self, pipeline_output, default_config):
        result = validate_output(pipeline_output, default_config)
        assert result.is_valid, f"Gold output failed validation: {result.violations}"

    def test_all_required_fields_present(self, pipeline_output):
        """Fields marked required=true in the default config must be non-null."""
        required_fields = ["candidate_id", "full_name"]
        for field in required_fields:
            assert pipeline_output.get(field) is not None, (
                f"Required field '{field}' is null in gold output"
            )
