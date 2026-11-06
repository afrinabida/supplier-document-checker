"""Explicit minimal JSON entry point using the existing guarded AI LangGraph."""
from src.pdf_ingestion import ingest_pdf
from src.minimal_json_adapter import MinimalJsonAdapter
from src.ai_graph_workflow import ai_sqlite_service


def run_ai_assisted(pdf,purchase_request,*,thread_id,storage_root,config,cloud_processing_authorized=False,transport=None,filename=None):
    # The configured feature/model remain operator-controlled; no hidden fallback.
    adapter=MinimalJsonAdapter(ingest_pdf(pdf),transport=transport)
    single_attempt=config.model_copy(update={'max_retries':0})
    with ai_sqlite_service(storage_root,config=single_attempt,provider=adapter,
        cloud_processing_authorized=cloud_processing_authorized) as service:
        return service.start(pdf,purchase_request,thread_id=thread_id,filename=filename)


def evaluation_id(pdf,purchase_request,*,filename,config):
    """Stable presentation run identity; repeated clicks reuse the saved workflow."""
    from hashlib import sha256
    from src.ai_graph_artifacts import canonical_bytes
    from src.ai_graph_workflow import implementation_hash
    from src.pdf_ingestion import safe_filename
    payload=dict(source=sha256(pdf).hexdigest(),filename=safe_filename(filename),
        purchase_request=purchase_request,configuration=config.model_copy(update={'max_retries':0}).model_dump(mode='json'),
        implementation=implementation_hash(),contract=MinimalJsonAdapter.contract_version)
    return 'minimal_'+sha256(canonical_bytes(payload)).hexdigest()[:48]
