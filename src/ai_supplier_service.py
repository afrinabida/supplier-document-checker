"""Opt-in LLM service; no graph, UI, database, logging or regex fallback."""
from pathlib import Path
from src.gemini_config import GeminiConfig
from src.pdf_ingestion import ingest_pdf, IngestionLimits
from src.semantic_extraction import extract_supplier_document
from src.evidence_verification import verify_extraction
from src.verification_models import VerificationConfig
from src.trusted_facts import build_trusted_facts, TrustedFactsError
from src.semantic_matching import match_trusted_items
from src.semantic_rules import evaluate_trusted_facts
from src.trusted_models import SupplierCheckResult
from src.rules_config import CONFIG


def check_supplier_document_ai(pdf,purchase_request,company_rules=CONFIG,*,provider=None,
        config: GeminiConfig | None=None,cloud_processing_authorized=False):
    """Return an immutable automated assessment, never a human override.

    provider is an optional Step 4 ExtractionProvider adapter (offline mocks are
    supported). The checked-in RulesConfig is the only supported policy version;
    arbitrary rules text/config cannot silently change evaluation thresholds.
    """
    configuration_error=False
    if config is None:
        try: config=GeminiConfig.from_env()
        except (ValueError,TypeError):
            config=GeminiConfig();configuration_error=True
    document=None
    def failure(status,code,model=None):
        return SupplierCheckResult(decision='Needs a person to check',processing_status=status,
            provider='gemini',model_identifier=model or config.model,
            document_id=document.metadata.document_id if document else None,error_codes=(code,),
            uncertainty=('NO_COMPLETE_VERIFIED_ASSESSMENT',),
            audit={'fallback':'none','pipeline_version':'semantic-service-v1.0'})
    if configuration_error:
        return failure('INPUT_FAILED','INVALID_PROVIDER_CONFIGURATION')
    if type(company_rules) is not type(CONFIG) or company_rules!=CONFIG:
        return failure('INPUT_FAILED','UNSUPPORTED_POLICY_CONFIGURATION')
    try:
        # Snapshot once so ingestion, provider and verification share the same bytes.
        if isinstance(pdf,bytes): data=pdf
        else:
            with Path(pdf).open('rb') as stream:
                data=stream.read(IngestionLimits().max_file_bytes+1)
        document=ingest_pdf(data)
    except (OSError,ValueError,TypeError):
        return failure('INPUT_FAILED','PDF_INPUT_UNAVAILABLE')
    extraction=extract_supplier_document(document,adapter=provider,config=config,
        cloud_processing_authorized=cloud_processing_authorized)
    if extraction.status!='SCHEMA_VALIDATED':
        return failure('EXTRACTION_FAILED',extraction.errors[0].code.value if extraction.errors else 'EXTRACTION_INCOMPLETE',extraction.model_identifier)
    try:
        report=verify_extraction(document,extraction,config=VerificationConfig(require_source_bytes=True),source_bytes=data)
        facts=build_trusted_facts(document,extraction,report,source_bytes=data)
        matching=match_trusted_items(facts,purchase_request,document)
        assessment,evaluations,uncertainty=evaluate_trusted_facts(facts,document,purchase_request,matching)
    except TrustedFactsError:
        return failure('TRUST_FAILED','VERIFICATION_BINDING_FAILED')
    except (ValueError,TypeError,ArithmeticError,AttributeError):
        return failure('INPUT_FAILED','UNSUPPORTED_VERIFIED_INPUT')
    return SupplierCheckResult(decision=assessment.decision,
        processing_status='COMPLETE' if assessment.decision=='Approve' else 'REVIEW_REQUIRED',
        provider=extraction.provider,model_identifier=extraction.model_identifier,
        document_id=facts.document_id,trusted_facts=facts,matching=matching,
        automated_assessment=assessment,item_evaluations=evaluations,uncertainty=uncertainty,
        findings=tuple(dict(rule_id=r.rule_id,status=r.status,category=r.category) for r in assessment.evaluation.results),
        audit={'fallback':'none','pipeline_version':'semantic-service-v1.0','policy_version':'checked-in-rules-v1',
            'verification_fingerprint':facts.verification_fingerprint,'content_sha256':facts.content_sha256,
            'prompt_version':extraction.prompt_version,'schema_version':extraction.schema_version,
            'transport':'injected-offline' if provider is not None and not provider.is_remote else 'remote',
            'cloud_processing_authorized':str(bool(cloud_processing_authorized)).lower()})
