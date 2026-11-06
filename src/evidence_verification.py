"""Independent source/role/normalization checks. No model, legacy extractor or rules."""
from hashlib import sha256
import re

from src.document_sources import get_page, get_page_text, resolve_source, SourceLookupError
from src.ingestion_models import ContractModel, DocumentRecord, Completeness, OCRStatus
from src.llm_contracts import StructuredExtractionResult
from src.semantic_models import SemanticDocument, SemanticField
from src.verification_conflicts import detect_source_conflicts
from src.verification_models import (VerificationOutcome as O, VerificationConfig,
    EvidenceCheck, FieldVerification, ResolvedSpan, CoverageSummary, EvidenceVerificationReport)
from src.verification_text import quote_spans, enclosing_context, contains_value
from src.verification_values import support_value, normalize_supported


def _one_raw(field):
    return field.raw_values[0] if field and len(set(field.raw_values))==1 else None


def _walk(value,path='',meta=None):
    meta=dict(meta or {})
    if isinstance(value,SemanticField):
        yield path,value,meta
    elif isinstance(value,ContractModel):
        if hasattr(value,'item_id'):
            meta.update(item_code=_one_raw(value.code),currency=_one_raw(value.currency))
        elif hasattr(value,'registrations'):
            pass
        elif hasattr(value,'kind') and hasattr(value,'owner_role'):
            meta['registration_kind']=value.kind
        if hasattr(value,'currency') and isinstance(value.currency,SemanticField):
            meta['currency']=_one_raw(value.currency)
        for name in type(value).model_fields:
            yield from _walk(getattr(value,name),f'{path}.{name}' if path else name,meta)
    elif isinstance(value,tuple):
        for index,item in enumerate(value):
            yield from _walk(item,f'{path}[{index}]',meta)


def _empty_field(path,field,reason):
    return FieldVerification(field_path=path,extraction_state=field.state,raw_values=field.raw_values,
        proposed_normalization=field.proposed_normalization,outcome=O.NOT_CHECKABLE,
        claimed_normalized_value=str(field.normalized_value) if field.normalized_value is not None else None,
        quote_outcome=O.NOT_CHECKABLE,raw_value_outcome=O.NOT_CHECKABLE,
        normalization_outcome=O.NOT_CHECKABLE,reason_codes=(reason,),
        explanation='No independently supported fact is produced; missing extraction is not proof of absence.',
        uncertain=True,requires_human_interpretation=bool(field.raw_values))


def _proposal(field):
    if field.proposed_normalization is not None:
        return field.proposed_normalization
    return str(field.normalized_value) if field.normalized_value is not None else None


def _check_evidence(document,path,field,evidence,meta,config):
    reasons=[];spans=[];supported=set();normalized=set();values=[];policy=False
    quote_state=O.UNVERIFIED
    try:
        if evidence.source.document_id!=document.metadata.document_id:
            raise SourceLookupError('Identity mismatch')
        page=get_page(document,evidence.source.page_number)
        if page.has_unreadable_content:
            return EvidenceCheck(source=evidence.source,claimed_quote=evidence.quote,
                quote_outcome=O.NOT_CHECKABLE,value_outcome=O.NOT_CHECKABLE,
                normalized_outcome=O.NOT_CHECKABLE,reasons=('PAGE_NOT_FULLY_READABLE',)),values
        if evidence.source.text_origin=='OCR' and not config.trust_completed_ocr:
            return EvidenceCheck(source=evidence.source,claimed_quote=evidence.quote,
                quote_outcome=O.NOT_CHECKABLE,value_outcome=O.NOT_CHECKABLE,
                normalized_outcome=O.NOT_CHECKABLE,reasons=('OCR_TRANSCRIPT_NOT_AUTHORIZED',)),values
        if len(evidence.quote)>config.max_quote_characters:
            raise SourceLookupError('Quote limit')
        resolved=resolve_source(document,evidence.source)
        page_text=get_page_text(document,evidence.source.page_number,origin=evidence.source.text_origin)
        matches=quote_spans(resolved.text,evidence.quote,config.max_quote_occurrences)
        page_matches=quote_spans(page_text,evidence.quote,config.max_quote_occurrences)
        if not matches or not page_matches:
            reasons.append('QUOTE_NOT_ON_CLAIMED_SOURCE')
        elif len(matches)>config.max_quote_occurrences:
            quote_state=O.AMBIGUOUS;reasons.append('QUOTE_OCCURRENCE_LIMIT')
        else:
            quote_state=O.VERIFIED
            for start,end,method in matches:
                spans.append(ResolvedSpan(source=evidence.source,start=start,end=end,
                    source_text=resolved.text[start:end],match_method=method))
            for index,raw in enumerate(field.raw_values):
                if not contains_value(evidence.quote,raw):
                    reasons.append('RAW_VALUE_NOT_IN_QUOTE')
                    continue
                per_match=[]
                for start,end,_ in matches:
                    context=enclosing_context(resolved.text,start,end)
                    check=support_value(path,raw,context,item_code=meta.get('item_code'),currency=meta.get('currency'),registration_kind=meta.get('registration_kind'))
                    per_match.append(check)
                # Repeated equal facts may corroborate a value, but no convenient
                # occurrence is selected if another occurrence has a different role.
                if not all(check.supported for check in per_match):
                    reasons.extend(check.reason for check in per_match if not check.supported)
                    policy|=any(check.policy_review for check in per_match)
                    continue
                supported.add(index)
                normalizations=[]
                for check in per_match:
                    ok,value,reason=normalize_supported(check,_proposal(field),config)
                    if field.proposed_normalization is not None and field.normalized_value is not None:
                        secondary,secondary_value,_=normalize_supported(check,str(field.normalized_value),config)
                        if not secondary or secondary_value!=value:
                            ok=False;reason='CLAIMED_NORMALIZATIONS_CONFLICT'
                    normalizations.append(ok)
                    reasons.append(reason);policy|=check.policy_review
                    if ok:
                        values.append((index,value))
                    else:
                        reasons.append('NORMALIZATION_NOT_SUPPORTED');policy=True
                if all(normalizations):
                    normalized.add(index)
    except (SourceLookupError,ValueError,TypeError):
        reasons.append('SOURCE_REFERENCE_INVALID_OR_UNAVAILABLE')
    value_state=O.VERIFIED if supported else O.UNVERIFIED if quote_state==O.VERIFIED else O.NOT_CHECKABLE
    norm_state=O.VERIFIED if normalized else O.UNVERIFIED if supported else O.NOT_CHECKABLE
    unique={value for _,value in values}
    return EvidenceCheck(source=evidence.source,claimed_quote=evidence.quote,
        quote_outcome=quote_state,value_outcome=value_state,normalized_outcome=norm_state,
        spans=tuple(spans),normalized_value=next(iter(unique)) if len(unique)==1 else None,
        reasons=tuple(dict.fromkeys(reasons)),policy_interpretation_required=policy,
        supported_raw_indices=tuple(sorted(supported)),normalized_raw_indices=tuple(sorted(normalized))),values


def _verify_field(document,path,field,meta,config):
    if field.state=='MISSING': return _empty_field(path,field,'MISSING_IS_NOT_ABSENCE')
    if not field.raw_values or not field.evidence:
        return _empty_field(path,field,'NO_USABLE_VALUE_OR_EVIDENCE')
    checks=[];values=[]
    for evidence in field.evidence:
        check,observed=_check_evidence(document,path,field,evidence,meta,config)
        checks.append(check);values.extend(observed)
    quote_ok=all(c.quote_outcome==O.VERIFIED for c in checks)
    raw_ok=set(range(len(field.raw_values)))<=set(i for c in checks for i in c.supported_raw_indices)
    norm_ok=set(range(len(field.raw_values)))<=set(i for c in checks for i in c.normalized_raw_indices)
    unique={value for _,value in values}
    policy=any(c.policy_interpretation_required for c in checks)
    reasons=list(dict.fromkeys(code for check in checks for code in check.reasons))
    if field.state=='CONFLICTING' or len(unique)>1:
        outcome=O.CONFLICTING;reasons.append('CLAIM_VALUES_CONFLICT')
    elif field.state=='AMBIGUOUS': outcome=O.AMBIGUOUS;reasons.append('CLAIM_REMAINS_AMBIGUOUS')
    elif field.state=='UNSUPPORTED': outcome=O.UNSUPPORTED
    elif any(c.quote_outcome==O.NOT_CHECKABLE for c in checks): outcome=O.NOT_CHECKABLE
    elif quote_ok and raw_ok and norm_ok and len(unique)==1: outcome=O.VERIFIED
    elif raw_ok and not norm_ok: outcome=O.AMBIGUOUS
    else: outcome=O.UNVERIFIED
    ready=outcome==O.VERIFIED and not policy and document.completeness==Completeness.COMPLETE
    return FieldVerification(field_path=path,extraction_state=field.state,raw_values=field.raw_values,
        proposed_normalization=field.proposed_normalization,outcome=outcome,
        claimed_normalized_value=str(field.normalized_value) if field.normalized_value is not None else None,
        quote_outcome=O.VERIFIED if quote_ok else O.NOT_CHECKABLE if any(c.quote_outcome==O.NOT_CHECKABLE for c in checks) else O.UNVERIFIED,
        raw_value_outcome=O.VERIFIED if raw_ok else O.UNVERIFIED,
        normalization_outcome=O.VERIFIED if norm_ok and len(unique)==1 else O.UNVERIFIED if raw_ok else O.NOT_CHECKABLE,
        normalized_value=next(iter(unique)) if outcome==O.VERIFIED else None,
        evidence_checks=tuple(checks),reason_codes=tuple(dict.fromkeys(reasons)),
        explanation='Quote location, field role, value and deterministic interpretation are assessed separately; procurement policy is not evaluated.',
        uncertain=outcome!=O.VERIFIED,conflicting=outcome==O.CONFLICTING,
        requires_human_interpretation=policy or outcome!=O.VERIFIED,eligible_for_future_use=ready)


def _coverage(document,fields,config,conflicts):
    by_path={field.field_path:field for field in fields}
    required=[]
    for pattern in config.required_fields:
        expression=re.escape(pattern).replace(r'\[\*\]',r'\[\d+\]')
        matches=[path for path in by_path if re.fullmatch(expression,path)]
        required.extend(matches or [pattern])
    required=tuple(dict.fromkeys(required))
    present=tuple(path for path in required if path in by_path and by_path[path].extraction_state!='MISSING' and by_path[path].raw_values)
    verified=tuple(path for path in required if path in by_path and by_path[path].outcome==O.VERIFIED)
    empty_groups=[]
    for pattern in ('supplier.registrations[*].value','line_items[*].description',
                    'payments[*].wording','penalties[*].wording','contractual_exceptions[*]'):
        expression=re.escape(pattern).replace(r'\[\*\]',r'\[\d+\]')
        if not any(re.fullmatch(expression,path) for path in by_path): empty_groups.append(pattern)
    missing=tuple(dict.fromkeys([f.field_path for f in fields if f.extraction_state=='MISSING']+[p for p in required if p not in by_path]+empty_groups))
    partial=tuple(p.page_number for p in document.pages if p.has_unreadable_content)
    ocr=tuple(p.page_number for p in document.pages if p.ocr_status in (OCRStatus.REQUIRED,OCRStatus.FAILED))
    ready=all(path in by_path and by_path[path].eligible_for_future_use for path in required) and not partial and not conflicts and document.completeness==Completeness.COMPLETE
    return CoverageSummary(required_fields=required,required_present=present,required_verified=verified,
        missing_from_output=missing,present_but_unsupported=tuple(f.field_path for f in fields if f.extraction_state!='MISSING' and f.outcome!=O.VERIFIED),
        conflicting_fields=tuple(f.field_path for f in fields if f.conflicting),
        human_interpretation_fields=tuple(f.field_path for f in fields if f.requires_human_interpretation),
        pages_not_fully_processed=partial,ocr_required_pages=ocr,required_fields_ready=ready)


def _check_identity(document,extraction,claims,source_bytes,config):
    try:
        DocumentRecord.model_validate(document.model_dump(round_trip=True))
        if not document.metadata.content_sha256 or claims is None or claims.document_id!=document.metadata.document_id:
            return O.UNVERIFIED,'MISMATCH','DOCUMENT_IDENTITY_MISMATCH'
        if isinstance(extraction,StructuredExtractionResult) and (extraction.document_id!=document.metadata.document_id or extraction.content_sha256!=document.metadata.content_sha256):
            return O.UNVERIFIED,'MISMATCH','EXTRACTION_HASH_MISMATCH'
        if isinstance(extraction,StructuredExtractionResult) and extraction.ingestion_completeness!=document.completeness:
            return O.UNVERIFIED,'MISMATCH','EXTRACTION_COVERAGE_MISMATCH'
        if source_bytes is not None:
            if not isinstance(source_bytes,bytes) or sha256(source_bytes).hexdigest()!=document.metadata.content_sha256:
                return O.UNVERIFIED,'MISMATCH','SOURCE_BYTES_HASH_MISMATCH'
            from src.pdf_ingestion import ingest_pdf
            actual=ingest_pdf(source_bytes)
            if actual.metadata.page_count!=document.metadata.page_count:
                return O.UNVERIFIED,'MISMATCH','SOURCE_PAGE_COUNT_MISMATCH'
            for page in document.pages:
                if page.extraction_method=='PYMUPDF_NATIVE' and page.text!=actual.pages[page.page_number-1].text:
                    return O.UNVERIFIED,'MISMATCH','SOURCE_TEXT_MISMATCH'
                for block in page.blocks:
                    if block.text_origin=='NATIVE' and block not in actual.pages[page.page_number-1].blocks:
                        return O.UNVERIFIED,'MISMATCH','SOURCE_BLOCK_MISMATCH'
            return O.VERIFIED,'RECOMPUTED','SOURCE_BYTES_AND_NATIVE_RECORD_MATCH'
        if config.require_source_bytes:
            return O.NOT_CHECKABLE,'UNAVAILABLE','ORIGINAL_BYTES_REQUIRED'
        return O.VERIFIED,'DECLARED_IDENTITIES_MATCH','TRUSTED_INGESTION_RECORD_IDENTITY_MATCH'
    except (ValueError,TypeError,IndexError):
        return O.UNVERIFIED,'MISMATCH','INVALID_INGESTION_RECORD'


def verify_extraction(ingested_document: DocumentRecord, structured_extraction,
                      *,config: VerificationConfig | None=None,source_bytes: bytes | None=None) -> EvidenceVerificationReport:
    """Verify claims without an API call, regex dependency or procurement outcome.

    The default trust boundary is the original ingestion record. Supply bytes
    (or require_source_bytes) to additionally bind its native text to original PDF
    bytes. OCR trust is explicit and never inferred from an LLM assertion.
    """
    config=config or VerificationConfig()
    document=ingested_document
    claims=structured_extraction.validated_claims if isinstance(structured_extraction,StructuredExtractionResult) else structured_extraction if isinstance(structured_extraction,SemanticDocument) else None
    identity,hash_check,reason=_check_identity(document,structured_extraction,claims,source_bytes,config)
    eligible_input=claims is not None and (not isinstance(structured_extraction,StructuredExtractionResult) or (structured_extraction.status=='SCHEMA_VALIDATED' and structured_extraction.input_complete))
    if claims and claims.ingestion_completeness!=document.completeness:
        identity=O.UNVERIFIED;reason='INGESTION_COMPLETENESS_MISMATCH'
    found=list(_walk(claims)) if claims else []
    if len(found)>config.max_fields:
        identity=O.NOT_CHECKABLE;reason='VERIFICATION_FIELD_LIMIT'
    fields=[]
    metadata={path:meta for path,_,meta in found}
    for path,field,meta in found:
        if not eligible_input or identity!=O.VERIFIED:
            fields.append(_empty_field(path,field,reason if eligible_input else 'EXTRACTION_NOT_READY'))
        else:
            fields.append(_verify_field(document,path,field,meta,config))
    comparisons=detect_source_conflicts(document,metadata) if identity==O.VERIFIED else ()
    conflicts=tuple(c for c in comparisons if c.requires_human_review)
    for index,field in enumerate(fields):
        affecting=[c for c in conflicts if field.field_path in c.field_paths]
        if affecting and field.extraction_state!='MISSING':
            strong=any(c.classification=='CONTRADICTION' for c in affecting)
            fields[index]=field.model_copy(update={'outcome':O.CONFLICTING if strong else O.AMBIGUOUS,
                'conflicting':strong,'uncertain':True,'requires_human_interpretation':True,
                'eligible_for_future_use':False,'normalized_value':None,
                'reason_codes':field.reason_codes+tuple(c.reason_code for c in affecting)})
    coverage=_coverage(document,fields,config,conflicts)
    identifier=document.metadata.document_id
    digest=document.metadata.content_sha256
    return EvidenceVerificationReport(document_id=identifier if identifier and re.fullmatch(r'sha256:[0-9a-f]{64}',identifier) else None,
        content_sha256=digest if digest and re.fullmatch('[0-9a-f]{64}',digest) else None,
        identity_outcome=identity,hash_check=hash_check,document_completeness=document.completeness,
        extraction_complete=eligible_input,configuration=config,
        fields=tuple(fields),conflicts=conflicts,comparisons=comparisons,coverage=coverage,
        needs_human_review=identity!=O.VERIFIED or not coverage.required_fields_ready or any(f.requires_human_interpretation for f in fields),
        diagnostics=(reason,'MISSING_EXTRACTION_IS_NOT_ABSENCE','NO_PROCUREMENT_DECISION_PRODUCED'))


def export_verification_audit(report: EvidenceVerificationReport, *,include_source_content=False):
    """Metadata-only by default; explicit sensitive export is the caller's choice."""
    data=report.model_dump(mode='json')
    if include_source_content:
        for item,field in zip(data['fields'],report.fields,strict=True):
            item['raw_values']=list(field.raw_values)
            item['proposed_normalization']=field.proposed_normalization
            item['claimed_normalized_value']=field.claimed_normalized_value
            item['normalized_value']=field.normalized_value.model_dump(mode='json') if field.normalized_value else None
            for audit,check in zip(item['evidence_checks'],field.evidence_checks,strict=True):
                audit['source']=check.source.model_dump(mode='json')
                audit['claimed_quote']=check.claimed_quote
                audit['normalized_value']=check.normalized_value.model_dump(mode='json') if check.normalized_value else None
                for span_data,span in zip(audit['spans'],check.spans,strict=True):
                    span_data.update(source_text=span.source_text,source=span.source.model_dump(mode='json'))
        for group_name in ('conflicts','comparisons'):
            for item,group in zip(data[group_name],getattr(report,group_name),strict=True):
                for audit,observation in zip(item['observations'],group.observations,strict=True):
                    audit.update(value=observation.value,quote=observation.quote,reference=observation.reference.model_dump(mode='json'))
    return data
