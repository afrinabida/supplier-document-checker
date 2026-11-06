"""Safe checkpoint and trusted-caller human review contracts for the AI graph."""
from typing import Literal, TypedDict
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AIGraphError(ValueError):
    """Sanitized operational/review error code, never original exception text."""


class TrustedReviewer(BaseModel):
    """Caller assertion, NOT an authenticated principal or authorization system."""
    model_config=ConfigDict(extra='forbid',frozen=True,strict=True,revalidate_instances='always')
    subject: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_.@-]+$')
    authorized: bool


class AIReviewSubmission(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True,strict=True,revalidate_instances='always')
    thread_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,96}$')
    workflow_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    review_token: str = Field(pattern=r'^[0-9a-f]{32}$')
    submission_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_.-]+$')
    action: Literal['approve','reject','request_correction']
    rationale: str = Field(min_length=1,max_length=5000,repr=False,exclude=True)

    @field_validator('rationale')
    @classmethod
    def nonblank(cls,value):
        if not value.strip(): raise ValueError('A documented rationale is required.')
        return value


class AIState(TypedDict, total=False):
    workflow_id: str
    thread_id: str
    document_id: str
    document_sha256: str
    input_filename: str
    purchase_request_sha256: str
    processing_mode: Literal['legacy','gemini']
    model_id: str
    prompt_version: str
    schema_version: str
    provider_contract: str
    current_stage: str
    ingestion_completeness: str
    extraction_status: str
    evidence_summary: dict
    trusted_facts_status: str
    matching_summary: dict
    rule_findings: list[dict]
    automated_decision: str | None
    review_required: bool
    review_reasons: list[str]
    human_review_status: str
    human_review: dict | None
    final_disposition: str | None
    audit_events: list[dict]
    safe_error: dict | None
    artifacts: dict[str,str]
    review_token: str
    checkpoint_version: str
    implementation_sha256: str
    configuration_sha256: str
    policy_sha256: str
    cancelled: bool
