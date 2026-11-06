"""Independent evidence audit contracts; never procurement decisions."""
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from src.ingestion_models import Completeness, ContractModel, SourceReference


class VerificationOutcome(str,Enum):
    VERIFIED='VERIFIED'
    UNVERIFIED='UNVERIFIED'
    UNSUPPORTED='UNSUPPORTED'
    AMBIGUOUS='AMBIGUOUS'
    CONFLICTING='CONFLICTING'
    NOT_CHECKABLE='NOT_CHECKABLE'


class VerificationConfig(ContractModel):
    required_fields: tuple[str,...] = ('supplier.name','line_items[*].description',
        'line_items[*].quantity','line_items[*].unit','line_items[*].unit_price','line_items[*].currency')
    require_source_bytes: bool = False
    trust_completed_ocr: bool = False
    max_fields: int = Field(default=3000,gt=0)
    max_quote_characters: int = Field(default=20_000,gt=0)
    max_quote_occurrences: int = Field(default=50,gt=0)
    money_decimal_places: int | None = Field(default=None,ge=0,le=8)
    rounding_mode: Literal['ROUND_HALF_UP','ROUND_HALF_EVEN'] | None = None

    @model_validator(mode='after')
    def rounding_is_explicit(self):
        if (self.money_decimal_places is None)!=(self.rounding_mode is None):
            raise ValueError('Explicit rounding requires both precision and mode')
        return self


class VerifiedValue(ContractModel):
    kind: Literal['DECIMAL','TEXT','DATE','DURATION','IDENTIFIER','POLARITY','TAX_BASIS']
    decimal_value: Decimal | None = Field(default=None,allow_inf_nan=False)
    text_value: str | None = None
    unit: str | None = None
    currency: str | None = None
    anchor: str | None = None


class ResolvedSpan(ContractModel):
    source: SourceReference = Field(repr=False,exclude=True)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    source_text: str = Field(repr=False,exclude=True)
    match_method: Literal['EXACT','WHITESPACE_PUNCTUATION']
    offset_basis: Literal['RESOLVED_SOURCE_TEXT'] = 'RESOLVED_SOURCE_TEXT'


class EvidenceCheck(ContractModel):
    source: SourceReference = Field(repr=False,exclude=True)
    claimed_quote: str = Field(repr=False,exclude=True)
    quote_outcome: VerificationOutcome
    value_outcome: VerificationOutcome
    normalized_outcome: VerificationOutcome
    spans: tuple[ResolvedSpan,...] = ()
    normalized_value: VerifiedValue | None = Field(default=None,repr=False,exclude=True)
    reasons: tuple[str,...] = ()
    policy_interpretation_required: bool = False
    supported_raw_indices: tuple[int,...] = ()
    normalized_raw_indices: tuple[int,...] = ()


class FieldVerification(ContractModel):
    field_path: str
    extraction_state: str
    raw_values: tuple[str,...] = Field(default=(),repr=False,exclude=True)
    proposed_normalization: str | None = Field(default=None,repr=False,exclude=True)
    claimed_normalized_value: str | None = Field(default=None,repr=False,exclude=True)
    outcome: VerificationOutcome
    quote_outcome: VerificationOutcome
    raw_value_outcome: VerificationOutcome
    normalization_outcome: VerificationOutcome
    normalized_value: VerifiedValue | None = Field(default=None,repr=False,exclude=True)
    evidence_checks: tuple[EvidenceCheck,...] = ()
    reason_codes: tuple[str,...] = ()
    explanation: str
    uncertain: bool
    conflicting: bool = False
    requires_human_interpretation: bool = False
    eligible_for_future_use: bool = False


class SourceObservation(ContractModel):
    key: str
    value: str = Field(repr=False,exclude=True)
    reference: SourceReference = Field(repr=False,exclude=True)
    quote: str = Field(repr=False,exclude=True)
    precedence_or_alternative: bool = False
    scope_uncertain: bool = False


class ConflictRecord(ContractModel):
    key: str
    classification: Literal['CONTRADICTION','PRECEDENCE_REVIEW','INCOMPARABLE','REPEATED_EQUIVALENT','COMPATIBLE_DETAIL']
    observations: tuple[SourceObservation,...] = ()
    field_paths: tuple[str,...] = ()
    reason_code: str
    explanation: str
    requires_human_review: bool


class CoverageSummary(ContractModel):
    required_fields: tuple[str,...]
    required_present: tuple[str,...]
    required_verified: tuple[str,...]
    missing_from_output: tuple[str,...]
    present_but_unsupported: tuple[str,...]
    conflicting_fields: tuple[str,...]
    human_interpretation_fields: tuple[str,...]
    pages_not_fully_processed: tuple[int,...]
    ocr_required_pages: tuple[int,...]
    required_fields_ready: bool


class EvidenceVerificationReport(ContractModel):
    verifier_version: Literal['evidence-v1.0'] = 'evidence-v1.0'
    document_id: str | None
    content_sha256: str | None
    identity_outcome: VerificationOutcome
    hash_check: Literal['DECLARED_IDENTITIES_MATCH','RECOMPUTED','UNAVAILABLE','MISMATCH']
    document_completeness: Completeness
    extraction_complete: bool
    configuration: VerificationConfig
    fields: tuple[FieldVerification,...]
    conflicts: tuple[ConflictRecord,...]
    comparisons: tuple[ConflictRecord,...]
    coverage: CoverageSummary
    needs_human_review: bool
    diagnostics: tuple[str,...] = ()
    source_content_in_default_serialization: Literal[False] = False
