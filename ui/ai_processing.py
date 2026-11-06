"""Explicit experimental AI mode; deterministic processing stays separately selected."""
from pathlib import Path
from uuid import uuid4
from src.ai_graph_models import TrustedReviewer,AIGraphError
from src.ai_graph_workflow import ai_sqlite_service
from src.ai_assisted import run_ai_assisted,evaluation_id
from src.gemini_config import GeminiConfig
from src.runtime_paths import DEFAULT_DB
from src.purchase_request import read_purchase_request, purchase_request_items


def switch_to_deterministic():
    import streamlit as st
    st.session_state['processing_backend']='LangGraph (persistent)'
    st.session_state['process_queued_documents']=False


def storage_root(): return DEFAULT_DB.parent/'ai_assisted'


def render_assessment(materials):
    import streamlit as st
    assessment=materials.get('assessment')
    if not assessment:
        st.info('No AI-based rule assessment is available. Unverified claims were not passed to the rules.')
        return
    st.write('Automated assessment:',assessment['decision'])
    checks=assessment['evaluation']['results']
    important=sorted(checks,key=lambda c:{'fail':0,'review':1,'unknown':2,'pass':3}.get(c['status'],4))
    for check in important:
        with st.expander(f"{check['rule_id']} | {check['status'].upper()} | {check['wording']}",expanded=check['status']!='pass'):
            st.write(check['explanation'])
            st.write('Actual:',str(check.get('actual_value')),'Required:',str(check.get('expected_value')))
            for evidence in check.get('evidence',[]):
                st.caption(f"{evidence.get('filename','Source document')} | Page {evidence['page_number']} | Exact source evidence")
                st.code(evidence['quote'],language=None)


def render_run(thread_id):
    import streamlit as st
    reviewer=TrustedReviewer(subject='local-demo-observer',authorized=True)
    try:
        with ai_sqlite_service(storage_root()) as service:
            state=service.inspect(thread_id)
            materials=service.review_materials(thread_id,reviewer=reviewer)
        from ui.components import badge
        from src.execution_status import presentation_outcome, TECHNICAL_FAILURE
        st.markdown(f"**{state.get('input_filename','document.pdf')} | AI-assisted**")
        outcome=presentation_outcome(state,assessment=materials.get('assessment',{}))
        badge(outcome['status'])
        if state.get('human_review_status')=='completed':
            st.caption('Human review has completed. The original automated assessment is preserved; see the separate final disposition below.')
        st.caption('Supplier: '+next((str(f['normalized'].get('text_value')) for f in materials.get('facts',{}).get('facts',[]) if f['path']=='supplier.name' and f['authoritative'] and f.get('normalized')),'Not independently verified'))
        st.caption(f"Stage: {state['current_stage']} | Review: {state['human_review_status']} | Model: {state['model_id']}")
        if state.get('safe_error'):
            from ui.ai_diagnostics import render_failure
            render_failure(state,materials)
            st.caption('The technical failure remains saved. The internal fail-closed review route is not a successful supplier assessment. Any retry requires another explicit Start Evaluation action; no automatic fallback was run.')
        from ui.components import navigate
        st.button('View saved evaluation',key='ai-result-'+thread_id,on_click=navigate,args=('Results','ai::'+thread_id))
        if outcome['status'] != TECHNICAL_FAILURE:
            render_assessment(materials)
        verified_rows=[]
        for fact in materials.get('facts',{}).get('facts',[]):
            if fact.get('authoritative') and fact.get('normalized') and (fact['path']=='supplier.name' or fact['path'].startswith('line_items[')):
                value=fact['normalized']
                verified_rows.append({'Field':fact['path'],'Verified value':str(value.get('decimal_value') if value.get('decimal_value') is not None else value.get('text_value'))})
        if verified_rows:
            st.write('Independently verified supplier and item information')
            st.dataframe(verified_rows,hide_index=True,width='stretch')
        from ui.ai_diagnostics import render_stages
        render_stages(state)
        if state.get('matching_summary'):
            st.write('Purchase request comparison:',state['matching_summary'])
        render_source_and_fields(state,materials,thread_id)
        summary=state.get('evidence_summary',{})
        st.write('Evidence verification:',{'verified_fields':summary.get('verified',0),
            'unresolved_fields':len(summary.get('unresolved',[])),'conflicts':len(summary.get('conflicts',[]))})
        if state.get('human_review_status')=='pending':
            if outcome['status'] == TECHNICAL_FAILURE:
                st.warning('Technical recovery pending. The workflow paused safely; no successful supplier assessment is available. Reviewer actions are explicit human dispositions, not verified AI results.')
            else:
                st.warning('Human Review Required. Business decision: Needs a person to check. This assessment is not finalized. Reviewer identity is self-declared, not authenticated; inspect original pages before acting.')
            document=materials.get('document',{})
            with st.expander('Original source text'):
                for page in document.get('pages',[]):
                    st.caption('Page '+str(page['page_number']));st.code(page['text'],language=None)
            with st.form('ai-review-'+thread_id):
                action=st.selectbox('Review action',['request_correction','approve','reject'])
                rationale=st.text_area('Written rationale')
                subject=st.text_input('Local reviewer identifier')
                acknowledged=st.checkbox('I have inspected the original source and am authorized to record this local review.')
                submit=st.form_submit_button('Record human review')
            if submit:
                if not acknowledged or not rationale.strip() or not subject.strip():
                    st.error('Provide reviewer identity, written rationale and review authorization.');return
                payload={key:state[key] for key in ('thread_id','workflow_id','document_sha256','review_token')}
                payload.update(action=action,rationale=rationale,submission_id=uuid4().hex)
                with ai_sqlite_service(storage_root()) as service:
                    source=service.store.get_bytes(state['artifacts']['source'])
                    service.resume(thread_id,payload,reviewer=TrustedReviewer(subject=subject,authorized=True),current_pdf=source)
                st.rerun()
        if materials.get('review_receipt'):
            submission=materials['review_receipt'].get('submission',{})
            st.write('Human-review rationale:',submission.get('rationale','Not recorded'))
        if state.get('final_disposition'):
            st.write('Final disposition:',state['final_disposition'])
            st.caption('Human disposition is recorded separately from the original automated assessment.')
            if outcome['status'] == TECHNICAL_FAILURE:
                st.caption('The technical failure remains recorded; a human disposition does not establish successful AI extraction.')
    except (AIGraphError,ValueError,OSError):
        st.error('The saved AI workflow could not be opened or updated safely. Its previous assessment has not been replaced.')


def start_document(data, name, *, config, cloud_processing_authorized, fresh=False, on_stage=None, thread_id=None):
    """UI boundary: retain F01 typed rows and existing guarded graph services."""
    from src.minimal_json_adapter import MinimalJsonAdapter
    from src.pdf_ingestion import ingest_pdf
    rows = purchase_request_items(read_purchase_request())
    thread = thread_id or ('minimal_' + uuid4().hex if fresh else evaluation_id(data, rows, filename=name, config=config))
    adapter = MinimalJsonAdapter(ingest_pdf(data))
    single_attempt = config.model_copy(update={'max_retries': 0})
    with ai_sqlite_service(storage_root(), config=single_attempt, provider=adapter,
                          cloud_processing_authorized=cloud_processing_authorized) as service:
        return service.start(data, rows, thread_id=thread, filename=name, on_stage=on_stage)


def render():
    """Compatibility entry: one public processing screen, no separate mode selector."""
    from ui.processing import render as render_processing
    render_processing()


def render_configuration():
    import streamlit as st
    with st.expander('Gemini configuration & availability'):
        try:
            config=GeminiConfig.from_env()
            secret=config.api_key.get_secret_value() if config.api_key else ''
            model=config.model.replace(secret,'<redacted>') if secret else config.model
            st.write('Configured model:',model)
            st.write('AI feature:', 'Enabled' if config.enabled else 'Disabled')
            st.write('Credential:', 'Present (hidden)' if secret else 'Not configured')
        except (ValueError,TypeError): st.error('Gemini configuration is invalid. Check documented environment settings; no credential values are displayed.')
        st.info('Provider availability: not verified. Configuration and metadata access do not guarantee generation availability. No live connectivity check is performed.')
        st.caption('A controlled synthetic live run succeeded with gemini-3.1-flash-lite. This does not guarantee current provider availability or accuracy on other documents. Use run.cmd for secure .env loading; direct Streamlit launch reads process environment only.')


def render_source_and_fields(state,materials,thread):
    import streamlit as st
    with st.expander('Assessed PDF & exact source pages'):
        with ai_sqlite_service(storage_root()) as service:
            data=service.store.get_bytes(state['artifacts']['source'])
        name=state.get('input_filename','document.pdf')
        render_pdf_preview(data, name, thread)
    with st.expander('Field-level evidence verification'):
        fields=materials.get('verification',{}).get('fields',[])
        if not fields: st.info('No evidence-verification result exists for this run. Provider or extraction failure did not create verified facts.')
        else:
            st.dataframe([{'Field':f['field_path'],'Verification':f['outcome'],
                'Raw values':' | '.join(f['raw_values']),'Eligible':f['eligible_for_future_use'],
                'Human interpretation':f['requires_human_interpretation']} for f in fields],hide_index=True,width='stretch')
            selected=st.selectbox('Inspect evidence field',[f['field_path'] for f in fields],key='ai-field-'+thread)
            field=next(f for f in fields if f['field_path']==selected)
            st.write('Verification reasons:',', '.join(field['reason_codes']) or 'No additional reason')
            for evidence in field.get('evidence_checks',[]):
                st.caption(f"{state.get('input_filename','document.pdf')} | Page {evidence['source']['page_number']} | {evidence['quote_outcome']}")
                st.code(evidence['claimed_quote'],language=None)


def render_pdf_preview(data, name, thread):
    import streamlit as st
    st.download_button('Download assessed PDF',data,file_name=name,mime='application/pdf',key='ai-pdf-'+thread)
    import pymupdf
    with pymupdf.open(stream=data,filetype='pdf') as pdf:
        number=st.selectbox('Source page',list(range(1,len(pdf)+1)),key='ai-page-'+thread)
        if st.checkbox('Render selected PDF page',key='ai-preview-'+thread):
            st.image(pdf[number-1].get_pixmap().tobytes('png'))
