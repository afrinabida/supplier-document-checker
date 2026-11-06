"""Local PyMuPDF reading with original, unsorted page text preserved."""
from pathlib import Path
import pymupdf
from src.models import PDFDocument, PDFPage

class PDFReadError(ValueError):
    """A document cannot be read safely as a text PDF."""

def read_pdf(source: str | Path | bytes, filename: str | None = None) -> PDFDocument:
    name = filename or (Path(source).name if not isinstance(source, bytes) else 'upload.pdf')
    if not name.lower().endswith('.pdf'):
        raise PDFReadError('Please select a PDF file.')
    try:
        data = source if isinstance(source, bytes) else Path(source).read_bytes()
        if not data:
            raise PDFReadError('The PDF is empty.')
        if not data.lstrip().startswith(b'%PDF-'):
            raise PDFReadError('The file does not have a PDF header.')
        with pymupdf.open(stream=data, filetype='pdf') as pdf:
            if pdf.needs_pass:
                raise PDFReadError('Password-protected PDFs are not supported.')
            pages = [PDFPage(page_number=i+1, text=page.get_text('text', sort=False))
                     for i, page in enumerate(pdf)]
        if not pages or not any(p.text.strip() for p in pages):
            raise PDFReadError('No extractable text found. Scanned PDFs need OCR, which is not implemented.')
        return PDFDocument(filename=name, pages=pages)
    except PDFReadError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise PDFReadError(f'Cannot read {name}: {exc}') from exc
