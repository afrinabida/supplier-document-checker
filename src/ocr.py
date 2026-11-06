"""OCR-ready contract only. No OCR engine or automatic external call is installed here."""
from typing import Protocol

from pydantic import Field, model_validator

from src.ingestion_models import ContractModel, Diagnostic, LayoutBlock


class OCRRequest(ContractModel):
    document_id: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    page_number: int = Field(ge=1)
    pdf_bytes: bytes = Field(repr=False, exclude=True)
    max_output_characters: int = Field(gt=0)


class OCRResult(ContractModel):
    adapter_id: str = Field(min_length=1)
    text: str
    blocks: tuple[LayoutBlock,...] = ()
    warnings: tuple[Diagnostic,...] = ()

    @model_validator(mode='after')
    def ocr_block_provenance(self):
        if any(block.text_origin!='OCR' for block in self.blocks):
            raise ValueError('OCR adapter blocks must retain OCR origin')
        return self


class OCRAdapter(Protocol):
    """Future local/approved provider adapter; caller owns isolation and consent.

    Implementations must apply independent render/pixel/time/output limits and
    return OCR-origin blocks. This protocol is intentionally not invoked by the
    ingestion API; Step 3 leaves image content explicitly unresolved.
    """
    def extract_page(self, request: OCRRequest) -> OCRResult: ...
