"""Evidence-backed facts and automated service results, separate from overrides."""
from typing import Literal
from pydantic import Field
from src.ingestion_models import ContractModel, Completeness, SourceReference
from src.verification_models import VerifiedValue, VerificationOutcome, ConflictRecord
from src.models import RuleEvaluation, SupplierDecision


class FactEvidence(ContractModel):
    source: SourceReference
    quote: str = Field(repr=False, exclude=True)


class TrustedFact(ContractModel):
    path: str
    raw_values: tuple[str, ...] = Field(default=(), repr=False, exclude=True)
    normalized: VerifiedValue | None = Field(default=None, repr=False, exclude=True)
    status: VerificationOutcome
    raw_value_status: VerificationOutcome
    normalization_status: VerificationOutcome
    authoritative: bool
    evidence: tuple[FactEvidence, ...] = ()
    reason_codes: tuple[str, ...] = ()
    uncertainty: bool
    policy_interpretation_required: bool
    normalizer_version: Literal['evidence-v1.0'] = 'evidence-v1.0'


class FactGroup(ContractModel):
    """Named typed facts preserve every field, including unverified observations."""
    facts: tuple[TrustedFact, ...] = ()


class TrustedRegistration(FactGroup):
    kind: Literal['TIN', 'BIN', 'OTHER']


class TrustedSupplier(FactGroup):
    registrations: tuple[TrustedRegistration, ...] = ()


class TrustedLineItem(FactGroup):
    index: int
    # Provider item IDs are private; never use them as a source identifier.
    item_id: str = Field(repr=False, exclude=True)
    tax: FactGroup


class TrustedFactsResult(ContractModel):
    document_id: str
    content_sha256: str
    completeness: Completeness
    extraction_complete: bool
    supplier: TrustedSupplier
    line_items: tuple[TrustedLineItem, ...]
    payments: tuple[FactGroup, ...]
    penalties: tuple[FactGroup, ...]
    delivery: FactGroup
    validity: FactGroup
    tax: FactGroup
    amounts: FactGroup
    facts: tuple[TrustedFact, ...]
    unresolved_fields: tuple[str, ...]
    conflicts: tuple[ConflictRecord, ...]
    verification_fingerprint: str
    normalization_provenance: Literal['independent-evidence-v1.0-unrounded'] = 'independent-evidence-v1.0-unrounded'


class ItemMatch(ContractModel):
    supplier_index: int
    status: Literal['MATCHED', 'UNMATCHED', 'AMBIGUOUS', 'DUPLICATE', 'INCOMPATIBLE']
    pr_index: int | None = None
    reason_codes: tuple[str, ...]


class MatchingResult(ContractModel):
    items: tuple[ItemMatch, ...]
    missing_pr_indices: tuple[int, ...]
    inventory_complete: bool


class SupplierCheckResult(ContractModel):
    mode: Literal['llm'] = 'llm'
    decision: Literal['Approve', 'Reject', 'Needs a person to check']
    processing_status: Literal['COMPLETE', 'REVIEW_REQUIRED', 'EXTRACTION_FAILED', 'INPUT_FAILED', 'TRUST_FAILED']
    provider: str
    model_identifier: str
    document_id: str | None = None
    error_codes: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    trusted_facts: TrustedFactsResult | None = Field(default=None, repr=False, exclude=True)
    matching: MatchingResult | None = None
    # Rule values/quotes require explicit access, never incidental serialization.
    automated_assessment: SupplierDecision | None = Field(default=None, repr=False, exclude=True)
    item_evaluations: tuple[RuleEvaluation, ...] = Field(default=(), repr=False, exclude=True)
    findings: tuple[dict[str, str], ...] = ()
    audit: dict[str, str] = Field(default_factory=dict)
