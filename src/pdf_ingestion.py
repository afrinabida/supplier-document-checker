"""Layout-independent, bounded native PDF ingestion parallel to the legacy parser.

No OCR, semantic extraction, network request, persistent storage or business rule
is executed here. COMPLETE means native text coverage, never verified meaning.
"""
from hashlib import sha256
from pathlib import Path, PureWindowsPath
import re
import unicodedata

import pymupdf
from pydantic import Field

from src.ingestion_models import (BoundingBox, Completeness, ContractModel,
    Diagnostic, DocumentMetadata, DocumentRecord, LayoutBlock, OCRStatus,
    PageRecord, TextQuality, coverage_state)


class IngestionLimits(ContractModel):
    max_file_bytes: int = Field(default=25*1024*1024, gt=0)
    max_pages: int = Field(default=200, gt=0)
    max_page_characters: int = Field(default=200_000, gt=0)
    max_total_characters: int = Field(default=2_000_000, gt=0)
    max_blocks_per_page: int = Field(default=5_000, gt=0)
    max_images_per_page: int = Field(default=200, ge=0)
    max_page_area_points: float = Field(default=100_000_000, gt=0, allow_inf_nan=False)
    minimum_text_characters: int = Field(default=1, gt=0)
    max_replacement_fraction: float = Field(default=0.05, ge=0, le=1, allow_inf_nan=False)
    include_layout_blocks: bool = True


def safe_filename(value: str | None) -> str:
    name=PureWindowsPath(str(value or 'document.pdf')).name
    name=unicodedata.normalize('NFKC',name)
    name=''.join(c for c in name if unicodedata.category(c) not in ('Cc','Cf'))
    name=re.sub(r'[<>:"/\\|?*]', '_', name).strip(' .')
    if not name or name in ('.','..'):
        return 'document.pdf'
    # Avoid reserved Windows device names as well as paths/control characters.
    if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        name='document_'+name
    if len(name)>128:
        suffix=Path(name).suffix[:16]
        name=name[:128-len(suffix)]+suffix
    return name


def diagnostic(code, message, *, error=False):
    return Diagnostic(code=code,message=message,severity='error' if error else 'warning')


def _failed(name, code, message, *, size=None, digest=None, count=None, hint='unknown'):
    return DocumentRecord(metadata=DocumentMetadata(
        document_id='sha256:'+digest if digest else None, original_filename=name,
        content_sha256=digest,file_size=size,page_count=count,
        document_type_hint=hint,processing_status='FAILED',
        diagnostics=(diagnostic(code,message,error=True),)),completeness=Completeness.FAILED)


def _unreadable_page(number, code, message, *, width=None, height=None, rotation=0):
    return PageRecord(page_number=number,width=width,height=height,rotation=rotation,
                      quality=TextQuality.EXTRACTION_FAILED,
                      warnings=(diagnostic(code,message,error=True),))


def _read_page(page, number, limits, remaining):
    width,height=float(page.cropbox.width),float(page.cropbox.height)
    rotation=page.rotation
    if width<=0 or height<=0 or width*height>limits.max_page_area_points:
        return _unreadable_page(number,'PAGE_AREA_LIMIT','Page geometry exceeds the configured safety limit.')
    if remaining<=0:
        return _unreadable_page(number,'DOCUMENT_TEXT_LIMIT','Text budget exhausted; page was not extracted.',width=width,height=height,rotation=rotation)
    # One native text page preserves source block order and image metadata without
    # decoding image payloads into Python or rasterizing the page.
    textpage=page.get_textpage(flags=pymupdf.TEXTFLAGS_BLOCKS | pymupdf.TEXT_PRESERVE_IMAGES)
    text=textpage.extractText(sort=False)
    native_blocks=textpage.extractBLOCKS()
    if len(text)>limits.max_page_characters or len(text)>remaining:
        return _unreadable_page(number,'TEXT_LIMIT','Page text exceeds the configured extraction budget.',width=width,height=height,rotation=rotation)
    if len(native_blocks)>limits.max_blocks_per_page:
        return _unreadable_page(number,'BLOCK_LIMIT','Page block count exceeds the configured safety limit.',width=width,height=height,rotation=rotation)
    images=sum(block[6]==1 for block in native_blocks)
    if images>limits.max_images_per_page:
        return _unreadable_page(number,'IMAGE_LIMIT','Page image count exceeds the configured safety limit.',width=width,height=height,rotation=rotation)
    blocks=[]
    for index,block in enumerate(native_blocks):
        bbox=BoundingBox(x0=block[0],y0=block[1],x1=block[2],y1=block[3])
        if limits.include_layout_blocks:
            blocks.append(LayoutBlock(block_id=f'p{number}-native-b{index}',source_index=block[5],
                kind='TEXT' if block[6]==0 else 'IMAGE',bbox=bbox,text=block[4] if block[6]==0 else ''))
    visible=text.strip()
    quality=TextQuality.AVAILABLE
    warnings=[]
    if not visible:
        quality=TextQuality.NO_TEXT
        warnings.append(diagnostic('NO_NATIVE_TEXT','No native text is available; OCR or manual inspection is required.'))
    elif (len(visible)<limits.minimum_text_characters or not any(c.isalnum() for c in visible)
          or visible.count('\ufffd')/len(visible)>limits.max_replacement_fraction):
        quality=TextQuality.LOW_QUALITY
        warnings.append(diagnostic('LOW_TEXT_QUALITY','Native text quality is uncertain; inspect the source page.'))
    if images:
        warnings.append(diagnostic('UNTRANSCRIBED_IMAGES','Displayed image content has not been transcribed; OCR or manual inspection is required.'))
    needs_ocr=quality!=TextQuality.AVAILABLE or images>0
    return PageRecord(page_number=number,text=text,extraction_method='PYMUPDF_NATIVE',
        quality=quality,width=width,height=height,rotation=rotation,blocks=tuple(blocks),
        image_count=images,ocr_status=OCRStatus.REQUIRED if needs_ocr else OCRStatus.NOT_REQUIRED,
        warnings=tuple(warnings))


def ingest_pdf(source: str | Path | bytes, filename: str | None = None, *,
               limits: IngestionLimits | None = None,
               document_type_hint: str = 'unknown') -> DocumentRecord:
    """Return page-aware records or safe typed failure; never mutate source bytes.

    Type hints are caller-supplied, non-authoritative metadata. The parser does
    not classify businesses/documents or infer procurement facts from layout.
    Invalid configuration is a caller error, not a document diagnostic.
    """
    limits=limits or IngestionLimits()
    if document_type_hint not in ('quotation','contract','combined','unknown'):
        raise ValueError('Unsupported document type hint')
    source_name=filename or (Path(source).name if isinstance(source,(str,Path)) else 'document.pdf')
    name=safe_filename(source_name)
    size=None
    try:
        if isinstance(source,bytes):
            size=len(source)
            if size>limits.max_file_bytes:
                return _failed(name,'FILE_SIZE_LIMIT','PDF exceeds the configured byte limit.',size=size,hint=document_type_hint)
            data=source
        elif isinstance(source,(str,Path)):
            path=Path(source)
            size=path.stat().st_size
            if size>limits.max_file_bytes:
                return _failed(name,'FILE_SIZE_LIMIT','PDF exceeds the configured byte limit.',size=size,hint=document_type_hint)
            with path.open('rb') as stream:
                data=stream.read(limits.max_file_bytes+1)
            size=len(data)
            if size>limits.max_file_bytes:
                return _failed(name,'FILE_SIZE_LIMIT','PDF exceeds the configured byte limit.',size=size,hint=document_type_hint)
        else:
            return _failed(name,'INPUT_TYPE','Supply PDF bytes or a local PDF file.',hint=document_type_hint)
    except (OSError,ValueError):
        return _failed(name,'SOURCE_READ_FAILED','PDF source could not be read. Check access and select the file again.',hint=document_type_hint)
    digest=sha256(data).hexdigest()
    if not data or not data.lstrip().startswith(b'%PDF-'):
        return _failed(name,'INVALID_PDF','The input is empty or does not have a PDF header.',size=size,digest=digest,hint=document_type_hint)
    try:
        with pymupdf.open(stream=data,filetype='pdf') as pdf:
            count=pdf.page_count
            if pdf.needs_pass or pdf.is_encrypted:
                return _failed(name,'ENCRYPTED_PDF','Password-protected PDFs are not supported. Supply an authorized unencrypted copy.',size=size,digest=digest,count=count,hint=document_type_hint)
            if not count or count>limits.max_pages:
                return _failed(name,'PAGE_COUNT_LIMIT','PDF page count is empty or exceeds the configured limit.',size=size,digest=digest,count=count,hint=document_type_hint)
            pages=[]
            remaining=limits.max_total_characters
            for index in range(count):
                try:
                    page=_read_page(pdf.load_page(index),index+1,limits,remaining)
                except (RuntimeError,ValueError,OverflowError,IndexError):
                    page=_unreadable_page(index+1,'PAGE_EXTRACTION_FAILED','Native page extraction failed; inspect or re-export the page.')
                pages.append(page)
                remaining-=len(page.text)
            coverage=coverage_state(pages)
            notes=[]
            if name!=str(source_name):
                notes.append(diagnostic('FILENAME_SANITIZED','Filename was reduced to a safe display basename.'))
            if pdf.is_repaired:
                notes.append(diagnostic('PDF_REPAIRED','The PDF parser repaired the input; inspect the original source.'))
                # Parser repair is not evidence that all source content survived.
                if coverage==Completeness.COMPLETE:
                    pages[0]=pages[0].model_copy(update={'quality':TextQuality.LOW_QUALITY,
                        'ocr_status':OCRStatus.REQUIRED,'warnings':pages[0].warnings+(notes[-1],)})
                    coverage=coverage_state(pages)
            if coverage!=Completeness.COMPLETE:
                notes.append(diagnostic('INCOMPLETE_COVERAGE','Some page content remains unreadable or untranscribed; clause absence cannot be inferred.'))
            return DocumentRecord(metadata=DocumentMetadata(document_id='sha256:'+digest,
                original_filename=name,content_sha256=digest,page_count=count,file_size=size,
                document_type_hint=document_type_hint,processing_status='SUCCEEDED' if coverage==Completeness.COMPLETE else 'PARTIAL',
                diagnostics=tuple(notes)),pages=tuple(pages),completeness=coverage)
    except (RuntimeError,ValueError,OverflowError,IndexError):
        return _failed(name,'PDF_OPEN_FAILED','PDF could not be parsed. Re-export the document or inspect it manually.',size=size,digest=digest,hint=document_type_hint)
