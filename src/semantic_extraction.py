"""Feature-flagged schema-validated LLM claims. No regex or evidence verification."""
import json
import math
from time import perf_counter, sleep

from pydantic import ValidationError

from src.document_sources import build_page_context, get_source_block, SourceLookupError
from src.extraction_prompt import SYSTEM_PROMPT, PROMPT_VERSION, SCHEMA_VERSION
from src.gemini_config import GeminiConfig, DEFAULT_GEMINI_MODEL
from src.gemini_provider import GeminiProvider
from src.ingestion_models import Completeness
from src.llm_contracts import (ExtractionErrorCode as Code, ExtractionError,
    ProviderFailure, StructuredExtractionResult)
from src.semantic_models import SemanticDocument, _semantic_fields
from src.local_validation import LocalValidationFailure,validation_boundary,diagnostic_for


def _unique_object(pairs):
    data={}
    for key,value in pairs:
        if key in data:
            raise ValueError('Duplicate JSON property')
        data[key]=value
    return data


def _claims_context(document):
    data=json.loads(build_page_context(document))
    for page,payload in zip(document.pages,data['pages'],strict=True):
        payload['page_id']=f'{document.metadata.document_id}:page:{page.page_number}'
        if page.ocr_text is not None:
            payload['ocr_text']=page.ocr_text
            payload['ocr_adapter_id']=page.ocr_adapter_id
    return json.dumps(data,ensure_ascii=False,separators=(',',':'))


def _structural_claim_check(claims,document):
    if claims.document_id!=document.metadata.document_id:
        raise ProviderFailure(Code.REFERENCE_INVALID)
    if claims.ingestion_completeness!=document.completeness or claims.interpretation_method!='FUTURE_PROVIDER':
        raise ProviderFailure(Code.TRUST_VIOLATION)
    for field in _semantic_fields(claims):
        if field.state=='VERIFIED' or field.normalized_value is not None or field.normalizer_version is not None:
            raise ProviderFailure(Code.TRUST_VIOLATION)
        if field.state=='UNSUPPORTED' and not field.ambiguity_reason:
            raise ProviderFailure(Code.INVALID_RESPONSE)
        if field.raw_values and not field.evidence:
            raise ProviderFailure(Code.INVALID_RESPONSE)
        for evidence in field.evidence:
            if evidence.verification!='UNCHECKED' or evidence.verifier_id is not None:
                raise ProviderFailure(Code.TRUST_VIOLATION)
            reference=evidence.source
            if not 1<=reference.page_number<=len(document.pages):
                raise ProviderFailure(Code.REFERENCE_INVALID)
            page=document.pages[reference.page_number-1]
            if (reference.text_origin=='OCR' and not page.ocr_text) or (reference.text_origin=='NATIVE' and not page.text.strip()):
                raise ProviderFailure(Code.REFERENCE_INVALID)
            if reference.block_id:
                try:
                    block=get_source_block(document,reference.page_number,reference.block_id)
                    if block.kind!='TEXT' or block.text_origin!=reference.text_origin:
                        raise ProviderFailure(Code.REFERENCE_INVALID)
                except SourceLookupError:
                    raise ProviderFailure(Code.REFERENCE_INVALID) from None
    # Deliberately no quote substring/semantic verification here: that is Step 5.


def extract_supplier_document(ingested_document,*,provider='gemini',config=None,
                              adapter=None,cloud_processing_authorized=False,
                              sleep_fn=sleep) -> StructuredExtractionResult:
    started=perf_counter()
    attempts=0
    raw=None
    usage=None
    model=DEFAULT_GEMINI_MODEL
    def result(status,*,claims=None,error=None,retryable=False,input_complete=None,diagnostic=None,validation_diagnostic=None):
        complete=ingested_document.completeness==Completeness.COMPLETE
        return StructuredExtractionResult(status=status,model_identifier=model,
            prompt_version=getattr(adapter,'prompt_version',PROMPT_VERSION),schema_version=SCHEMA_VERSION,
            document_id=ingested_document.metadata.document_id,
            content_sha256=ingested_document.metadata.content_sha256,
            ingestion_completeness=ingested_document.completeness,
            input_complete=complete if input_complete is None else input_complete,
            raw_claims_json=raw,validated_claims=claims,usage=usage,attempts=attempts,
            warnings=('LLM claims are unverified. Source quotes and semantic support require Step 5.',) if claims else (),
            errors=(ExtractionError(code=error,message='Gemini extraction did not complete: '+error.value+'. No legacy fallback was used.',retryable=retryable,diagnostic=diagnostic,validation_diagnostic=validation_diagnostic),) if error else (),
            duration_seconds=max(0,perf_counter()-started))
    try:
        config=config or GeminiConfig.from_env()
    except (ValueError,TypeError):
        return result('FAILED',error=Code.CONFIGURATION)
    model=config.model
    if provider!='gemini' or (adapter is not None and adapter.name!=provider):
        return result('FAILED',error=Code.UNSUPPORTED_PROVIDER)
    if not config.enabled:
        return result('DISABLED',error=Code.DISABLED)
    if ingested_document.completeness!=Completeness.COMPLETE:
        return result('INCOMPLETE',error=Code.INCOMPLETE_INPUT,input_complete=False)
    remote=adapter is None or adapter.is_remote
    if remote and (config.api_key is None or not config.api_key.get_secret_value().strip()):
        return result('FAILED',error=Code.MISSING_API_KEY)
    if remote and not cloud_processing_authorized:
        return result('FAILED',error=Code.CONSENT_REQUIRED)
    try:
        context=_claims_context(ingested_document)
        schema=SemanticDocument.model_json_schema()
        request_bytes=len((context+SYSTEM_PROMPT+json.dumps(schema,separators=(',',':'))).encode('utf-8'))
    except (ValueError,TypeError,RecursionError):
        return result('INCOMPLETE',error=Code.INCOMPLETE_INPUT,input_complete=False)
    # UTF-8 bytes + fixed envelope allowance is a conservative local token budget,
    # not an exact provider tokenizer. Never truncate or silently drop a page.
    if request_bytes>config.max_input_bytes or request_bytes+1024>config.max_input_tokens:
        return result('INCOMPLETE',error=Code.INPUT_LIMIT,input_complete=False)
    adapter=adapter or GeminiProvider()
    for index in range(config.max_retries+1):
        attempts+=1
        try:
            with validation_boundary('ADAPTER_BOUNDARY'):
                reply=adapter.generate(system_prompt=SYSTEM_PROMPT,context=context,schema=schema,config=config)
            usage=reply.usage
            if len(reply.text.encode('utf-8'))>config.max_response_bytes:
                return result('FAILED',error=Code.OUTPUT_LIMIT)
            raw=reply.text
            with validation_boundary('DOMAIN_JSON_SYNTAX'):
                payload=json.loads(raw,object_pairs_hook=_unique_object,
                    parse_constant=lambda value:(_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
            with validation_boundary('DOMAIN_PYDANTIC',model=SemanticDocument):
                claims=SemanticDocument.model_validate(payload)
            with validation_boundary('DOMAIN_STRUCTURE',model=SemanticDocument):
                _structural_claim_check(claims,ingested_document)
            return result('SCHEMA_VALIDATED',claims=claims)
        except LocalValidationFailure as error:
            return result('FAILED',error=Code.INVALID_RESPONSE,validation_diagnostic=error.validation_diagnostic)
        except (ValidationError,ValueError,TypeError,RecursionError) as error:
            return result('FAILED',error=Code.INVALID_RESPONSE,validation_diagnostic=diagnostic_for('ADAPTER_BOUNDARY',error))
        except ProviderFailure as error:
            if not error.retryable or index==config.max_retries:
                return result('FAILED',error=error.code,retryable=error.retryable,diagnostic=error.diagnostic,
                    validation_diagnostic=error.validation_diagnostic)
            delay=min(0.5*(2**index),config.max_backoff_seconds)
            if error.retry_after is not None:
                if not math.isfinite(error.retry_after) or error.retry_after>config.max_backoff_seconds:
                    return result('FAILED',error=error.code,retryable=True,diagnostic=error.diagnostic,
                        validation_diagnostic=error.validation_diagnostic)
                delay=max(delay,max(0,error.retry_after))
            sleep_fn(delay)
        except Exception:
            return result('FAILED',error=Code.PROVIDER_FAILURE)
