"""
Merger — deduplicates and merges multiple raw candidate dicts into one CanonicalProfile.

Design decisions (aligned with design doc):
  1. Merge key: normalized email (primary) or name+location composite hash (fallback)
  2. Source priority tier: ATS JSON (1) > Recruiter CSV (2) > PDF (3) > Notes/TXT (4)
  3. List fields (emails, phones, skills) are unioned and deduplicated
  4. Experience entries are merged by company+title similarity (fuzzy, Jaro-Winkler via WRatio)
  5. All accepted values are recorded in provenance
  6. Confidence formula: Score = [Σ(Wf × Cf) / ΣW] - Pmissing
     where Wf=field priority, Cf=source trust, Pmissing=penalty for missing essential contact keys
"""
from __future__ import annotations
import hashlib
import logging
import re
from typing import Any, Optional

from rapidfuzz import fuzz

from models.canonical_profile import (
    CanonicalProfile,
    SkillModel,
    ExperienceModel,
    EducationModel,
    LocationModel,
    LinksModel,
)
from pipeline.normalizer import (
    normalize_email,
    normalize_phone,
    normalize_date,
    normalize_country,
    canonicalize_skill,
)

logger = logging.getLogger(__name__)

# ─── Source reliability weights (tier order per design doc) ───────────────────
# Tier 1 (highest): ATS JSON  → most structured, system-of-record
# Tier 2          : Recruiter CSV → well-structured but manual entry
# Tier 3          : Resume PDF   → self-reported, rich but prose
# Tier 4 (lowest) : Recruiter Notes → free-text, most subjective
SOURCE_WEIGHTS: dict[str, float] = {
    "json_ats": 0.95,    # Tier 1 — ATS is authoritative system
    "csv": 0.85,         # Tier 2 — Recruiter CSV
    "pdf": 0.75,         # Tier 3 — Resume PDF
    "github_url": 0.70,  # Tier 3b — GitHub (public, structured)
    "linkedin_url": 0.65,# Tier 3c — LinkedIn
    "txt": 0.55,         # Tier 4 — Recruiter Notes (free text)
    "unknown": 0.35,
}

# Missing essential contact key penalty (applied to overall_confidence)
_MISSING_CONTACT_PENALTY = 0.10

# Jaro-Winkler similarity threshold for blocking auto-merge on shared contacts
_JARO_WINKLER_BLOCK_THRESHOLD = 65  # 0–100 scale via rapidfuzz WRatio


def _source_weight(source_type: str) -> float:
    return SOURCE_WEIGHTS.get(source_type, 0.35)


def _candidate_id(candidates: list[dict]) -> str:
    """
    Generate a stable candidate ID.
    Primary key  : lowest sorted normalized email.
    Fallback key : composite hash of name + location (per design doc).
    """
    emails = set()
    names = []
    locations = []
    for c in candidates:
        if c.get("email"):
            norm = normalize_email(str(c["email"]))
            if norm:
                emails.add(norm)
        if c.get("full_name"):
            names.append(str(c["full_name"]).strip().lower())
        if c.get("location"):
            locations.append(str(c["location"]).strip().lower())

    if emails:
        key = sorted(emails)[0]
    elif names:
        # Composite hash: name + location (design doc fallback)
        name_part = names[0]
        loc_part = locations[0] if locations else ""
        key = f"{name_part}|{loc_part}"
    else:
        key = "unknown"

    return "cand_" + hashlib.md5(key.encode()).hexdigest()[:12]


def _should_block_merge(name_a: str, name_b: str) -> bool:
    """
    Block auto-merge if candidate names are too dissimilar (Jaro-Winkler via WRatio).
    Prevents merging two different people sharing a corporate email/phone.
    Per design doc: blocks merge if similarity < 0.65 (65 on 0-100 scale).
    """
    from rapidfuzz import fuzz as _fuzz
    similarity = _fuzz.WRatio(name_a.strip().lower(), name_b.strip().lower())
    return similarity < _JARO_WINKLER_BLOCK_THRESHOLD


def _pick_winner(
    candidates: list[dict], field: str, default: Any = None
) -> tuple[Any, Optional[str]]:
    """
    Pick the best value for a scalar field across candidates.
    Returns (value, source_type) of the winning candidate.
    Winner = highest source weight; ties broken by value length (prefer more complete).
    """
    best_val = default
    best_weight = -1.0
    best_source = None

    for c in candidates:
        val = c.get(field)
        if val is None or val == "":
            continue
        weight = _source_weight(c.get("_source", "unknown"))
        val_len = len(str(val))
        if weight > best_weight or (weight == best_weight and val_len > len(str(best_val or ""))):
            best_val = val
            best_weight = weight
            best_source = c.get("_source", "unknown")

    return best_val, best_source


def _union_strings(
    candidates: list[dict], field: str, normalize_fn=None
) -> list[tuple[str, str]]:
    """
    Collect all unique values for a list field across candidates.
    Returns list of (normalized_value, source_type) tuples.
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for c in candidates:
        raw_val = c.get(field)
        if not raw_val:
            continue
        items = raw_val if isinstance(raw_val, list) else [raw_val]
        for item in items:
            if not item:
                continue
            normalized = normalize_fn(str(item)) if normalize_fn else str(item).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append((normalized, c.get("_source", "unknown")))
    return result


def _merge_skills(candidates: list[dict]) -> list[SkillModel]:
    """
    Union all skills across candidates.
    If a skill appears in multiple sources → higher confidence.
    """
    skill_map: dict[str, dict] = {}  # canonical_name → {sources, base_confidence}

    for c in candidates:
        raw_skills = c.get("skills")
        if not raw_skills:
            continue
        if isinstance(raw_skills, str):
            raw_skills = [s.strip() for s in raw_skills.split(",")]
        source = c.get("_source", "unknown")
        weight = _source_weight(source)

        for raw in raw_skills:
            if not raw:
                continue
            canonical = canonicalize_skill(str(raw))
            key = canonical.lower()
            if key not in skill_map:
                skill_map[key] = {
                    "name": canonical,
                    "sources": [source],
                    "confidence": weight,
                }
            else:
                # Boost confidence for cross-source confirmation
                if source not in skill_map[key]["sources"]:
                    skill_map[key]["sources"].append(source)
                    skill_map[key]["confidence"] = min(
                        1.0,
                        skill_map[key]["confidence"] + weight * 0.15,
                    )

    return [
        SkillModel(
            name=v["name"],
            confidence=round(v["confidence"], 3),
            sources=v["sources"],
        )
        for v in sorted(skill_map.values(), key=lambda x: -x["confidence"])
    ]


def _merge_experience(candidates: list[dict]) -> list[ExperienceModel]:
    """
    Merge experience lists. Entries with >80% company+title similarity are merged.
    The more complete (longer) entry wins for each field.
    """
    all_entries: list[dict] = []
    for c in candidates:
        exps = c.get("experience", [])
        if not isinstance(exps, list):
            continue
        for exp in exps:
            if isinstance(exp, dict):
                all_entries.append({**exp, "_source": c.get("_source", "unknown")})

    merged: list[dict] = []
    used: set[int] = set()

    for i, entry in enumerate(all_entries):
        if i in used:
            continue
        group = [entry]
        for j, other in enumerate(all_entries):
            if j <= i or j in used:
                continue
            # Fuzzy match on company + title
            company_sim = fuzz.token_set_ratio(
                entry.get("company", ""), other.get("company", "")
            )
            title_sim = fuzz.token_set_ratio(
                entry.get("title", ""), other.get("title", "")
            )
            if company_sim >= 80 and title_sim >= 70:
                group.append(other)
                used.add(j)

        used.add(i)
        # Merge group: pick longest non-null value for each field
        best: dict = {}
        for field in ("company", "title", "start", "end", "summary"):
            candidates_for_field = [
                g.get(field) for g in group if g.get(field)
            ]
            if candidates_for_field:
                best[field] = max(candidates_for_field, key=lambda x: len(str(x)))

        # Normalize dates
        for date_field in ("start", "end"):
            if best.get(date_field):
                normalized = normalize_date(str(best[date_field]))
                best[date_field] = normalized

        merged.append(best)

    return [ExperienceModel(**e) for e in merged]


def _merge_education(candidates: list[dict]) -> list[EducationModel]:
    """Simple education merge — union entries, dedup by institution+degree similarity."""
    all_entries: list[dict] = []
    for c in candidates:
        edus = c.get("education", [])
        if not isinstance(edus, list):
            continue
        for edu in edus:
            if isinstance(edu, dict):
                all_entries.append(edu)

    merged: list[dict] = []
    used: set[int] = set()

    for i, entry in enumerate(all_entries):
        if i in used:
            continue
        group = [entry]
        for j, other in enumerate(all_entries):
            if j <= i or j in used:
                continue
            inst_sim = fuzz.token_set_ratio(
                entry.get("institution", ""), other.get("institution", "")
            )
            deg_sim = fuzz.token_set_ratio(
                entry.get("degree", ""), other.get("degree", "")
            )
            if inst_sim >= 80 or deg_sim >= 80:
                group.append(other)
                used.add(j)
        used.add(i)

        best: dict = {}
        for field in ("institution", "degree", "field"):
            vals = [g.get(field) for g in group if g.get(field)]
            if vals:
                best[field] = max(vals, key=lambda x: len(str(x)))
        years = [g.get("end_year") for g in group if g.get("end_year")]
        if years:
            try:
                best["end_year"] = int(max(years))
            except (ValueError, TypeError):
                pass
        merged.append(best)

    return [EducationModel(**e) for e in merged]


def _parse_location(raw: str) -> LocationModel:
    """
    Parse 'City, Region, Country' or 'City, Country' strings into LocationModel.
    """
    if not raw:
        return LocationModel()
    parts = [p.strip() for p in raw.split(",")]
    loc = LocationModel()
    if len(parts) == 1:
        loc.city = parts[0]
    elif len(parts) == 2:
        loc.city = parts[0]
        # Try as country first
        country = normalize_country(parts[1])
        if country:
            loc.country = country
        else:
            loc.region = parts[1]
    elif len(parts) >= 3:
        loc.city = parts[0]
        loc.region = parts[1]
        loc.country = normalize_country(parts[2]) or parts[2]
    return loc


def merge_candidates(candidates: list[dict]) -> CanonicalProfile:
    """
    Merge a list of raw candidate dicts (from all extractors) into one CanonicalProfile.

    Args:
        candidates: List of raw dicts. Each must have '_source' key.

    Returns:
        CanonicalProfile — the merged, normalized, provenance-tracked profile.
    """
    if not candidates:
        return CanonicalProfile(candidate_id="cand_empty")

    profile = CanonicalProfile(candidate_id=_candidate_id(candidates))

    # ── Scalar fields ──────────────────────────────────────────
    name, name_src = _pick_winner(candidates, "full_name")
    if name:
        profile.full_name = str(name).strip().title()
        profile.add_provenance("full_name", name_src or "unknown", "source_priority")

    headline, hl_src = _pick_winner(candidates, "headline")
    if headline:
        profile.headline = str(headline).strip()[:300]
        profile.add_provenance("headline", hl_src or "unknown", "source_priority")

    yoe, yoe_src = _pick_winner(candidates, "years_experience")
    if yoe is not None:
        try:
            profile.years_experience = float(yoe)
            profile.add_provenance("years_experience", yoe_src or "unknown", "source_priority")
        except (ValueError, TypeError):
            pass

    # ── Email list ─────────────────────────────────────────────
    email_pairs = _union_strings(candidates, "email", normalize_email)
    profile.emails = [e for e, _ in email_pairs]
    if email_pairs:
        sources_used = list({src for _, src in email_pairs})
        profile.add_provenance("emails", ",".join(sources_used), "union_dedup")

    # ── Phone list ─────────────────────────────────────────────
    phone_pairs = _union_strings(candidates, "phone", lambda p: normalize_phone(p))
    profile.phones = [p for p, _ in phone_pairs if p]
    if phone_pairs:
        sources_used = list({src for _, src in phone_pairs})
        profile.add_provenance("phones", ",".join(sources_used), "normalize_e164+union")

    # ── Location ───────────────────────────────────────────────
    loc_val, loc_src = _pick_winner(candidates, "location")
    if loc_val:
        profile.location = _parse_location(str(loc_val))
        profile.add_provenance("location", loc_src or "unknown", "parse_location_string")

    # ── Links ──────────────────────────────────────────────────
    links = LinksModel()
    for link_field in ("linkedin", "github", "portfolio"):
        val, src = _pick_winner(candidates, link_field)
        if val:
            setattr(links, link_field, str(val).strip())
            profile.add_provenance(f"links.{link_field}", src or "unknown", "source_priority")
    profile.links = links

    # ── Skills ─────────────────────────────────────────────────
    profile.skills = _merge_skills(candidates)
    if profile.skills:
        profile.add_provenance("skills", "multiple", "union_canonical_fuzzy")

    # ── Experience ─────────────────────────────────────────────
    profile.experience = _merge_experience(candidates)
    if profile.experience:
        profile.add_provenance("experience", "multiple", "fuzzy_dedup_merge")

    # ── Education ──────────────────────────────────────────────
    profile.education = _merge_education(candidates)
    if profile.education:
        profile.add_provenance("education", "multiple", "fuzzy_dedup_merge")

    # ── Overall confidence ─────────────────────────────────────────────────────
    # Formula (per design doc): Score = [Σ(Wf × Cf) / ΣW] - Pmissing
    # Wf = field priority weight (approximated as 1.0 per field — all equal)
    # Cf = source trust weight of winning source for each field
    # Pmissing = penalty for each missing essential contact key
    sources_present = list({c.get("_source", "unknown") for c in candidates})
    field_weights: list[float] = []

    # Collect Cf for each filled provenance field
    source_trust_map = {entry.field: _source_weight(entry.source)
                        for entry in profile.provenance}
    for trust_val in source_trust_map.values():
        field_weights.append(trust_val)

    if not field_weights:
        # Fallback: simple average of source weights
        weights = [_source_weight(s) for s in sources_present]
        raw_score = sum(weights) / len(weights) if weights else 0.0
    else:
        # Weighted average: Σ(1.0 × Cf) / Σ(1.0)
        raw_score = sum(field_weights) / len(field_weights)

    # Pmissing: deduct penalty for each missing essential contact key
    essential_keys = ["emails", "phones"]
    missing_count = sum(
        1 for k in essential_keys
        if not getattr(profile, k, None)
    )
    penalty = missing_count * _MISSING_CONTACT_PENALTY

    profile.overall_confidence = round(max(0.0, min(1.0, raw_score - penalty)), 3)

    return profile
