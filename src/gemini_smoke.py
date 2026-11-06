"""Explicit opt-in synthetic-only live smoke command. No arbitrary file argument."""
import argparse
import json
import os
import sys
from pathlib import Path
from importlib.metadata import version

from src.gemini_config import GeminiConfig
from src.pdf_ingestion import ingest_pdf
from src.semantic_extraction import extract_supplier_document
from src.gemini_schema import WIRE_SCHEMA_VERSION, gemini_response_schema
from src.semantic_models import SemanticDocument


def smoke_config(env_file=None,*,single_attempt=False):
    """Explicit env-file opt-in; process environment takes precedence. No mutation."""
    values={}
    allowed={'ENABLE_GEMINI_EXTRACTION','GEMINI_API_KEY','GEMINI_MODEL','GEMINI_TIMEOUT_SECONDS','GEMINI_MAX_RETRIES'}
    if env_file is not None:
        try:
            for line in Path(env_file).read_text(encoding='utf-8-sig').splitlines():
                line=line.strip()
                if not line or line.startswith('#'): continue
                if line.startswith('export '): line=line[7:]
                if '=' not in line: raise ValueError
                key,value=line.split('=',1);key=key.strip();value=value.strip()
                if key not in allowed: continue
                if value.startswith(('\"',"'")):
                    if len(value)<2 or value[-1]!=value[0]: raise ValueError
                    value=value[1:-1]
                else: value=value.split(' #',1)[0].rstrip()
                values[key]=value
        except (OSError,UnicodeError,ValueError):
            raise ValueError('Cannot load explicit smoke configuration.') from None
    values.update(os.environ)
    config=GeminiConfig.from_env(environ=values)
    return config.model_copy(update={'max_retries':0}) if single_attempt else config


def synthetic_smoke_document():
    """The same non-sensitive quotation for smoke and selected diagnostic C."""
    import pymupdf
    with pymupdf.open() as pdf:
        page=pdf.new_page()
        page.insert_text((40,50),'Example Supplier Ltd\nQUOTATION\nItem A: 2 pieces at BDT 10.00 each.\nDelivery within 14 days of purchase order.\nNo penalty applies for late delivery.')
        # A stable synthetic hash permits controlled A/B/C reproduction; supplied
        # documents are never changed. Do not create a random PDF trailer ID.
        return ingest_pdf(pdf.tobytes(no_new_id=True),'synthetic-smoke.pdf',document_type_hint='quotation')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live',action='store_true')
    parser.add_argument('--allow-cloud-processing',action='store_true')
    parser.add_argument('--env-file',type=Path,help='Explicit local configuration file; environment variables override it.')
    parser.add_argument('--single-attempt',action='store_true',help='Disable application retries for one generation request.')
    args=parser.parse_args(argv)
    if not args.run_live or not args.allow_cloud_processing or 'PYTEST_CURRENT_TEST' in os.environ or 'pytest' in sys.modules:
        print('Live Gemini smoke test not run: explicit opt-in and cloud authorization are required outside pytest.')
        return 0
    try:
        config=smoke_config(args.env_file,single_attempt=args.single_attempt)
    except ValueError:
        print('Live Gemini smoke test not run: invalid configuration.')
        return 1
    if not config.enabled or config.api_key is None:
        print('Live Gemini smoke test not run: enable extraction and supply the API key securely.')
        return 0
    document=synthetic_smoke_document()
    result=extract_supplier_document(document,config=config,cloud_processing_authorized=True)
    schema=gemini_response_schema(SemanticDocument.model_json_schema())
    print(json.dumps(dict(model=result.model_identifier,status=result.status,
        duration_seconds=result.duration_seconds,attempts=result.attempts,
        line_item_count=len(result.validated_claims.line_items) if result.validated_claims else 0,
        evidence_stage=result.evidence_stage,error_codes=[e.code.value for e in result.errors],
        diagnostics=[e.diagnostic.model_dump(mode='json') for e in result.errors if e.diagnostic],
        sdk_version=version('google-genai'),prompt_version=result.prompt_version,schema_version=result.schema_version,
        wire_schema_version=WIRE_SCHEMA_VERSION,wire_schema_bytes=len(json.dumps(schema,separators=(',',':')).encode()),
        usage=result.usage.model_dump() if result.usage else None)))
    return 0 if result.status=='SCHEMA_VALIDATED' else 1


if __name__=='__main__':
    raise SystemExit(main())
