"""Single UI evaluation entry, backed by the unchanged fixed LangGraph services."""
from src.evaluation_store import validate_upload


STEP_LABELS = {
    'validate_pdf': '1. Validate supplier PDF',
    'ingest': '1. Validate supplier PDF',
    'extract': '2. Extract supplier information',
    'validate': '3. Validate extracted fields and exact evidence',
    'verify': '3. Verify source evidence',
    'trust': '3. Build trusted facts',
    'match_pr': '4. Compare with purchase request',
    'arithmetic': '5. Check amounts and VAT arithmetic',
    'rules': '5. Validate company rules',
    'decide': '6. Generate deterministic decision',
    'human_review': '7. Await human review',
    'finalize': '8. Save outcome and workflow state',
}


def start_evaluation(name, data, *, config, batch, cloud_processing_authorized=False,
                     fresh=False, on_stage=None, thread_id=None):
    """No fallback: operator configuration selects a persisted graph before execution."""
    if on_stage:
        on_stage('validate_pdf')
    validate_upload(name, data)
    if config.enabled:
        from ui.ai_processing import start_document
        return 'AI-assisted', start_document(data, name, config=config,
            cloud_processing_authorized=cloud_processing_authorized, fresh=fresh, on_stage=on_stage,
            thread_id=thread_id)
    from src.graph_workflow import sqlite_service, store_upload, evaluation_thread_id
    from src.workflow import SAMPLE_DIR
    source = store_upload(data, name)
    thread = thread_id or evaluation_thread_id(source, SAMPLE_DIR/'purchase-request.csv', namespace='ui_' + batch)
    with sqlite_service() as service:
        return 'Deterministic', service.start(source, thread_id=thread, filename=name, on_stage=on_stage)
