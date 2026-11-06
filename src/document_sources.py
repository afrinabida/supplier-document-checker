"""Page/block source lookup. Quote location is not semantic verification."""
import json
from typing import Literal

from src.ingestion_models import (BoundingBox, ContractModel, DocumentRecord,
                                  LayoutBlock, SourceReference)


class SourceLookupError(ValueError):
    """A source reference cannot be resolved against the supplied record."""


class ResolvedSource(ContractModel):
    reference: SourceReference
    text: str
    blocks: tuple[LayoutBlock,...] = ()


class QuoteLookup(ContractModel):
    reference: SourceReference
    quote: str
    located: bool
    verification_level: Literal['TEXT_LOCATION_ONLY'] = 'TEXT_LOCATION_ONLY'


def get_page(record: DocumentRecord, page_number: int):
    if isinstance(page_number,bool) or not isinstance(page_number,int) or not 1<=page_number<=len(record.pages):
        raise SourceLookupError('Page reference is outside the ingested document.')
    return record.pages[page_number-1]


def get_page_text(record, page_number, *, origin='NATIVE') -> str:
    page=get_page(record,page_number)
    if origin=='NATIVE':
        return page.text
    if origin=='OCR' and page.ocr_text is not None:
        return page.ocr_text
    raise SourceLookupError('Requested text origin is unavailable.')


def get_source_block(record, page_number, block_id) -> LayoutBlock:
    for block in get_page(record,page_number).blocks:
        if block.block_id==block_id:
            return block
    raise SourceLookupError('Block reference is unavailable on this page.')


def source_reference(record, page_number, block_id=None, *, origin='NATIVE'):
    if record.metadata.document_id is None:
        raise SourceLookupError('Source content identity is unavailable.')
    get_page(record,page_number)
    block=get_source_block(record,page_number,block_id) if block_id else None
    ref=SourceReference(document_id=record.metadata.document_id,page_number=page_number,
        block_id=block_id,bbox=block.bbox if block else None,text_origin=origin)
    resolve_source(record,ref)
    return ref


def _contained(inner: BoundingBox, outer: BoundingBox):
    return inner.x0>=outer.x0 and inner.y0>=outer.y0 and inner.x1<=outer.x1 and inner.y1<=outer.y1


def resolve_source(record: DocumentRecord, reference: SourceReference) -> ResolvedSource:
    if reference.document_id!=record.metadata.document_id:
        raise SourceLookupError('Source reference belongs to a different document.')
    page=get_page(record,reference.page_number)
    if reference.block_id:
        block=get_source_block(record,reference.page_number,reference.block_id)
        if block.text_origin!=reference.text_origin:
            raise SourceLookupError('Referenced block has no text of the requested origin.')
        if reference.bbox is not None and reference.bbox!=block.bbox:
            raise SourceLookupError('Reference coordinates do not match the source block.')
        return ResolvedSource(reference=reference,text=block.text,blocks=(block,))
    if reference.bbox:
        if page.width is None or page.height is None:
            raise SourceLookupError('Page geometry is unavailable.')
        bounds=BoundingBox(x0=0,y0=0,x1=page.width,y1=page.height)
        if not _contained(reference.bbox,bounds):
            raise SourceLookupError('Reference coordinates are outside the source page.')
        blocks=tuple(b for b in page.blocks if b.kind=='TEXT' and b.text_origin==reference.text_origin and _contained(b.bbox,reference.bbox))
        if not blocks:
            raise SourceLookupError('No source text blocks are contained in this region.')
        return ResolvedSource(reference=reference,text='\n'.join(b.text for b in blocks),blocks=blocks)
    return ResolvedSource(reference=reference,text=get_page_text(record,reference.page_number,origin=reference.text_origin))


def lookup_quote(record, reference, quote: str) -> QuoteLookup:
    if not isinstance(quote,str) or not quote.strip():
        raise SourceLookupError('A nonblank exact source quote is required.')
    resolved=resolve_source(record,reference)
    return QuoteLookup(reference=reference,quote=quote,located=quote in resolved.text)


def build_page_context(record, *, page_numbers=None, origin='NATIVE') -> str:
    """JSON boundaries keep page/block structure explicit even for hostile text.

    Native PDF order is not a reconstructed table or semantic reading order.
    Image-only/unreadable pages are included with their quality/OCR status.
    """
    numbers=list(range(1,len(record.pages)+1)) if page_numbers is None else list(page_numbers)
    pages=[]
    for number in numbers:
        page=get_page(record,number)
        pages.append(dict(page_number=number,quality=page.quality.value,ocr_status=page.ocr_status.value,
            text_origin=origin,text=get_page_text(record,number,origin=origin),
            reading_order=page.reading_order,rotation=page.rotation,
            blocks=[b.model_dump(mode='json') for b in page.blocks if b.text_origin==origin]))
    return json.dumps(dict(document_id=record.metadata.document_id,
        completeness=record.completeness.value,pages=pages),ensure_ascii=False)
