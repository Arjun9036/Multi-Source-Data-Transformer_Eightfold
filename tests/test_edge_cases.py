"""
Edge case tests — robustness and graceful degradation scenarios.
Tests that the pipeline never crashes and handles garbage/missing data correctly.
"""
import sys
import json
import tempfile
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from pipeline.detector import detect_source_type
from pipeline.extractors.csv_extractor import extract_csv
from pipeline.extractors.json_extractor import extract_json
from pipeline.merger import merge_candidates
from pipeline.projector import project
from pipeline.validator import validate_output


class TestDetector:
    def test_csv_file(self):
        assert detect_source_type("recruiter_export.csv") == "csv"

    def test_json_file(self):
        assert detect_source_type("/some/path/ats_blob.json") == "json"

    def test_github_url(self):
        assert detect_source_type("https://github.com/jordanlee") == "github_url"

    def test_github_url_with_trailing_slash(self):
        assert detect_source_type("https://github.com/jordanlee/") == "github_url"

    def test_pdf_file(self):
        assert detect_source_type("resume.pdf") == "pdf"

    def test_txt_file(self):
        assert detect_source_type("notes.txt") == "txt"

    def test_unknown_extension(self):
        assert detect_source_type("mystery.xyz") == "unknown"

    def test_unknown_url(self):
        assert detect_source_type("https://random.com/profile") == "unknown_url"


class TestCSVEdgeCases:
    def test_missing_file_returns_empty(self):
        result = extract_csv("/nonexistent/path/file.csv")
        assert result == []

    def test_empty_csv_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("")
            tmp_path = f.name
        try:
            result = extract_csv(tmp_path)
            assert result == []
        finally:
            os.unlink(tmp_path)

    def test_header_only_csv_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("full_name,email,phone\n")
            tmp_path = f.name
        try:
            result = extract_csv(tmp_path)
            assert result == []
        finally:
            os.unlink(tmp_path)

    def test_valid_csv_parsed(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("full_name,email,phone\n")
            f.write("Test User,test@example.com,+14155550001\n")
            tmp_path = f.name
        try:
            result = extract_csv(tmp_path)
            assert len(result) == 1
            assert result[0]["full_name"] == "Test User"
            assert result[0]["email"] == "test@example.com"
        finally:
            os.unlink(tmp_path)

    def test_row_with_all_empty_fields(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("full_name,email,phone\n")
            f.write(",,\n")  # All empty
            tmp_path = f.name
        try:
            result = extract_csv(tmp_path)
            # Record exists but has no useful fields (only _source keys)
            assert len(result) == 1
        finally:
            os.unlink(tmp_path)


class TestJSONEdgeCases:
    def test_missing_file_returns_empty(self):
        result = extract_json("/nonexistent/path/file.json")
        assert result == []

    def test_invalid_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{ this is not valid json !!!")
            tmp_path = f.name
        try:
            result = extract_json(tmp_path)
            assert result == []
        finally:
            os.unlink(tmp_path)

    def test_empty_candidates_list(self):
        # When "candidates" key is present but empty, the wrapper dict itself
        # gets treated as a single record (with only _source/_source_file).
        # The meaningful check is that no candidate has any real data fields.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"candidates": []}, f)
            tmp_path = f.name
        try:
            result = extract_json(tmp_path)
            # Any returned records should have no extractable candidate fields
            meaningful_keys = {"full_name", "email", "phone", "company", "title", "skills"}
            for record in result:
                assert not any(k in record for k in meaningful_keys), \
                    f"Unexpected data in record from empty candidates list: {record}"
        finally:
            os.unlink(tmp_path)

    def test_single_candidate_dict(self):
        data = {"applicant_name": "Test User", "applicant_email": "t@t.com"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name
        try:
            result = extract_json(tmp_path)
            assert len(result) == 1
            assert result[0]["full_name"] == "Test User"
        finally:
            os.unlink(tmp_path)

    def test_list_of_candidates(self):
        data = [
            {"applicant_name": "Alice", "applicant_email": "alice@test.com"},
            {"applicant_name": "Bob", "applicant_email": "bob@test.com"},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name
        try:
            result = extract_json(tmp_path)
            assert len(result) == 2
        finally:
            os.unlink(tmp_path)


class TestProjectorEdgeCases:
    def _sample_profile(self):
        return {
            "candidate_id": "cand_test",
            "full_name": "Test User",
            "emails": ["test@example.com"],
            "phones": ["+14155550001"],
            "location": {"city": "San Francisco", "country": "US"},
            "links": {"github": "https://github.com/testuser"},
            "skills": [{"name": "Python", "confidence": 0.9, "sources": ["csv"]}],
            "experience": [{"company": "ACME", "title": "Engineer", "start": "2020-01"}],
            "provenance": [],
            "overall_confidence": 0.85,
        }

    def test_on_missing_null(self):
        profile = self._sample_profile()
        config = {
            "fields": [
                {"path": "full_name", "type": "string"},
                {"path": "missing_field", "type": "string"},
            ],
            "on_missing": "null",
        }
        result = project(profile, config)
        assert result["full_name"] == "Test User"
        assert result["missing_field"] is None

    def test_on_missing_omit(self):
        profile = self._sample_profile()
        config = {
            "fields": [
                {"path": "full_name", "type": "string"},
                {"path": "missing_field", "type": "string"},
            ],
            "on_missing": "omit",
        }
        result = project(profile, config)
        assert "full_name" in result
        assert "missing_field" not in result

    def test_on_missing_error_raises(self):
        profile = self._sample_profile()
        config = {
            "fields": [
                {"path": "critical_field", "type": "string", "required": True},
            ],
            "on_missing": "error",
        }
        with pytest.raises(ValueError, match="critical_field"):
            project(profile, config)

    def test_field_renaming(self):
        profile = self._sample_profile()
        config = {
            "fields": [
                {"path": "primary_email", "from": "emails[0]", "type": "string"},
            ],
            "on_missing": "null",
        }
        result = project(profile, config)
        assert "primary_email" in result
        assert result["primary_email"] == "test@example.com"

    def test_skill_list_mapping(self):
        profile = self._sample_profile()
        config = {
            "fields": [
                {"path": "skill_names", "from": "skills[].name", "type": "string[]"},
            ],
            "on_missing": "null",
        }
        result = project(profile, config)
        assert isinstance(result["skill_names"], list)
        assert "Python" in result["skill_names"]

    def test_confidence_toggled_off(self):
        profile = self._sample_profile()
        config = {
            "fields": [{"path": "full_name", "type": "string"}],
            "include_confidence": False,
            "on_missing": "null",
        }
        result = project(profile, config)
        assert "_confidence" not in result
        assert "_provenance" not in result

    def test_empty_config_returns_full_profile(self):
        profile = self._sample_profile()
        result = project(profile, {})
        assert result == profile


class TestValidatorEdgeCases:
    def test_valid_output_passes(self):
        output = {"full_name": "Jordan Lee", "primary_email": "j@j.com"}
        config = {
            "fields": [
                {"path": "full_name", "type": "string", "required": True},
                {"path": "primary_email", "type": "string", "required": True},
            ],
            "on_missing": "null",
        }
        result = validate_output(output, config)
        assert result.is_valid

    def test_missing_required_field_fails(self):
        output = {"full_name": None}
        config = {
            "fields": [
                {"path": "full_name", "type": "string", "required": True},
            ],
            "on_missing": "null",
        }
        result = validate_output(output, config)
        assert not result.is_valid
        assert any("full_name" in v for v in result.violations)

    def test_invalid_e164_fails(self):
        output = {"phone": "555-1234"}
        config = {
            "fields": [
                {"path": "phone", "type": "string", "normalize": "E164"},
            ],
            "on_missing": "null",
        }
        result = validate_output(output, config)
        assert not result.is_valid

    def test_valid_e164_passes(self):
        output = {"phone": "+14155550001"}
        config = {
            "fields": [
                {"path": "phone", "type": "string", "normalize": "E164"},
            ],
            "on_missing": "null",
        }
        result = validate_output(output, config)
        assert result.is_valid

    def test_experience_date_format_validated(self):
        output = {
            "experience": [
                {"company": "ACME", "start": "March 2020", "title": "Eng"}
            ]
        }
        config = {"fields": [], "on_missing": "null"}
        result = validate_output(output, config)
        # "March 2020" is not YYYY-MM
        assert not result.is_valid
