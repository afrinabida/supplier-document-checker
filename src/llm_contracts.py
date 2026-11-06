"""Untrusted provider transport and safe extraction result contracts."""
from enum import Enum
from typing import Literal, Protocol
from pydantic import Field
from src.ingestion_models import Completeness, ContractModel
from src.semantic_models import SemanticDocument
from src.local_validation import LocalValidationDiagnostic


class ExtractionErrorCode(str,Enum):
    DISABLED='DISABLED'
    CONFIGURATION='CONFIGURATION'
    MISSING_API_KEY='MISSING_API_KEY'
    CONSENT_REQUIRED='CONSENT_REQUIRED'
    UNSUPPORTED_PROVIDER='UNSUPPORTED_PROVIDER'
    INCOMPLETE_INPUT='INCOMPLETE_INPUT'
    INPUT_LIMIT='INPUT_LIMIT'
    OUTPUT_LIMIT='OUTPUT_LIMIT'
    TIMEOUT='TIMEOUT'
    QUOTA_EXCEEDED='QUOTA_EXCEEDED'
    NETWORK='NETWORK'
    AUTHENTICATION='AUTHENTICATION'
    MODEL_UNAVAILABLE='MODEL_UNAVAILABLE'
    INVALID_REQUEST='INVALID_REQUEST'
    INVALID_RESPONSE='INVALID_RESPONSE'
    RESPONSE_BLOCKED='RESPONSE_BLOCKED'
    REFERENCE_INVALID='REFERENCE_INVALID'
    TRUST_VIOLATION='TRUST_VIOLATION'
    SDK_UNAVAILABLE='SDK_UNAVAILABLE'
    LIVE_CALL_BLOCKED='LIVE_CALL_BLOCKED'
    PROVIDER_FAILURE='PROVIDER_FAILURE'


class ProviderDiagnostic(ContractModel):
    """Allowlisted diagnostics, never raw provider messages/HTTP bodies."""
    http_status: int | None = Field(default=None,ge=100,le=599)
    provider_status: Literal['INVALID_ARGUMENT','UNAUTHENTICATED','PERMISSION_DENIED',
        'NOT_FOUND','RESOURCE_EXHAUSTED','DEADLINE_EXCEEDED','UNAVAILABLE','INTERNAL','UNKNOWN'] = 'UNKNOWN'
    category: Literal['AUTHENTICATION','AUTHORIZATION','MODEL_UNAVAILABLE','SCHEMA_COMPLEXITY','RESPONSE_SCHEMA',
        'INVALID_PARAMETER','INVALID_REQUEST','SDK_SERIALIZATION','TIMEOUT','NETWORK',
        'QUOTA_EXCEEDED','PROVIDER_FAILURE','PROVIDER_UNAVAILABLE']
    exception_type: Literal['APIError','ClientError','ServerError','ValueError','TypeError',
        'ValidationError','Timeout','TransportError','Other'] = 'Other'
    parameter: Literal['response_json_schema','response_mime_type','max_output_tokens',
        'temperature','thinking_config','model','api_key','tools','tool_config',
        'automatic_function_calling','contents','system_instruction','unknown'] = 'unknown'
    detail: Literal['API_CREDENTIAL_REJECTED','ACCESS_DENIED','MODEL_NOT_FOUND_OR_UNAVAILABLE',
        'STRUCTURED_SCHEMA_STATE_OR_DEPTH_LIMIT','STRUCTURED_SCHEMA_REJECTED',
        'GENERATION_PARAMETER_REJECTED','REQUEST_REJECTED_DETAILS_WITHHELD',
        'LOCAL_REQUEST_SERIALIZATION_FAILED','REQUEST_TIMEOUT','TRANSPORT_UNAVAILABLE',
        'QUOTA_OR_RATE_LIMIT','PROVIDER_ERROR','PROVIDER_FIELD_REJECTED','PROVIDER_REASON_REPORTED',
        'PROVIDER_GENERIC_INVALID_ARGUMENT','PROVIDER_MESSAGE_NORMALIZED']
    schema_features: tuple[Literal['$defs','$ref','anyOf','oneOf','prefixItems','pattern',
        'default','additionalProperties','const'],...] = ()
    provider_reason: str | None = Field(default=None,max_length=80)
    rejected_fields: tuple[str,...] = ()
    safe_message: str | None = Field(default=None,max_length=1024)
    timeout_origin: Literal['PROVIDER_HTTP','LOCAL_CLIENT'] | None = None


class ExtractionError(ContractModel):
    code: ExtractionErrorCode
    message: str
    retryable: bool = False
    diagnostic: ProviderDiagnostic | None = None
    validation_diagnostic: LocalValidationDiagnostic | None = None


class UsageMetadata(ContractModel):
    input_tokens: int | None = Field(default=None,ge=0)
    output_tokens: int | None = Field(default=None,ge=0)
    total_tokens: int | None = Field(default=None,ge=0)


class ProviderReply(ContractModel):
    text: str = Field(repr=False,exclude=True)
    usage: UsageMetadata | None = None


class ProviderFailure(Exception):
    def __init__(self,code: ExtractionErrorCode,*,retryable=False,retry_after=None,diagnostic=None,validation_diagnostic=None):
        super().__init__(code.value)
        self.code=code
        self.retryable=retryable
        self.retry_after=retry_after
        self.diagnostic=diagnostic
        self.validation_diagnostic=validation_diagnostic


class ExtractionProvider(Protocol):
    name: str
    is_remote: bool
    def generate(self,*,system_prompt: str,context: str,schema: dict,config) -> ProviderReply: ...


class StructuredExtractionResult(ContractModel):
    status: Literal['SCHEMA_VALIDATED','FAILED','DISABLED','INCOMPLETE']
    provider: Literal['gemini'] = 'gemini'
    model_identifier: str
    prompt_version: str
    schema_version: str
    document_id: str | None
    content_sha256: str | None
    ingestion_completeness: Completeness
    input_complete: bool
    raw_claims_json: str | None = Field(default=None,repr=False,exclude=True)
    validated_claims: SemanticDocument | None = Field(default=None,repr=False,exclude=True)
    evidence_stage: Literal['UNVERIFIED_LLM_CLAIMS'] = 'UNVERIFIED_LLM_CLAIMS'
    warnings: tuple[str,...] = ()
    errors: tuple[ExtractionError,...] = ()
    usage: UsageMetadata | None = None
    attempts: int = Field(default=0,ge=0)
    duration_seconds: float = Field(ge=0,allow_inf_nan=False)
