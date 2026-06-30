"""
Tests for the merger module.
Tests conflict resolution, deduplication, provenance, and confidence scoring.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from pipeline.merger import merge_candidates, _pick_winner, _union_strings


class TestPickWinner:
    def test_higher_weight_wins(self):
        candidates = [
            {"_source": "csv", "full_name": "Jordan Lee"},
            {"_source": "txt", "full_name": "J. Lee"},
        ]
        val, src = _pick_winner(candidates, "full_name")
        assert val == "Jordan Lee"
        assert src == "csv"

    def test_missing_field_ignored(self):
        candidates = [
            {"_source": "csv", "full_name": None},
            {"_source": "txt", "full_name": "Jordan Lee"},
        ]
        val, src = _pick_winner(candidates, "full_name")
        assert val == "Jordan Lee"

    def test_all_missing_returns_default(self):
        candidates = [{"_source": "csv"}]
        val, src = _pick_winner(candidates, "full_name")
        assert val is None
        assert src is None


class TestUnionStrings:
    def test_deduplicates_emails(self):
        candidates = [
            {"_source": "csv", "email": "jordan@email.com"},
            {"_source": "json_ats", "email": "JORDAN@EMAIL.COM"},
        ]
        from pipeline.normalizer import normalize_email
        pairs = _union_strings(candidates, "email", normalize_email)
        emails = [e for e, _ in pairs]
        assert len(emails) == 1
        assert emails[0] == "jordan@email.com"

    def test_collects_from_multiple_sources(self):
        candidates = [
            {"_source": "csv", "email": "a@example.com"},
            {"_source": "json_ats", "email": "b@example.com"},
        ]
        from pipeline.normalizer import normalize_email
        pairs = _union_strings(candidates, "email", normalize_email)
        assert len(pairs) == 2


class TestMergeCandidates:
    def test_basic_merge(self):
        candidates = [
            {
                "_source": "csv",
                "full_name": "Jordan Lee",
                "email": "jordan.lee@email.com",
                "phone": "+14155550192",
                "company": "Stripe",
                "title": "Senior SWE",
                "location": "San Francisco, CA, US",
                "skills": "Python, Go",
                "github": "https://github.com/jordanlee",
            }
        ]
        profile = merge_candidates(candidates)
        assert profile.full_name == "Jordan Lee"
        assert "jordan.lee@email.com" in profile.emails
        assert "+14155550192" in profile.phones
        assert len(profile.skills) >= 2
        assert len(profile.provenance) > 0

    def test_email_dedup_across_sources(self):
        candidates = [
            {"_source": "csv", "email": "jordan@email.com", "full_name": "Jordan Lee"},
            {"_source": "json_ats", "email": "JORDAN@EMAIL.COM", "full_name": "Jordan Lee"},
        ]
        profile = merge_candidates(candidates)
        # Should have only 1 unique email
        assert len(profile.emails) == 1

    def test_name_conflict_resolved_by_weight(self):
        candidates = [
            {"_source": "csv", "full_name": "Jordan Lee"},       # weight 0.90
            {"_source": "txt", "full_name": "Jordan M. Lee"},    # weight 0.60
        ]
        profile = merge_candidates(candidates)
        # CSV (higher weight) should win
        assert profile.full_name == "Jordan Lee"

    def test_overall_confidence_populated(self):
        candidates = [
            {"_source": "csv", "full_name": "Test User", "email": "t@t.com"}
        ]
        profile = merge_candidates(candidates)
        assert 0.0 < profile.overall_confidence <= 1.0

    def test_empty_candidates_returns_valid_profile(self):
        profile = merge_candidates([])
        assert profile.candidate_id is not None
        assert profile.full_name is None

    def test_skills_boosted_when_from_multiple_sources(self):
        candidates = [
            {"_source": "csv", "full_name": "A", "skills": "Python"},
            {"_source": "json_ats", "full_name": "A", "skills": "Python"},
        ]
        profile = merge_candidates(candidates)
        python_skill = next((s for s in profile.skills if "python" in s.name.lower()), None)
        assert python_skill is not None
        # Should have both sources listed
        assert len(python_skill.sources) >= 2

    def test_provenance_recorded(self):
        candidates = [
            {"_source": "csv", "full_name": "Jordan Lee", "email": "j@j.com"}
        ]
        profile = merge_candidates(candidates)
        fields_tracked = {p.field for p in profile.provenance}
        assert "full_name" in fields_tracked
        assert "emails" in fields_tracked

    def test_malformed_phone_excluded(self):
        candidates = [
            {"_source": "csv", "full_name": "Test", "phone": "not-a-phone"}
        ]
        profile = merge_candidates(candidates)
        # Invalid phone should be excluded (normalize_phone returns None)
        assert len(profile.phones) == 0
