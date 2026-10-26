from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BugReport(StrictModel):
    text: str = Field(min_length=3, max_length=6000)
    mode: Literal["demo", "connected"] = "demo"
    sample: Literal["checkout", "insufficient", "no_session", "non_bug"] = "checkout"
    user_id: str = Field(default="", max_length=200)
    start: str = ""
    end: str = ""
    repository: str = Field(default=".", max_length=250)
    diagnostics: str = Field(default="", max_length=20000)

    @model_validator(mode="after")
    def validate_dates(self):
        dates = []
        for value in (self.start, self.end):
            if value:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("Time windows must include a timezone.")
                dates.append(parsed)
        if bool(self.start) != bool(self.end):
            raise ValueError("Provide both start and end, or leave both blank.")
        if len(dates) == 2 and dates[0] >= dates[1]:
            raise ValueError("The end must be after the start.")
        if not self.text.strip():
            raise ValueError("A bug description is required.")
        return self


class Evidence(StrictModel):
    id: str
    source: Literal["repository", "posthog", "sentry", "diagnostics"]
    origin: Literal["synthetic", "retrieved", "user_supplied"]
    title: str
    content: str
    timestamp: str = ""
    location: str = ""


class Citation(StrictModel):
    evidence_id: str
    quote: str = Field(min_length=8, max_length=1500)


class Diagnosis(StrictModel):
    outcome: Literal["DIAGNOSED", "NO_SESSION", "NO_DIAGNOSIS", "SKIPPED"]
    root_cause: str = Field(max_length=2500)
    summary: str = Field(max_length=3000)
    confidence: float = Field(ge=0, le=1)
    citations: list[Citation] = Field(default_factory=list, max_length=20)
    missing: list[str] = Field(default_factory=list, max_length=20)
    next_steps: list[str] = Field(default_factory=list, max_length=20)
    basis: Literal["model_inference", "deterministic_fixture", "abstention"] = "model_inference"


def abstain(reason, outcome="NO_DIAGNOSIS", missing=None):
    return Diagnosis(outcome=outcome, root_cause="A reliable cause could not be established.",
                     summary=reason, confidence=0, missing=missing or [reason],
                     next_steps=["Collect a reproducible example, the failing request, and its matching stack trace."],
                     basis="abstention")


def validate_diagnosis(candidate, evidence, threshold):
    known = {item.id: item for item in evidence}
    if len(known) != len(evidence):
        return abstain("The evidence set contained ambiguous duplicate IDs.")
    for citation in candidate.citations:
        item = known.get(citation.evidence_id)
        if item is None or not citation.quote.strip() or citation.quote not in item.content:
            return abstain("The proposed diagnosis contained an unknown evidence ID or an unsupported quote.")
    if candidate.outcome == "DIAGNOSED":
        if not candidate.root_cause.strip() or not candidate.summary.strip():
            return abstain("The analysis did not state a supported causal inference.")
        cited = {c.evidence_id for c in candidate.citations}
        sources = {known[key].source for key in cited}
        if candidate.confidence < threshold or len(cited) < 2 or "repository" not in sources or not sources.intersection({"sentry", "diagnostics"}):
            return abstain("The evidence did not meet the diagnosis threshold: matching source code and failure evidence are required.")
    else:
        candidate.root_cause = "A reliable cause could not be established."
        candidate.confidence = 0
    return candidate


def observed_excerpts(diagnosis: Diagnosis, evidence: list[Evidence]) -> list[dict[str, str]]:
    """Build observations from collected text, never from model-written factual claims.

    Exact quotations prove what a source contains, not whether an AI inference is true.
    Keep the source's provenance (including unverified user input) attached.
    """
    known = {item.id: item for item in evidence}
    observations: list[dict[str, str]] = []
    seen = set()
    for citation in diagnosis.citations:
        item = known.get(citation.evidence_id)
        if item is None or not citation.quote.strip() or citation.quote not in item.content:
            raise ValueError("Cannot display an unsupported observation.")
        key = (item.id, citation.quote)
        if key in seen:
            continue
        seen.add(key)
        start = item.content.index(citation.quote)
        observations.append({"evidence_id": item.id, "quote": item.content[start:start + len(citation.quote)],
                             "source": item.source, "origin": item.origin,
                             "location": item.location, "timestamp": item.timestamp})
    return observations
