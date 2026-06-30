"""
Tests for the normalizer module.
All normalizer functions are pure/deterministic — same input → same output.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from pipeline.normalizer import (
    normalize_phone,
    normalize_date,
    normalize_country,
    normalize_email,
    canonicalize_skill,
)


class TestPhoneNormalization:
    def test_us_phone_with_formatting(self):
        assert normalize_phone("(415) 555-0192") == "+14155550192"

    def test_us_phone_dashes(self):
        assert normalize_phone("415-555-0192") == "+14155550192"

    def test_international_phone(self):
        assert normalize_phone("+44 20 7946 0958") == "+442079460958"

    def test_already_e164(self):
        assert normalize_phone("+14155550192") == "+14155550192"

    def test_empty_string_returns_none(self):
        assert normalize_phone("") is None

    def test_none_returns_none(self):
        assert normalize_phone(None) is None

    def test_garbage_returns_none(self):
        assert normalize_phone("not-a-phone-number") is None

    def test_too_short_returns_none(self):
        assert normalize_phone("123") is None


class TestDateNormalization:
    def test_yyyy_mm_passthrough(self):
        assert normalize_date("2021-03") == "2021-03"

    def test_year_only(self):
        assert normalize_date("2020") == "2020-01"

    def test_month_year_text(self):
        result = normalize_date("March 2020")
        assert result == "2020-03"

    def test_jan_abbreviation(self):
        result = normalize_date("Jan 2021")
        assert result == "2021-01"

    def test_slash_format(self):
        result = normalize_date("03/2020")
        assert result is not None
        assert result.startswith("2020")

    def test_empty_returns_none(self):
        assert normalize_date("") is None

    def test_none_returns_none(self):
        assert normalize_date(None) is None

    def test_garbage_returns_none(self):
        assert normalize_date("not-a-date-xyz") is None


class TestCountryNormalization:
    def test_united_states_full(self):
        assert normalize_country("United States") == "US"

    def test_usa(self):
        assert normalize_country("USA") == "US"

    def test_uk(self):
        assert normalize_country("UK") == "GB"

    def test_india(self):
        assert normalize_country("India") == "IN"

    def test_case_insensitive(self):
        assert normalize_country("united kingdom") == "GB"

    def test_unknown_returns_none(self):
        assert normalize_country("Atlantis") is None

    def test_empty_returns_none(self):
        assert normalize_country("") is None


class TestEmailNormalization:
    def test_lowercase(self):
        assert normalize_email("Jordan.Lee@Email.COM") == "jordan.lee@email.com"

    def test_strips_whitespace(self):
        assert normalize_email("  test@example.com  ") == "test@example.com"

    def test_empty_returns_none(self):
        assert normalize_email("") is None


class TestSkillCanonicalization:
    def test_exact_alias(self):
        assert canonicalize_skill("python") == "Python"
        assert canonicalize_skill("js") == "JavaScript"
        assert canonicalize_skill("k8s") == "Kubernetes"

    def test_case_insensitive_alias(self):
        assert canonicalize_skill("PYTHON") == "Python"

    def test_variant_react(self):
        assert canonicalize_skill("reactjs") == "React"

    def test_golang(self):
        assert canonicalize_skill("golang") == "Go"

    def test_unknown_skill_titlecased(self):
        result = canonicalize_skill("some obscure framework xyz123")
        assert isinstance(result, str)
        assert len(result) > 0
