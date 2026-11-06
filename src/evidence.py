"""Exact source evidence validation, independent of field extraction."""
from src.models import ExtractionResult, SourceEvidence

def collect_evidence(result: ExtractionResult) -> list[SourceEvidence]:
    pages = {p.page_number: p.text for p in result.document.pages}
    collected = []
    for field in result.fields.values():
        if not field.evidence:
            raise ValueError('Extracted field has no evidence.')
        for evidence in field.evidence:
            if (evidence.filename != result.document.filename or
                evidence.quote not in pages.get(evidence.page_number, '')):
                raise ValueError('Evidence does not match the source PDF.')
            collected.append(evidence)
    return collected
