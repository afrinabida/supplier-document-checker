"""Explicit synthetic single-request diagnostic; no rules, storage or production routing."""
from collections import Counter
from importlib.metadata import version
import json
from time import perf_counter
from pydantic import ValidationError
from src.gemini_minimal_contract import proposal,parse_minimal,response_model,VERSION
from src.semantic_extraction import _unique_object
from src.llm_contracts import ProviderFailure
from src.evidence_verification import verify_extraction
from src.verification_models import VerificationConfig
from src.trusted_facts import build_trusted_facts,TrustedFactsError

STAGE='MINIMAL_JSON_LIVE'
MODEL='gemini-3.5-flash-lite'


def synthetic_source():
    """Fixed in-memory synthetic PDF; no file/path argument or runtime writes."""
    import pymupdf
    from src.pdf_ingestion import ingest_pdf
    with pymupdf.open() as pdf:
        page=pdf.new_page()
        page.insert_text((40,50),'Example Supplier Ltd\nQUOTATION\nItem A: 2 pieces at BDT 10.00 each.\nDelivery within 14 days of purchase order.\nNo penalty applies for late delivery.')
        data=pdf.tobytes(no_new_id=True)
    return ingest_pdf(data,'synthetic-minimal-diagnostic.pdf',document_type_hint='quotation'),data


def run_minimal_live(config,provider,request):
    started=perf_counter()
    result=dict(stage=STAGE,status='FAILED',model=config.model,sdk_version=version('google-genai'),
        contract_version=VERSION,request=request,attempts=0,error_codes=[],diagnostics=[],
        response_received=False,output_check_passed=False,usage=None,diagnostic_completed=False,
        provider_generation=dict(status='NOT_RUN'),json_parsing=dict(status='NOT_RUN'),
        local_schema_validation=dict(status='NOT_RUN'),claims_mapping=dict(status='NOT_RUN'),
        evidence_verification=dict(status='NOT_RUN'),trusted_facts=dict(status='NOT_RUN'),
        unresolved=True,human_review_required=True,decision='NOT_EVALUATED')
    def finish():
        result['duration_seconds']=max(0,perf_counter()-started)
        return result
    if config.model!=MODEL:
        result['error_codes']=['DIAGNOSTIC_MODEL_REQUIRED'];return finish()
    config=config.model_copy(update={'max_retries':0})
    document,source_bytes=synthetic_source()
    plan=proposal(document)
    size=len((plan['context']+plan['system_instruction']).encode())
    if size>config.max_input_bytes or size+1024>config.max_input_tokens:
        result['error_codes']=['INPUT_LIMIT'];return finish()
    try:
        result['attempts']=1
        reply=provider.generate_minimal_json(system_prompt=plan['system_instruction'],context=plan['context'],config=config)
        result.update(response_received=True,provider_generation=dict(status='SUCCESS'))
        result['usage']=reply.usage.model_dump() if reply.usage else None
    except ProviderFailure as error:
        result.update(error_codes=[error.code.value],provider_generation=dict(status='FAILED'),
            diagnostics=[error.diagnostic.model_dump(mode='json')] if error.diagnostic else [])
        result['response_received']=bool(error.diagnostic and error.diagnostic.http_status is not None) or error.code.value in ('OUTPUT_LIMIT','RESPONSE_BLOCKED','INVALID_RESPONSE')
        return finish()
    # Do not serialize a response, validation error, verification report or fact value.
    if len(reply.text.encode())>min(config.max_response_bytes,1_000_000):
        result.update(error_codes=['OUTPUT_LIMIT'],json_parsing=dict(status='NOT_RUN',reason='RESPONSE_LIMIT'))
        return finish()
    try:
        json.loads(reply.text,object_pairs_hook=_unique_object,
            parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite JSON forbidden')))
        result['json_parsing']=dict(status='SUCCESS')
    except (ValueError,TypeError,RecursionError):
        result.update(error_codes=['INVALID_JSON'],json_parsing=dict(status='FAILED'))
        return finish()
    try:
        response_model().model_validate_json(reply.text)
        result['local_schema_validation']=dict(status='SUCCESS',supplier_groups=17)
    except (ValidationError,ValueError,TypeError):
        result.update(error_codes=['INVALID_RESPONSE'],local_schema_validation=dict(status='FAILED'))
        return finish()
    try:
        claims=parse_minimal(reply.text,document)
        result['claims_mapping']=dict(status='SUCCESS')
    except (ValueError,TypeError,ProviderFailure):
        result.update(error_codes=['CLAIM_MAPPING_INVALID'],claims_mapping=dict(status='FAILED',reason='SEMANTIC_OR_EXACT_SOURCE_VALIDATION_FAILED'))
        return finish()
    try:
        report=verify_extraction(document,claims,source_bytes=source_bytes,
            config=VerificationConfig(require_source_bytes=True))
        result['evidence_verification']=dict(status='SUCCESS',identity_outcome=report.identity_outcome.value,
            hash_check=report.hash_check,field_outcomes=dict(Counter(f.outcome.value for f in report.fields)),
            eligible_field_count=sum(f.eligible_for_future_use for f in report.fields),
            unresolved_field_count=sum(not f.eligible_for_future_use for f in report.fields),
            required_fields_ready=report.coverage.required_fields_ready,
            required_field_count=len(report.coverage.required_fields),
            required_verified_count=len(report.coverage.required_verified),
            missing_required_count=len(report.coverage.missing_from_output),
            conflict_count=len(report.conflicts),needs_human_review=report.needs_human_review)
    except (ValueError,TypeError,ArithmeticError):
        result.update(error_codes=['VERIFICATION_FAILED'],evidence_verification=dict(status='FAILED'))
        return finish()
    try:
        facts=build_trusted_facts(document,claims,report,source_bytes=source_bytes)
        result['trusted_facts']=dict(status='SUCCESS',authoritative_fact_count=sum(f.authoritative for f in facts.facts),
            unresolved_field_count=len(facts.unresolved_fields),conflict_count=len(facts.conflicts),
            verification_binding_validated=True)
    except (TrustedFactsError,ValueError,TypeError,ArithmeticError):
        result.update(error_codes=['TRUST_VALIDATION_FAILED'],trusted_facts=dict(status='FAILED'))
        return finish()
    unresolved=report.needs_human_review or not report.coverage.required_fields_ready or bool(facts.unresolved_fields)
    result.update(status='UNRESOLVED' if unresolved else 'SUCCESS',unresolved=unresolved,
        human_review_required=unresolved,diagnostic_completed=True,output_check_passed=True)
    return finish()
