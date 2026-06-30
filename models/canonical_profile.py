"""
Canonical Candidate Profile — Pydantic v2 data models.
This is the internal representation before any projection/config is applied.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class LocationModel(BaseModel):
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None  # ISO-3166 alpha-2


class LinksModel(BaseModel):
    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None
    other: list[str] = Field(default_factory=list)


class SkillModel(BaseModel):
    name: str
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[str] = Field(default_factory=list)


class ExperienceModel(BaseModel):
    company: Optional[str] = None
    title: Optional[str] = None
    start: Optional[str] = None   # YYYY-MM
    end: Optional[str] = None     # YYYY-MM
    summary: Optional[str] = None


class EducationModel(BaseModel):
    institution: Optional[str] = None
    degree: Optional[str] = None
    field: Optional[str] = None
    end_year: Optional[int] = None


class ProvenanceModel(BaseModel):
    field: str
    source: str
    method: str


class CanonicalProfile(BaseModel):
    candidate_id: str
    full_name: Optional[str] = None
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)   # E.164
    location: LocationModel = Field(default_factory=LocationModel)
    links: LinksModel = Field(default_factory=LinksModel)
    headline: Optional[str] = None
    years_experience: Optional[float] = None
    skills: list[SkillModel] = Field(default_factory=list)
    experience: list[ExperienceModel] = Field(default_factory=list)
    education: list[EducationModel] = Field(default_factory=list)
    provenance: list[ProvenanceModel] = Field(default_factory=list)
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    def add_provenance(self, field: str, source: str, method: str) -> None:
        """Record where a field value came from."""
        self.provenance.append(ProvenanceModel(field=field, source=source, method=method))
