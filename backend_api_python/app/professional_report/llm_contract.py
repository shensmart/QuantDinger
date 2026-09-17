"""Strict schema boundary for the explanatory LLM response."""

from __future__ import annotations

import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .narrative import normalize_report_text


_EVIDENCE_ID_IN_TEXT = re.compile(r"(?<![A-Za-z0-9])ev_[A-Za-z0-9]+")
_MISLABELED_TECHNICAL_INCONSISTENCY_ZH = re.compile(
    r"(技术面(?:内部)?不一致|技术指标(?:内部)?不一致)"
)
_MISLABELED_TECHNICAL_INCONSISTENCY_EN = re.compile(
    r"(?:technical(?:\s+analysis)?\s+(?:is\s+)?internally\s+inconsistent"
    r"|technical\s+indicators?\s+(?:are\s+)?internally\s+inconsistent)",
    re.IGNORECASE,
)


def _clean_visible_narrative(value: Any) -> tuple[Any, bool]:
    """Remove audit-only evidence IDs and correct a common mixed-signal mislabel."""
    if isinstance(value, str):
        cleaned = _MISLABELED_TECHNICAL_INCONSISTENCY_ZH.sub("技术面信号混合", value)
        cleaned = _MISLABELED_TECHNICAL_INCONSISTENCY_EN.sub("mixed technical picture", cleaned)
        cleaned = _EVIDENCE_ID_IN_TEXT.sub("", cleaned)
        cleaned = re.sub(r"\(\s*[、,，;；:：\s]*\)", "", cleaned)
        cleaned = re.sub(r"[\s、,，;；:：]+([)）])", r"\1", cleaned)
        cleaned = re.sub(r"[\s、,，;；:：]+([。.!?！？])", r"\1", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([,，。；;.!?])", r"\1", cleaned).strip()
        return cleaned, cleaned != value
    if isinstance(value, list):
        changed = False
        cleaned_items = []
        for item in value:
            cleaned, item_changed = _clean_visible_narrative(item)
            cleaned_items.append(cleaned)
            changed = changed or item_changed
        return cleaned_items, changed
    if isinstance(value, dict):
        changed = False
        cleaned_items = {}
        for key, item in value.items():
            cleaned, item_changed = _clean_visible_narrative(item)
            cleaned_items[key] = cleaned
            changed = changed or item_changed
        return cleaned_items, changed
    return value, False


class AnalysisSections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    technical: str = ""
    fundamental: str = ""
    sentiment: str = ""


class NarrativeClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["thesis", "risk", "catalyst", "counter_argument"] = "thesis"
    text: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class FastAnalysisNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["BUY", "SELL", "HOLD"] = "HOLD"
    confidence: int = Field(default=50, ge=0, le=100)
    summary: str = ""
    analysis: AnalysisSections = Field(default_factory=AnalysisSections)
    entry_price: float | None = Field(default=None, ge=0)
    stop_loss: float | None = Field(default=None, ge=0)
    take_profit: float | None = Field(default=None, ge=0)
    position_size_pct: int = Field(default=0, ge=0, le=100)
    timeframe: Literal["short", "medium", "long"] = "medium"
    key_reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence_claims: list[NarrativeClaim] = Field(default_factory=list)
    technical_score: int = Field(default=50, ge=0, le=100)
    fundamental_score: int = Field(default=50, ge=0, le=100)
    sentiment_score: int = Field(default=50, ge=0, le=100)

    @field_validator("decision", mode="before")
    @classmethod
    def normalise_decision(cls, value: Any) -> str:
        return str(value or "HOLD").upper()


_ALLOWED_FIELDS = set(FastAnalysisNarrative.model_fields)


def validate_llm_analysis(
    payload: Mapping[str, Any] | None,
    fallback: Mapping[str, Any],
    *,
    known_evidence_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Validate/coerce the model payload and expose every repair made.

    Unknown top-level fields are removed before strict model validation but
    recorded.  If validation still fails, the deterministic fallback is used;
    a malformed model response never masquerades as a successful analysis.
    """
    source = dict(payload or {})
    warnings = [f"unknown_llm_field:{key}" for key in source if key not in _ALLOWED_FIELDS]
    clean = {key: value for key, value in source.items() if key in _ALLOWED_FIELDS}
    try:
        model = FastAnalysisNarrative.model_validate(clean)
        valid = True
    except ValidationError as exc:
        warnings.extend(
            "invalid_llm_field:" + ".".join(str(part) for part in error.get("loc") or ())
            for error in exc.errors()
        )
        fallback_clean = {key: value for key, value in dict(fallback).items() if key in _ALLOWED_FIELDS}
        model = FastAnalysisNarrative.model_validate(fallback_clean)
        valid = False

    result = model.model_dump(mode="json")
    for key in ("summary", "analysis", "key_reasons", "risks"):
        cleaned, changed = _clean_visible_narrative(result.get(key))
        result[key] = cleaned
        if changed:
            warnings.append(f"visible_evidence_metadata_cleaned:{key}")
    cleaned_claims = []
    for index, claim in enumerate(result["evidence_claims"]):
        claim_text, changed = _clean_visible_narrative(claim.get("text"))
        if changed:
            warnings.append(f"visible_evidence_metadata_cleaned:evidence_claims.{index}.text")
        cleaned_claims.append({**claim, "text": claim_text})
    result["evidence_claims"] = cleaned_claims
    known = known_evidence_ids or set()
    if known:
        filtered_claims = []
        for index, claim in enumerate(result["evidence_claims"]):
            refs = [ref for ref in claim["evidence_refs"] if ref in known]
            if len(refs) != len(claim["evidence_refs"]):
                warnings.append(f"claim_{index}_unknown_evidence_removed")
            if not refs:
                warnings.append(f"claim_{index}_dropped_without_evidence")
                continue
            filtered_claims.append({**claim, "evidence_refs": refs})
        result["evidence_claims"] = filtered_claims
    result["_llm_contract"] = {
        "valid": valid,
        "schema_version": "fast_analysis_narrative_v1",
        "warnings": sorted(set(warnings)),
    }
    return normalize_report_text(result)


__all__ = ["FastAnalysisNarrative", "validate_llm_analysis"]
