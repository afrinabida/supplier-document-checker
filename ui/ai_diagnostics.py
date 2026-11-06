"""Display fixed diagnostic metadata, never provider text or exception strings."""
from pydantic import ValidationError
from src.llm_contracts import ExtractionErrorCode, ProviderDiagnostic
from src.local_validation import LocalValidationDiagnostic


def failure_details(state, materials):
    result = {'category': 'Extraction failure', 'code': 'UNKNOWN', 'stage': 'unknown'}
    safe = state.get('safe_error') or {}
    try:
        result['code'] = ExtractionErrorCode(safe.get('code')).value
    except (ValueError, TypeError):
        pass
    if safe.get('stage') in ('ingest','extract','verify','trust','match_pr','rules','decide'):
        result['stage'] = safe['stage']
    for error in materials.get('extraction',{}).get('errors',[])[:1]:
        try:
            diagnostic = ProviderDiagnostic.model_validate(error.get('diagnostic'))
            result.update(category=diagnostic.category, http_status=diagnostic.http_status,
                provider_status=diagnostic.provider_status, origin=diagnostic.timeout_origin,
                diagnostic_code=diagnostic.detail)
        except (ValidationError, TypeError):
            pass
        try:
            local = LocalValidationDiagnostic.model_validate(error.get('validation_diagnostic'))
            category = {'JSON_SYNTAX':'Invalid JSON','JSON_STRUCTURE':'Invalid JSON structure',
                'RESPONSE_PYDANTIC':'Response contract validation',
                'DOCUMENT_IDENTITY':'Document identity mismatch',
                'SOURCE_QUOTE':'Source evidence validation','SOURCE_PAGE':'Source evidence validation',
                'SOURCE_BLOCK':'Source evidence validation','DOMAIN_MAPPING':'Domain mapping',
                'DOMAIN_SERIALIZATION':'Domain mapping'}.get(local.stage,'Local validation')
            result.update(category=category, diagnostic_code=local.stage,
                error_types=sorted({issue.error_type for issue in local.issues}))
        except (ValidationError, TypeError):
            pass
    return result


def render_failure(state, materials):
    import streamlit as st
    details = failure_details(state, materials)
    st.error(f"Technical Processing Failed: {details['category']} | {details['code']} | "
             f"{details.get('diagnostic_code',details['stage'])}. No successful supplier assessment or automatic fallback was created.")
    st.write('Safe diagnostic:', details)


def render_stages(state):
    import streamlit as st
    events = state.get('audit_events',[])
    completed = {event.get('stage') for event in events if event.get('event')=='stage_completed'}
    rows=[]
    failed=(state.get('safe_error') or {}).get('stage')
    for stage in ('ingest','extract','verify','trust','match_pr','rules','decide','human_review'):
        status='Not reached'
        if stage in completed: status='Completed'
        if stage=='extract' and state.get('extraction_status')=='SCHEMA_VALIDATED': status='Completed'
        if stage=='trust' and state.get('trusted_facts_status')=='BUILT': status='Completed'
        if stage=='decide':
            status='Completed' if state.get('artifacts',{}).get('assessment') and not state.get('safe_error') else 'Not reached'
            if state.get('safe_error') and state.get('automated_decision'):
                status='Not assessed | technical recovery routing only'
        if stage==failed: status='Failed'
        if stage=='human_review':
            status={'pending':'Awaiting reviewer','completed':'Completed',
                    'not_required':'Not required','cancelled':'Cancelled'}.get(state.get('human_review_status'),'Not reached')
            if state.get('safe_error') and state.get('human_review_status')=='pending':
                status='Awaiting technical recovery'
        rows.append({'Workflow stage':stage,'Status':status})
    st.dataframe(rows,hide_index=True,width='stretch')
