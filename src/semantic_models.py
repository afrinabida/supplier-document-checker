"""Future interpretation contract only: no provider, extractor or decision engine."""
from datetime import date
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BeforeValidator, Field, model_validator

from src.ingestion_models import Completeness, ContractModel, SourceReference


def _exact_decimal(value):
    if isinstance(value,(float,bool)):
        raise ValueError('Decimal observations require exact strings or integers, never floats/bools')
    return value


ExactDecimal=Annotated[Decimal,BeforeValidator(_exact_decimal),Field(allow_inf_nan=False)]
T=TypeVar('T')


class SemanticEvidence(ContractModel):
    source: SourceReference
    quote: str = Field(min_length=1)
    verification: Literal['UNCHECKED','QUOTE_LOCATED','SEMANTIC_VERIFIED'] = 'UNCHECKED'
    verifier_id: str | None = None

    @model_validator(mode='after')
    def verification_provenance(self):
        if not self.quote.strip():
            raise ValueError('Semantic evidence requires a nonblank source quote')
        if self.verification=='SEMANTIC_VERIFIED' and (not self.verifier_id or not self.verifier_id.strip()):
            raise ValueError('Semantic verification requires verifier provenance')
        return self


class SemanticField(ContractModel, Generic[T]):
    state: Literal['MISSING','UNVERIFIED','AMBIGUOUS','CONFLICTING','UNSUPPORTED','VERIFIED'] = 'MISSING'
    raw_values: tuple[str,...] = ()
    normalized_value: T | None = None
    normalizer_version: str | None = None
    evidence: tuple[SemanticEvidence,...] = ()
    proposed_normalization: str | None = None
    normalization_assumptions: tuple[str,...] = ()
    ambiguity_reason: str | None = None

    @model_validator(mode='after')
    def field_states(self):
        if self.state=='MISSING' and (self.raw_values or self.normalized_value is not None or self.evidence or self.proposed_normalization is not None):
            raise ValueError('Missing means not extracted; do not invent an absent/default value')
        if self.state not in ('MISSING','UNSUPPORTED') and not self.raw_values:
            raise ValueError('Observed interpretations require raw source values')
        if self.normalized_value is not None and (not self.normalizer_version or not self.normalizer_version.strip()):
            raise ValueError('Normalized values require deterministic normalizer provenance')
        if self.state in ('AMBIGUOUS','CONFLICTING','UNSUPPORTED') and self.normalized_value is not None:
            raise ValueError('Unresolved interpretations cannot select one normalized value')
        if self.state=='VERIFIED' and (not self.evidence or any(e.verification!='SEMANTIC_VERIFIED' for e in self.evidence)):
            raise ValueError('Quote location alone cannot verify a semantic field')
        return self


class RegistrationIdentifier(ContractModel):
    kind: Literal['TIN','BIN','OTHER']
    label: SemanticField[str] = Field(default_factory=SemanticField[str])
    value: SemanticField[str] = Field(default_factory=SemanticField[str])
    owner_role: SemanticField[str] = Field(default_factory=SemanticField[str])


class SupplierIdentity(ContractModel):
    name: SemanticField[str] = Field(default_factory=SemanticField[str])
    address: SemanticField[str] = Field(default_factory=SemanticField[str])
    registrations: tuple[RegistrationIdentifier,...] = ()


class TaxTreatment(ContractModel):
    treatment: SemanticField[Literal['included','separate','exempt','other']] = Field(default_factory=SemanticField)
    rate_percent: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    amount: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)


class SemanticLineItem(ContractModel):
    item_id: str = Field(min_length=1)
    code: SemanticField[str] = Field(default_factory=SemanticField[str])
    description: SemanticField[str] = Field(default_factory=SemanticField[str])
    quantity: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    unit: SemanticField[str] = Field(default_factory=SemanticField[str])
    unit_price: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    currency: SemanticField[str] = Field(default_factory=SemanticField[str])
    line_total: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    tax: TaxTreatment = Field(default_factory=TaxTreatment)


class DeliveryTerms(ContractModel):
    wording: SemanticField[str] = Field(default_factory=SemanticField[str])
    calendar_date: SemanticField[date] = Field(default_factory=SemanticField)
    duration: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    duration_unit: SemanticField[str] = Field(default_factory=SemanticField[str])
    anchor: SemanticField[str] = Field(default_factory=SemanticField[str])


class PaymentObligation(ContractModel):
    obligation_id: str = Field(min_length=1)
    wording: SemanticField[str] = Field(default_factory=SemanticField[str])
    kind: SemanticField[str] = Field(default_factory=SemanticField[str])
    percent: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    amount: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    currency: SemanticField[str] = Field(default_factory=SemanticField[str])
    timing: DeliveryTerms = Field(default_factory=DeliveryTerms)


class PenaltyClause(ContractModel):
    clause_id: str = Field(min_length=1)
    wording: SemanticField[str] = Field(default_factory=SemanticField[str])
    polarity: SemanticField[Literal['operative','negated','conditional','uncertain']] = Field(default_factory=SemanticField)
    rate_percent: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    basis: SemanticField[str] = Field(default_factory=SemanticField[str])
    period: SemanticField[str] = Field(default_factory=SemanticField[str])
    cap_percent: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    conditions: SemanticField[str] = Field(default_factory=SemanticField[str])


class ValidityTerms(ContractModel):
    wording: SemanticField[str] = Field(default_factory=SemanticField[str])
    days: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    expires_on: SemanticField[date] = Field(default_factory=SemanticField)
    anchor: SemanticField[str] = Field(default_factory=SemanticField[str])


class DocumentAmounts(ContractModel):
    subtotal: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    total: SemanticField[ExactDecimal] = Field(default_factory=SemanticField)
    currency: SemanticField[str] = Field(default_factory=SemanticField[str])


class SemanticDocument(ContractModel):
    schema_version: Literal['1.0'] = '1.0'
    document_id: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    ingestion_completeness: Completeness
    interpretation_method: Literal['MANUAL','FUTURE_PROVIDER']
    document_type: SemanticField[str] = Field(default_factory=SemanticField[str])
    supplier: SupplierIdentity = Field(default_factory=SupplierIdentity)
    quotation_number: SemanticField[str] = Field(default_factory=SemanticField[str])
    quotation_date: SemanticField[date] = Field(default_factory=SemanticField)
    purchase_reference: SemanticField[str] = Field(default_factory=SemanticField[str])
    line_items: tuple[SemanticLineItem,...] = ()
    delivery: DeliveryTerms = Field(default_factory=DeliveryTerms)
    payments: tuple[PaymentObligation,...] = ()
    tax: TaxTreatment = Field(default_factory=TaxTreatment)
    amounts: DocumentAmounts = Field(default_factory=DocumentAmounts)
    penalties: tuple[PenaltyClause,...] = ()
    validity: ValidityTerms = Field(default_factory=ValidityTerms)
    contractual_exceptions: tuple[SemanticField[str],...] = ()

    @model_validator(mode='after')
    def identities(self):
        for values,key in ((self.line_items,'item_id'),(self.payments,'obligation_id'),(self.penalties,'clause_id')):
            ids=[getattr(value,key) for value in values]
            if len(ids)!=len(set(ids)):
                raise ValueError('Semantic collection identities must be unique')
        for field in _semantic_fields(self):
            if any(e.source.document_id!=self.document_id for e in field.evidence):
                raise ValueError('Semantic evidence belongs to another document')
        return self

    @property
    def fully_verified(self) -> bool:
        fields=list(_semantic_fields(self))
        return self.ingestion_completeness==Completeness.COMPLETE and bool(fields) and all(f.state=='VERIFIED' for f in fields)


def _semantic_fields(value):
    if isinstance(value,SemanticField):
        yield value
    elif isinstance(value,ContractModel):
        for name in type(value).model_fields:
            yield from _semantic_fields(getattr(value,name))
    elif isinstance(value,tuple):
        for item in value:
            yield from _semantic_fields(item)
