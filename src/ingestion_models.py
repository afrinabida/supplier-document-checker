"""Versioned raw ingestion contracts, independent of procurement interpretation."""
from enum import Enum
import math
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Completeness(str, Enum):
    COMPLETE = 'COMPLETE'
    PARTIAL = 'PARTIAL'
    OCR_REQUIRED = 'OCR_REQUIRED'
    UNREADABLE = 'UNREADABLE'
    FAILED = 'FAILED'


class TextQuality(str, Enum):
    AVAILABLE = 'AVAILABLE'
    LOW_QUALITY = 'LOW_QUALITY'
    NO_TEXT = 'NO_TEXT'
    EXTRACTION_FAILED = 'EXTRACTION_FAILED'


class OCRStatus(str, Enum):
    NOT_REQUIRED = 'NOT_REQUIRED'
    REQUIRED = 'OCR_REQUIRED'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'


class Diagnostic(ContractModel):
    code: str = Field(min_length=1)
    severity: Literal['warning', 'error'] = 'warning'
    message: str = Field(min_length=1)


class BoundingBox(ContractModel):
    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode='after')
    def ordered_finite(self):
        if not all(math.isfinite(v) for v in (self.x0,self.y0,self.x1,self.y1)):
            raise ValueError('Coordinates must be finite')
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError('Coordinates must be ordered')
        return self


class SourceReference(ContractModel):
    document_id: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    page_number: int = Field(ge=1, strict=True)
    block_id: str | None = Field(default=None, min_length=1)
    bbox: BoundingBox | None = None
    text_origin: Literal['NATIVE', 'OCR'] = 'NATIVE'


class LayoutBlock(ContractModel):
    block_id: str = Field(min_length=1)
    source_index: int = Field(ge=0)
    kind: Literal['TEXT', 'IMAGE']
    bbox: BoundingBox
    text: str = ''
    text_origin: Literal['NATIVE', 'OCR'] = 'NATIVE'


class PageRecord(ContractModel):
    page_number: int = Field(ge=1, strict=True)
    text: str = ''
    extraction_method: Literal['PYMUPDF_NATIVE', 'NONE'] = 'NONE'
    quality: TextQuality = TextQuality.NO_TEXT
    width: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    height: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    rotation: Literal[0, 90, 180, 270] = 0
    blocks: tuple[LayoutBlock, ...] = ()
    reading_order: Literal['PDF_NATIVE_NOT_SEMANTIC'] = 'PDF_NATIVE_NOT_SEMANTIC'
    coordinate_system: Literal['UNROTATED_PAGE_POINTS_TOP_LEFT'] = 'UNROTATED_PAGE_POINTS_TOP_LEFT'
    image_count: int | None = Field(default=None, ge=0)
    ocr_status: OCRStatus = OCRStatus.REQUIRED
    ocr_text: str | None = None
    ocr_adapter_id: str | None = None
    warnings: tuple[Diagnostic, ...] = ()

    @model_validator(mode='after')
    def validate_provenance(self):
        ids=[b.block_id for b in self.blocks]
        if len(ids)!=len(set(ids)):
            raise ValueError('Block identifiers must be unique within a page')
        if self.quality == TextQuality.AVAILABLE and not self.text.strip():
            raise ValueError('Available native text cannot be empty')
        if self.ocr_status == OCRStatus.NOT_REQUIRED and (not self.text.strip() or self.image_count!=0):
            raise ValueError('Unresolved empty/image content requires OCR or review')
        if self.ocr_status == OCRStatus.COMPLETED and (not self.ocr_text or not self.ocr_text.strip() or not self.ocr_adapter_id):
            raise ValueError('Completed OCR requires text and adapter provenance')
        if self.ocr_status != OCRStatus.COMPLETED and self.ocr_text is not None:
            raise ValueError('OCR text requires completed OCR provenance')
        return self

    @computed_field
    @property
    def has_unreadable_content(self) -> bool:
        return (self.quality in (TextQuality.LOW_QUALITY,TextQuality.EXTRACTION_FAILED)
                or self.ocr_status in (OCRStatus.REQUIRED,OCRStatus.FAILED))


class DocumentMetadata(ContractModel):
    document_id: str | None = None
    original_filename: str = Field(min_length=1, max_length=128)
    content_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    page_count: int | None = Field(default=None, ge=0)
    file_size: int | None = Field(default=None, ge=0)
    document_type_hint: Literal['quotation','contract','combined','unknown'] = 'unknown'
    processing_status: Literal['SUCCEEDED','PARTIAL','FAILED']
    diagnostics: tuple[Diagnostic, ...] = ()

    @model_validator(mode='after')
    def identity_and_safe_filename(self):
        name=self.original_filename
        if (any(unicodedata.category(c) in ('Cc','Cf') or c in '/\\<>:"|?*' for c in name)
                or name!=name.strip(' .') or name in ('.','..')
                or re.fullmatch(r'(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',name)):
            raise ValueError('Filename must be a safe basename')
        expected='sha256:'+self.content_sha256 if self.content_sha256 else None
        if self.document_id!=expected:
            raise ValueError('Document identity must match content hash')
        return self


class DocumentRecord(ContractModel):
    schema_version: Literal['1.0'] = '1.0'
    metadata: DocumentMetadata
    pages: tuple[PageRecord, ...] = ()
    completeness: Completeness

    @model_validator(mode='after')
    def coherent_coverage(self):
        if [p.page_number for p in self.pages]!=list(range(1,len(self.pages)+1)):
            raise ValueError('Pages must be ordered, contiguous and 1-based')
        if self.metadata.processing_status=='FAILED':
            if self.completeness!=Completeness.FAILED or self.pages:
                raise ValueError('Failed ingestion cannot expose completed page records')
        else:
            if not self.pages or self.metadata.page_count!=len(self.pages) or not self.metadata.document_id:
                raise ValueError('Successful/partial ingestion requires identified page coverage')
            unreadable=any(p.has_unreadable_content for p in self.pages)
            expected_status='PARTIAL' if unreadable else 'SUCCEEDED'
            if self.metadata.processing_status!=expected_status:
                raise ValueError('Processing status must reflect page coverage')
            if self.completeness!=coverage_state(self.pages):
                raise ValueError('Completeness must reflect unreadable content')
        return self

    @computed_field
    @property
    def has_unreadable_pages(self) -> bool:
        return self.completeness==Completeness.FAILED or any(p.has_unreadable_content for p in self.pages)

    @computed_field
    @property
    def aggregated_text(self) -> str:
        parts=[]
        for page in self.pages:
            parts.append(f'<<<PAGE {page.page_number} BEGIN; ORIGIN=NATIVE; QUALITY={page.quality.value}>>>\n'
                         +page.text+f'\n<<<PAGE {page.page_number} END>>>')
        return '\n\n'.join(parts)


def coverage_state(pages) -> Completeness:
    if not any(page.has_unreadable_content for page in pages):
        return Completeness.COMPLETE
    if all(page.quality==TextQuality.EXTRACTION_FAILED for page in pages):
        return Completeness.UNREADABLE
    if any(page.text.strip() or page.ocr_text for page in pages):
        return Completeness.PARTIAL
    return Completeness.OCR_REQUIRED
