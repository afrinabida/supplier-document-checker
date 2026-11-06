"""Revalidate report/claim binding before admitting any procurement fact."""
from hashlib import sha256
import json
import re
from src.document_sources import resolve_source
from src.verification_text import enclosing_context, quote_spans
from src.evidence_verification import verify_extraction, export_verification_audit
from src.llm_contracts import StructuredExtractionResult
from src.trusted_models import (TrustedFact, FactEvidence, FactGroup, TrustedSupplier,
    TrustedRegistration, TrustedLineItem, TrustedFactsResult)
from src.verification_models import VerificationOutcome as O


class TrustedFactsError(ValueError):
    """Safe error codes only: never include rejected source values."""


def _fingerprint(report):
    payload=export_verification_audit(report, include_source_content=True)
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def build_trusted_facts(ingested_document, semantic_extraction, verification_report, *, source_bytes=None):
    """Recompute verification; reject stale, altered or differently scoped reports.

    Original trusted ingestion is required. Pass original bytes when the report's
    configuration requires them. Monetary presentation rounding is never policy.
    """
    claims=(semantic_extraction.validated_claims if isinstance(semantic_extraction, StructuredExtractionResult)
            else semantic_extraction)
    meta=ingested_document.metadata
    if (claims is None or not meta.document_id or not meta.content_sha256 or
        verification_report.document_id!=meta.document_id or
        verification_report.content_sha256!=meta.content_sha256 or claims.document_id!=meta.document_id or
        verification_report.identity_outcome!=O.VERIFIED):
        raise TrustedFactsError('IDENTITY_OR_EXTRACTION_MISMATCH')
    if verification_report.configuration.money_decimal_places is not None:
        raise TrustedFactsError('ROUNDED_REPORT_NOT_POLICY_INPUT')
    # Recompute in the same identity mode; a RECOMPUTED report requires bytes.
    if verification_report.hash_check=='RECOMPUTED' and source_bytes is None:
        raise TrustedFactsError('ORIGINAL_BYTES_REQUIRED')
    current=verify_extraction(ingested_document,semantic_extraction,
        config=verification_report.configuration,source_bytes=source_bytes)
    if current.identity_outcome!=O.VERIFIED or _fingerprint(current)!=_fingerprint(verification_report):
        raise TrustedFactsError('STALE_OR_ALTERED_VERIFICATION')
    facts=[]
    for field in current.fields:
        conditional=False
        # Step 5 proves observations, not unconditional contractual applicability.
        # Inspect original enclosing clauses, including text outside a short quote.
        if field.field_path.startswith(('line_items[','delivery.','payments[','penalties[','validity.','tax.','amounts.')):
            for check in field.evidence_checks:
                if check.quote_outcome!=O.VERIFIED:
                    continue
                resolved=resolve_source(ingested_document,check.source)
                for start,end,_ in quote_spans(resolved.text,check.claimed_quote,50):
                    context=enclosing_context(resolved.text,start,end)
                    if not field.field_path.startswith('penalties[') and re.search(r'(?i)\bnot\b|\bno\b|\bnonbinding\b|not guaranteed',context):
                        conditional=True
                    if re.search(r'(?i)\bif\b|\bunless\b|\bexcept\b|subject to|provided that|contingent|proposed|estimated|tentative|supersed|amendment|alternative|optional|\bbetween\b|\brange\b|\bwindow\b|\bat least\b|\bat most\b|\bup to\b|\bto\s+\d|\bapproximately\b|\bmay\b|\bcould\b|\bor\b|\d\s*(?:to|-)\s*\d',context):
                        conditional=True
        authoritative=(field.outcome==O.VERIFIED and field.normalization_outcome==O.VERIFIED
            and not field.requires_human_interpretation and field.normalized_value is not None and not conditional)
        evidence=tuple(FactEvidence(source=span.source,quote=span.source_text)
            for check in field.evidence_checks for span in check.spans)
        facts.append(TrustedFact(path=field.field_path,raw_values=field.raw_values,
            normalized=field.normalized_value,status=field.outcome,
            raw_value_status=field.raw_value_outcome,normalization_status=field.normalization_outcome,
            authoritative=authoritative,evidence=evidence,reason_codes=field.reason_codes+(('CONDITIONAL_POLICY_SCOPE',) if conditional else ()),
            uncertainty=not authoritative,policy_interpretation_required=field.requires_human_interpretation or conditional))
    def group(prefix):
        return tuple(f for f in facts if f.path.startswith(prefix+'.'))
    supplier=TrustedSupplier(facts=group('supplier'),registrations=tuple(
        TrustedRegistration(kind=r.kind,facts=group(f'supplier.registrations[{i}]'))
        for i,r in enumerate(claims.supplier.registrations)))
    return TrustedFactsResult(document_id=meta.document_id,content_sha256=meta.content_sha256,
        completeness=ingested_document.completeness,extraction_complete=current.extraction_complete,
        supplier=supplier,line_items=tuple(TrustedLineItem(index=i,item_id=item.item_id,
            facts=group(f'line_items[{i}]'),tax=FactGroup(facts=group(f'line_items[{i}].tax')))
            for i,item in enumerate(claims.line_items)),
        payments=tuple(FactGroup(facts=group(f'payments[{i}]')) for i in range(len(claims.payments))),
        penalties=tuple(FactGroup(facts=group(f'penalties[{i}]')) for i in range(len(claims.penalties))),
        delivery=FactGroup(facts=group('delivery')),validity=FactGroup(facts=group('validity')),
        tax=FactGroup(facts=group('tax')),amounts=FactGroup(facts=group('amounts')),
        facts=tuple(facts),unresolved_fields=tuple(f.path for f in facts if f.uncertainty),
        conflicts=current.conflicts,verification_fingerprint=_fingerprint(current))


def fact_at(facts, path):
    return next((f for f in facts.facts if f.path==path),None)


def value_at(facts, path):
    fact=fact_at(facts,path)
    if not fact or not fact.authoritative or fact.normalized is None:
        return None
    value=fact.normalized
    return value.decimal_value if value.decimal_value is not None else value.text_value
