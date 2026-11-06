"""Typed extraction, purchase request, rule evaluation and decision records."""
from typing import Literal
from pydantic import BaseModel, Field

class SourceEvidence(BaseModel):
    filename: str
    page_number: int = Field(ge=1)
    quote: str = Field(min_length=1)

class PDFPage(BaseModel):
    page_number: int = Field(ge=1)
    text: str

class PDFDocument(BaseModel):
    filename: str
    pages: list[PDFPage]

class ExtractedField(BaseModel):
    raw_values: list[str]
    normalized: str | int | bool | dict | None = None
    evidence: list[SourceEvidence]
    status: Literal['found', 'ambiguous'] = 'found'

class MissingField(BaseModel):
    field: str
    reason: str = 'Not located by the supported extraction patterns; inspect source.'
    requires_review: bool = True

class ExtractionWarning(BaseModel):
    field: str
    message: str
    requires_review: bool = True

class ExtractionResult(BaseModel):
    document: PDFDocument
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    missing_fields: list[MissingField] = Field(default_factory=list)
    warnings: list[ExtractionWarning] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)

    @property
    def requires_review(self) -> bool:
        return bool(self.missing_fields or self.warnings)

# Decision records extend, rather than replace, the extraction contract.
from decimal import Decimal
from typing import Any

class PurchaseRequestItem(BaseModel):
    pr_number: str
    item_code: str
    description: str
    unit: str
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    maximum_unit_price: Decimal = Field(gt=0, allow_inf_nan=False)
    source_filename: str = 'purchase-request.csv'
    price_tax_basis: Literal['separate', 'included'] | None = None

class RuleResult(BaseModel):
    rule_id: str
    wording: str
    category: Literal['Reject', 'Needs a person to check']
    status: Literal['pass', 'fail', 'unknown', 'not_applicable']
    actual_value: Any = None
    expected_value: Any = None
    evidence: list[SourceEvidence] = Field(default_factory=list)
    explanation: str

class RuleEvaluation(BaseModel):
    filename: str
    matched_item: PurchaseRequestItem | None = None
    results: list[RuleResult]

class SupplierDecision(BaseModel):
    filename: str
    decision: Literal['Approve', 'Reject', 'Needs a person to check']
    evaluation: RuleEvaluation
    reasons: list[RuleResult]
