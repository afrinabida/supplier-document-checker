"""Explicit, synthetic-only single-stage live request diagnostics: A, B or C.

No stage chaining, arbitrary PDF input, retries, graph execution or persistence.
"""
import argparse
from hashlib import sha256
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import sys
from time import perf_counter
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from src.gemini_provider import GeminiProvider
from src.gemini_config import GeminiConfig
from src.gemini_schema import gemini_response_schema, WIRE_SCHEMA_VERSION
from src.gemini_smoke import smoke_config, synthetic_smoke_document
from src.semantic_extraction import extract_supplier_document, _unique_object
from src.semantic_models import SemanticDocument
from src.llm_contracts import ProviderFailure
from src.llm_contracts import ExtractionErrorCode
from src.extraction_prompt import PROMPT_VERSION, SCHEMA_VERSION
from src.gemini_isolation import VARIANTS,MAPPING_VERSION,isolation_schema,parse_supplier_reply
from src.gemini_compatibility import (COMPAT_VARIANT,MAPPING_VERSION as GROUP_MAPPING_VERSION,
    compatibility_schema,compatibility_context,parse_grouped)
from src.gemini_minimal_live import STAGE as MINIMAL_STAGE,MODEL as MINIMAL_MODEL,run_minimal_live


class TinyJson(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    ok: StrictBool


class SingleStage(argparse.Action):
    def __call__(self,parser,namespace,value,option_string=None):
        if getattr(namespace,self.dest,None) is not None: parser.error('Select exactly one stage.')
        setattr(namespace,self.dest,value)


def diagnostic_timeout(value):
    """Use existing finite bounds without changing shared production defaults."""
    try: return GeminiConfig(timeout_seconds=value).timeout_seconds
    except (ValueError,TypeError):
        raise argparse.ArgumentTypeError('Use a finite timeout from 0.1 through 120 seconds.') from None


def with_diagnostic_timeout(config,seconds=None):
    if seconds is None: return config
    return config.model_copy(update={'timeout_seconds':diagnostic_timeout(seconds)})


def diagnostic_model(value):
    try: return GeminiConfig(model=value).model
    except (ValueError,TypeError): raise argparse.ArgumentTypeError('Use a valid diagnostic model ID.') from None


def request_summary(stage,config):
    if stage not in ('A','B','C',*VARIANTS,COMPAT_VARIANT,MINIMAL_STAGE): raise ValueError('Select a diagnostic stage or variant.')
    schema=compatibility_schema() if stage==COMPAT_VARIANT else isolation_schema(stage) if stage in VARIANTS else ({'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
        if stage=='B' else gemini_response_schema(SemanticDocument.model_json_schema()) if stage=='C' else None)
    supplier_settings=stage in ('C','TINY_C','REDUCED_C',COMPAT_VARIANT,MINIMAL_STAGE)
    serialized=json.dumps(schema,separators=(',',':')).encode() if schema else b''
    return dict(api_method='models.generate_content',api='Gemini Developer API',
        automatic_function_calling=False,tools_present=False,function_declarations_present=False,
        response_schema_parameter='response_json_schema' if schema else None,
        response_mime_type='application/json' if schema or stage==MINIMAL_STAGE else None,
        schema_bytes=len(serialized),schema_sha256=sha256(serialized).hexdigest() if schema else None,
        max_output_tokens=config.max_output_tokens if supplier_settings else min(config.max_output_tokens,2048),
        system_instruction_present=supplier_settings,temperature=0 if supplier_settings else None,
        application_retries=0,sdk_max_attempts=1,
        client_timeout_seconds=config.timeout_seconds,
        sdk_timeout_milliseconds=round(config.timeout_seconds*1000),
        server_timeout_header_seconds=math.ceil(round(config.timeout_seconds*1000)/1000))


def _run_compatibility(config,provider):
    config=config.model_copy(update={'max_retries':0});started=perf_counter()
    result=dict(stage=COMPAT_VARIANT,status='FAILED',model=config.model,sdk_version=version('google-genai'),
        prompt_version=PROMPT_VERSION,schema_version=SCHEMA_VERSION,wire_schema_version=GROUP_MAPPING_VERSION,
        request=request_summary(COMPAT_VARIANT,config),attempts=0,error_codes=[],diagnostics=[],
        response_received=False,output_check_passed=False,usage=None,evidence_stage='UNVERIFIED_LLM_CLAIMS',
        mapping='Every original supplier group decoded without projection; original local and structural validation',
        compatibility_outcome='NOT_ESTABLISHED')
    document=synthetic_smoke_document()
    try:
        context=compatibility_context(document)
        from src.extraction_prompt import SYSTEM_PROMPT
        size=len((context+SYSTEM_PROMPT+json.dumps(compatibility_schema(),separators=(',',':'))).encode())
        if size>config.max_input_bytes or size+1024>config.max_input_tokens:
            result.update(error_codes=['INPUT_LIMIT'],duration_seconds=max(0,perf_counter()-started))
            return result
        result['attempts']=1
        reply=provider.generate_compatibility(context=context,config=config)
        result['response_received']=True
        result['usage']=reply.usage.model_dump() if reply.usage else None
        if len(reply.text.encode())>config.max_response_bytes: raise ProviderFailure(ExtractionErrorCode.OUTPUT_LIMIT)
        parse_grouped(reply.text,document)
        result.update(status='SUCCESS',output_check_passed=True,compatibility_outcome='RESPONSE_VALIDATED')
    except ProviderFailure as error:
        result.update(error_codes=[error.code.value],diagnostics=[error.diagnostic.model_dump(mode='json')] if error.diagnostic else [])
        if error.diagnostic and error.diagnostic.http_status==503:
            result['compatibility_outcome']='INCONCLUSIVE_PROVIDER_UNAVAILABLE'
        elif error.diagnostic and error.diagnostic.http_status==400:
            result['compatibility_outcome']='REQUEST_REJECTED_CAUSE_NOT_ESTABLISHED'
    except (ValidationError,ValueError,TypeError):
        result.update(error_codes=['INVALID_RESPONSE'],compatibility_outcome='LOCAL_VALIDATION_FAILED')
    if any(d['http_status'] is not None for d in result['diagnostics']) or 'OUTPUT_LIMIT' in result['error_codes']:
        result['response_received']=True
    result['duration_seconds']=max(0,perf_counter()-started)
    return result


def _run_isolation(stage,config,provider):
    from src.semantic_extraction import _claims_context
    config=config.model_copy(update={'max_retries':0});started=perf_counter()
    result=dict(stage=stage,status='FAILED',model=config.model,sdk_version=version('google-genai'),
        prompt_version=PROMPT_VERSION if stage!='FULL_MIN' else None,
        schema_version='tiny-json-v1' if stage=='TINY_C' else SCHEMA_VERSION,
        wire_schema_version=MAPPING_VERSION if stage=='REDUCED_C' else 'tiny-json-v1' if stage=='TINY_C' else WIRE_SCHEMA_VERSION,
        request=request_summary(stage,config),attempts=0,error_codes=[],diagnostics=[],
        response_received=False,output_check_passed=False,usage=None,
        evidence_stage='UNVERIFIED_LLM_CLAIMS',mapping='claims_json -> full SemanticDocument' if stage=='REDUCED_C' else 'identity',
        validation_contract='TinyJson only; no supplier facts' if stage=='TINY_C' else 'Original SemanticDocument + structural trust checks')
    document=synthetic_smoke_document()
    try:
        result['attempts']=1
        reply=provider.generate_isolation(variant=stage,context=_claims_context(document),config=config)
        result['response_received']=True
        result['usage']=reply.usage.model_dump() if reply.usage else None
        if len(reply.text.encode())>config.max_response_bytes: raise ProviderFailure(ExtractionErrorCode.OUTPUT_LIMIT)
        if stage=='TINY_C':
            data=json.loads(reply.text,object_pairs_hook=_unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
            valid=TinyJson.model_validate(data).ok is True
        else:
            parse_supplier_reply(reply.text,document,enveloped=stage=='REDUCED_C');valid=True
        result.update(status='SUCCESS' if valid else 'FAILED',output_check_passed=valid,
            error_codes=[] if valid else ['UNEXPECTED_SYNTHETIC_RESPONSE'])
    except ProviderFailure as error:
        result.update(error_codes=[error.code.value],diagnostics=[error.diagnostic.model_dump(mode='json')] if error.diagnostic else [])
    except (ValidationError,ValueError,TypeError): result['error_codes']=['INVALID_RESPONSE']
    if any(d['http_status'] is not None for d in result['diagnostics']) or 'OUTPUT_LIMIT' in result['error_codes']:
        result['response_received']=True
    result['duration_seconds']=max(0,perf_counter()-started)
    return result


def _run_selected_raw(stage,config,provider):
    """Internal runner; CLI owns live authorization, real SDK owns pytest block."""
    if stage==MINIMAL_STAGE: return run_minimal_live(config,provider,request_summary(stage,config))
    if stage==COMPAT_VARIANT: return _run_compatibility(config,provider)
    if stage in VARIANTS: return _run_isolation(stage,config,provider)
    if stage not in ('A','B','C'): raise ValueError('Select one stage.')
    config=config.model_copy(update={'max_retries':0})
    started=perf_counter()
    result=dict(stage=stage,status='FAILED',model=config.model,sdk_version=version('google-genai'),
        prompt_version=PROMPT_VERSION if stage=='C' else 'synthetic-request-diagnostic-v1',
        schema_version=SCHEMA_VERSION if stage=='C' else 'tiny-json-v1' if stage=='B' else None,
        wire_schema_version=WIRE_SCHEMA_VERSION if stage=='C' else None,
        request=request_summary(stage,config),attempts=0,error_codes=[],diagnostics=[],
        response_received=False,output_check_passed=False,usage=None)
    if stage=='C':
        extraction=extract_supplier_document(synthetic_smoke_document(),config=config,adapter=provider,
            cloud_processing_authorized=True)
        result.update(status='SUCCESS' if extraction.status=='SCHEMA_VALIDATED' else 'FAILED',
            extraction_status=extraction.status,attempts=extraction.attempts,
            error_codes=[e.code.value for e in extraction.errors],
            diagnostics=[e.diagnostic.model_dump(mode='json') for e in extraction.errors if e.diagnostic],
            output_check_passed=extraction.status=='SCHEMA_VALIDATED',
            evidence_stage=extraction.evidence_stage,
            response_received=extraction.status=='SCHEMA_VALIDATED' or extraction.raw_claims_json is not None,
            usage=extraction.usage.model_dump() if extraction.usage else None)
    else:
        try:
            result['attempts']=1
            reply=provider.generate_minimal(stage=stage,config=config)
            result['response_received']=True
            result['usage']=reply.usage.model_dump() if reply.usage else None
            if stage=='A': valid=reply.text.strip()=='OK'
            else:
                # Strict response validation is local and never dumps input on error.
                data=json.loads(reply.text,object_pairs_hook=_unique_object,
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
                parsed=TinyJson.model_validate(data);valid=parsed.ok is True
            result.update(status='SUCCESS' if valid else 'FAILED',output_check_passed=valid,
                error_codes=[] if valid else ['UNEXPECTED_SYNTHETIC_RESPONSE'])
        except ProviderFailure as error:
            result.update(error_codes=[error.code.value],diagnostics=[error.diagnostic.model_dump(mode='json')] if error.diagnostic else [])
        except (ValidationError,ValueError,TypeError): result['error_codes']=['INVALID_SYNTHETIC_RESPONSE']
    # HTTP 400 is an error response received, not a successful generation. Output
    # limit/refusal codes also imply a response; do not confuse them with HTTP 400.
    if any(d['http_status'] is not None for d in result['diagnostics']) or any(
        code in ('OUTPUT_LIMIT','RESPONSE_BLOCKED','INVALID_RESPONSE') for code in result['error_codes']):
        result['response_received']=True
    result['duration_seconds']=max(0,perf_counter()-started)
    return result


def _run_selected(stage,config,provider):
    result=_run_selected_raw(stage,config,provider)
    diagnostics=result.get('diagnostics',[])
    statuses={d.get('http_status') for d in diagnostics}
    categories={d.get('category') for d in diagnostics}
    if 503 in statuses: outcome='INCONCLUSIVE_PROVIDER_UNAVAILABLE'
    elif 504 in statuses: outcome='INCONCLUSIVE_PROVIDER_DEADLINE'
    elif 400 in statuses:
        outcome='SCHEMA_REJECTED' if categories & {'SCHEMA_COMPLEXITY','RESPONSE_SCHEMA'} else 'REQUEST_REJECTED_CAUSE_NOT_ESTABLISHED'
    elif result['status']=='SUCCESS' or result.get('diagnostic_completed'): outcome='RESPONSE_VALIDATED'
    elif any(d.get('timeout_origin')=='LOCAL_CLIENT' for d in diagnostics): outcome='INCONCLUSIVE_LOCAL_TIMEOUT'
    else: outcome='NOT_ESTABLISHED'
    result['request_outcome']=outcome
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',choices=('A','B','C',*VARIANTS,COMPAT_VARIANT,MINIMAL_STAGE),action=SingleStage)
    parser.add_argument('--preflight-only',action='store_true',help='Read-only model listing; no generation.')
    parser.add_argument('--listed-fallback-model',type=diagnostic_model,
        help='Explicit diagnostic model choice, checked against a fresh API model list; no automatic retry or switch.')
    parser.add_argument('--run-live',action='store_true')
    parser.add_argument('--allow-cloud-processing',action='store_true')
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--timeout-seconds',type=diagnostic_timeout,
        help='Diagnostic-only timeout override in seconds (0.1–120); production default unchanged.')
    parser.add_argument('--model',type=diagnostic_model,help='Diagnostic-only model override; .env and production default unchanged.')
    args=parser.parse_args(argv)
    if args.preflight_only and (args.stage or args.listed_fallback_model or args.run_live):
        parser.error('Preflight-only cannot be combined with generation selection or live generation authorization.')
    if not args.preflight_only and not args.stage: parser.error('Select exactly one stage or --preflight-only.')
    if args.listed_fallback_model and args.model: parser.error('Select --model or --listed-fallback-model, not both.')
    if args.stage==MINIMAL_STAGE and (args.model!=MINIMAL_MODEL or args.listed_fallback_model):
        parser.error('MINIMAL_JSON_LIVE requires explicit --model gemini-3.5-flash-lite and no fallback.')
    if (not args.preflight_only and (not args.run_live or not args.allow_cloud_processing)) or 'pytest' in sys.modules or 'PYTEST_CURRENT_TEST' in os.environ:
        print(json.dumps(dict(stage=args.stage,status='NOT_RUN',reason='Explicit live authorization is required outside pytest.',attempts=0)))
        return 0
    try: config=with_diagnostic_timeout(smoke_config(args.env_file,single_attempt=True),args.timeout_seconds)
    except (ValueError,TypeError):
        print(json.dumps(dict(stage=args.stage,status='NOT_RUN',reason='INVALID_CONFIGURATION',attempts=0)))
        return 1
    if args.model is not None: config=config.model_copy(update={'model':args.model})
    if (not config.enabled and not args.preflight_only) or config.api_key is None or not config.api_key.get_secret_value().strip():
        print(json.dumps(dict(stage=args.stage,status='NOT_RUN',reason='Enable extraction and configure the credential securely.',attempts=0)))
        return 0
    from src.gemini_preflight import model_preflight,listed_fallback
    if args.preflight_only:
        result=model_preflight(config)
    elif args.listed_fallback_model:
        preflight=model_preflight(config)
        try: selected=listed_fallback(config,args.listed_fallback_model,preflight)
        except ValueError:
            result=dict(status='NOT_RUN',attempts=0,reason='FALLBACK_NOT_VERIFIED',preflight=preflight)
        else:
            result=_run_selected(args.stage,selected,GeminiProvider())
            result.update(configured_model=config.model,explicit_fallback=True,preflight=preflight)
    else:
        result=_run_selected(args.stage,config,GeminiProvider())
    # Defense in depth for an accidentally configured model value equal to a key.
    output=json.dumps(result)
    secret=config.api_key.get_secret_value()
    print(output.replace(secret,'<redacted>') if secret else output)
    return 0 if result['status']=='SUCCESS' or result.get('diagnostic_completed') else 1


if __name__=='__main__': raise SystemExit(main())
