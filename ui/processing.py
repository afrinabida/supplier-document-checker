"""Run and inspect the real sequential extraction workflow."""
from uuid import uuid4
import sqlite3


def show_result(result):
    import streamlit as st
    from ui.components import display_value
    st.caption('Processing mode: Deterministic | Local PDF extraction; no external AI request')
    supplier=result.fields.get('supplier')
    st.write('Supplier:',display_value(supplier.normalized) if supplier else 'Not established')
    decision = st.session_state.get('supplier_decisions', {}).get(result.document.filename)
    state = st.session_state.get('graph_states', {}).get(result.document.filename)
    assessment, fields, evidence_tab = st.tabs(['Assessment', 'Extracted values', 'Source evidence'])
    with assessment:
        if decision is not None:
            show_decision(decision)
        else:
            st.info('An automated assessment is not available for this document.')
    with fields:
        st.caption('Raw source values are retained alongside normalized values. Missing fields are never filled with guesses.')
        st.dataframe([{'Field': name.replace('_',' ').title(), 'Raw value': ' | '.join(field.raw_values),
                       'Normalized value': display_value(field.normalized), 'Extraction status': field.status,
                       'Source pages': ', '.join(str(e.page_number) for e in field.evidence)}
                      for name, field in result.fields.items()], hide_index=True, width='stretch')
        if not result.missing_fields and not result.warnings:
            st.success('All expected extraction fields were located without parser warnings.')
        for missing in result.missing_fields:
            st.warning(f'Missing {missing.field}: {missing.reason}')
        for warning in result.warnings:
            st.warning(f'{warning.field}: {warning.message}')
    with evidence_tab:
        selected_field = st.selectbox('Inspect field evidence', list(result.fields),
                                     key='evidence-field-'+result.document.filename) if result.fields else None
        if selected_field:
            for source in result.fields[selected_field].evidence:
                st.caption(f'{source.filename} | Page {source.page_number} | Exact source text')
                st.code(source.quote, language=None)
        with st.expander('Complete original page text'):
            for page in result.document.pages:
                st.caption(f'Page {page.page_number}')
                st.code(page.text, language=None)
    st.caption('Extraction stages completed: ' + ' | '.join(result.stages))
    st.download_button('Download extraction JSON', result.model_dump_json(indent=2),
                       file_name=result.document.filename+'.json', mime='application/json',
                       key='json-'+result.document.filename)
    if state is not None:
        show_graph_review(state)


def restore_graph_runs():
    """Reruns/restarts inspect persisted threads; they never implicitly start runs."""
    import streamlit as st
    from src.graph_workflow import sqlite_service, automatic_records
    threads = st.session_state.get('graph_thread_ids') or st.query_params.get_all('workflow')
    if not threads:
        return
    results, decisions, states, errors = [], {}, {}, []
    try:
        with sqlite_service() as service:
            for thread in threads:
                try:
                    state = service.inspect(thread)
                    states[state['input_filename']] = state
                    extraction, decision = automatic_records(state)
                    results.append(extraction)
                    decisions[state['input_filename']] = decision
                except ValueError as exc:
                    errors.append(str(exc))
    except (OSError, sqlite3.Error) as exc:
        errors.append(f'Checkpoint storage is unavailable: {exc}')
    st.session_state.update(graph_thread_ids=list(threads), extraction_results=results,
                            supplier_decisions=decisions, graph_states=states, extraction_errors=errors)


def show_graph_review(state):
    import streamlit as st
    from hashlib import sha256
    from src.graph_workflow import sqlite_service
    from src.evaluation_store import export_evaluation, workflow_status
    st.divider()
    st.markdown('**Human review & final outcome**')
    st.write(f"Workflow: {workflow_status(state)} Â· Human review: {state['review_status']}")
    st.write('Final outcome:', state['final_outcome'] or 'Not finalized â€” awaiting a reviewer' if state['review_status']=='pending' else state['final_outcome'] or 'Not available')
    st.caption(f"Saved thread: {state['thread_id']}. Human outcomes never overwrite the automated assessment or change machine-prediction accuracy.")
    with st.expander('Inspect graph state'):
        st.json(state)
    with st.expander('Original supplier PDF'):
        from src.evaluation_store import original_pdf
        try:
            data = original_pdf(state)
            st.download_button('Download assessed source PDF', data, file_name=state['input_filename'],
                               mime='application/pdf', key='source-pdf-'+state['thread_id'])
            if st.checkbox('Show original pages', key='source-preview-'+state['thread_id']):
                import pymupdf
                with pymupdf.open(stream=data, filetype='pdf') as pdf:
                    for index, page in enumerate(pdf):
                        st.caption(f"{state['input_filename']} | Page {index+1}")
                        st.image(page.get_pixmap().tobytes('png'))
        except ValueError as exc:
            st.error(str(exc))
    if state['review_status'] == 'pending':
        st.warning('Demo only: reviewer identity is not authenticated. Submit an action only after inspecting the original document, rules and evidence.')
        with st.form('review-' + state['thread_id']):
            action = st.selectbox('Reviewer action', ['approve', 'reject', 'request_correction'])
            reason = st.text_area('Mandatory review reason')
            reviewer = st.text_input('Reviewer (optional)')
            submitted = st.form_submit_button('Submit review')
        if submitted:
            if not reason.strip():
                st.error('A written review reason is mandatory. The pending review has not been changed.')
                return
            response = {key: state[key] for key in ('thread_id', 'pack_id', 'review_token')}
            response.update(action=action, reason=reason, reviewer=reviewer or None,
                            submission_id=sha256((state['thread_id']+action+reason+reviewer).encode()).hexdigest())
            try:
                with sqlite_service() as service:
                    service.resume(state['thread_id'], response)
                st.rerun()
            except (ValueError, OSError, sqlite3.Error) as exc:
                st.error(str(exc))
    elif state.get('human_review'):
        audit = state['human_review']
        st.success(f"Reviewer action recorded: {audit['action'].replace('_',' ').title()}")
        st.write('Written rationale:', audit['reason'])
        st.caption(f"Recorded {audit['timestamp_utc']} (UTC) Â· Reviewer: {audit.get('reviewer') or 'Not supplied'}")
        st.caption('This review is complete. Further changed submissions are blocked by the backend.')
    st.download_button('Export complete evaluation JSON', export_evaluation(state),
                       file_name=state['pack_id']+'.evaluation.json', mime='application/json',
                       key='audit-export-'+state['thread_id'])


def show_decision(decision):
    import streamlit as st
    from ui.components import badge, display_value, finding_label
    st.subheader('Automated assessment')
    badge(decision.decision)
    st.caption('Deterministic checks against the supplied procurement rules. Reviewer outcomes are stored separately.')
    if not decision.reasons:
        st.success('All required checks passed. No unresolved or failing checks were found.')
    else:
        for reason in decision.reasons:
            label = finding_label(reason)
            st.write(f'**{reason.rule_id} | {label}** | {reason.wording}')
    with st.expander(f'All rule findings | {len(decision.evaluation.results)} checks'):
        st.caption('PASS: condition satisfied. FAIL: rejecting condition triggered. REVIEW: human-review condition triggered. UNKNOWN: source does not establish a safe answer. NOT APPLICABLE: unsupported separate arithmetic check.')
        st.dataframe([{'Rule': check.rule_id, 'Status': finding_label(check),
                       'Actual value': display_value(check.actual_value), 'Required / expected': display_value(check.expected_value)}
                      for check in decision.evaluation.results], hide_index=True, width='stretch')
        for check in decision.evaluation.results:
            st.markdown(f'**{check.rule_id} | {finding_label(check)}**')
            st.write(check.wording)
            left, right = st.columns(2)
            with left:
                st.caption('ACTUAL VALUE')
                st.code(display_value(check.actual_value), language=None)
            with right:
                st.caption('REQUIRED / EXPECTED')
                st.code(display_value(check.expected_value), language=None)
            st.write(check.explanation)
            for source in check.evidence:
                st.caption(f'{source.filename} | Page {source.page_number} | Verbatim evidence / search context')
                st.code(source.quote, language=None)
            st.divider()
    st.download_button('Download decision JSON', decision.model_dump_json(indent=2),
                       file_name=decision.filename+'.decision.json', mime='application/json',
                       key='decision-json-'+decision.filename)


def render() -> None:
    import streamlit as st
    from hashlib import sha256
    from ui.components import page_header
    from ui.ai_processing import GeminiConfig, render_configuration, render_run
    from ui.upload_state import active_pending_documents
    from ui.evaluation_workflow import STEP_LABELS
    from ui.batch_evaluation import prepare_batch, run_batch, summary_rows
    page_header('Document processing', 'Start one fixed-step evaluation: extract, verify evidence, compare the purchase request, check rules and retain the decision for review.')
    restore_graph_runs()
    # Discard legacy quick-run flags: navigation/reruns never authorize processing.
    st.session_state.pop('process_queued_documents', None)
    pending = active_pending_documents(st.session_state)
    st.subheader('Selected supplier documents')
    if pending:
        st.dataframe([{'Filename': name, 'Size (KB)': round(len(data)/1024, 1)} for name, data in pending],
                     hide_index=True, width='stretch')
    else:
        st.info('Upload supplier PDFs and continue here to start an evaluation. Saved evaluations remain available in Results and Evaluations.')
    config = None
    consent, fresh = False, False
    ready = False
    try:
        config = GeminiConfig.from_env()
        if config.enabled:
            st.warning('AI extraction is configured. Python independently verifies evidence and decides procurement outcomes. Missing or unsupported facts require review; there is no automatic fallback.')
            identity = sha256(repr([(name, sha256(data).hexdigest()) for name, data in pending]).encode() + config.model.encode()).hexdigest()
            if st.session_state.get('evaluation_consent_batch') != identity:
                st.session_state['ai_cloud_consent'] = False
                st.session_state['evaluation_consent_batch'] = identity
            consent = st.checkbox('Allow external Gemini processing of these documents for this run.', key='ai_cloud_consent')
            fresh = st.checkbox('Create a new AI attempt (keeps prior run)', value=False, key='ai_fresh_attempt')
            ready = bool(config.api_key and config.api_key.get_secret_value().strip())
            if not ready:
                st.error('AI is configured but its credential is unavailable. No document will be submitted and no fallback will run.')
            ready = ready and consent
        else:
            st.info('AI extraction is not enabled by the operator. This evaluation uses the existing local deterministic LangGraph; no document is sent to Gemini.')
            st.session_state.pop('evaluation_consent_batch', None)
            ready = True
    except (ValueError, TypeError):
        st.error('Evaluation configuration is invalid. No processing or automatic fallback will run.')
    render_configuration()
    st.caption('LangGraph runs fixed steps and saves resumable checkpoints automatically. Existing history is preserved. Backend configuration is managed by the operator; there is no processing-mode menu.')
    clicked = st.button('Start Evaluation', disabled=not pending or not ready, type='primary')
    if clicked and config is not None:
        batch = st.query_params.get('evaluation_batch') or st.session_state.get('evaluation_batch') or uuid4().hex
        st.session_state['evaluation_batch'] = batch
        st.query_params['evaluation_batch'] = batch
        # Detach the other backend's current view only; its persisted history is untouched.
        if config.enabled:
            for key in ('graph_thread_ids','graph_states','supplier_decisions','extraction_results','extraction_errors'):
                st.session_state.pop(key, None)
            st.query_params.pop('workflow', None)
        else:
            st.session_state.pop('ai_thread_ids', None)
            st.query_params.pop('ai_workflow', None)
        batch_state=prepare_batch(st.session_state.get('batch_evaluations'),pending,config,fresh=fresh)
        st.session_state['batch_evaluations']=batch_state
        progress, status, live_summary = st.progress(0), st.empty(), st.empty()
        def update(index,total,row,stage):
            status.write(row['filename']+': '+STEP_LABELS.get(stage,stage.replace('_',' ').title()))
            fraction={'validate_pdf':0.02,'ingest':0.10,'extract':0.25,'validate':0.40,
                'verify':0.40,'trust':0.50,'match_pr':0.60,'arithmetic':0.70,
                'rules':0.80,'decide':0.90,'human_review':0.95,'finalize':1.0,'saved':1.0}.get(stage,0)
            progress.progress((index+fraction)/total)
            live_summary.dataframe(summary_rows(batch_state),hide_index=True,width='stretch')
        run_batch(batch_state,pending,config=config,cloud_processing_authorized=consent,on_progress=update)
        errors, results, decisions, states, threads, ai_threads = [], [], {}, {}, [], []
        for row in batch_state['documents']:
            name,state=row['filename'],row['state']
            if row['status']=='Processing Error':
                errors.append(name+': '+row['reason'])
            if state is None:
                continue
            if row['mode']=='AI-assisted':
                ai_threads.append(state['thread_id'])
                if row['status']=='Human Review':
                    st.warning(name+': Human Review Required. The saved assessment is awaiting review.')
                elif row['status'] in ('Approve','Reject'):
                    st.success(name+': '+row['status'])
            else:
                from src.graph_workflow import automatic_records
                threads.append(state['thread_id'])
                states[name]=state
                if not state.get('error'):
                    extraction,decision=automatic_records(state)
                    results.append(extraction)
                    decisions[name]=decision
        if config.enabled:
            st.session_state['ai_thread_ids']=ai_threads
            if ai_threads:
                st.query_params['ai_workflow']=ai_threads
            else:
                st.query_params.pop('ai_workflow',None)
        live_summary.empty()
        st.session_state['evaluation_start_errors'] = errors
        if not config.enabled:
            st.session_state.update(extraction_results=results, extraction_errors=errors,
                supplier_decisions=decisions, graph_states=states, graph_thread_ids=threads)
            if threads:
                st.query_params['workflow'] = threads
            else:
                st.query_params.pop('workflow', None)
        status.write('Evaluation batch finished. Decisions and unresolved reviews remain saved; provider failures are not AI successes.')
    if st.session_state.get('batch_evaluations'):
        st.subheader('Batch summary')
        st.dataframe(summary_rows(st.session_state['batch_evaluations']),hide_index=True,width='stretch')
    for error in st.session_state.get('evaluation_start_errors', []):
        st.error(error)
    for state in st.session_state.get('graph_states', {}).values():
        render_stages(state)
    for error in st.session_state.get('extraction_errors', []):
        if error not in st.session_state.get('evaluation_start_errors', []):
            st.error(error)
    for result in st.session_state.get('extraction_results', []):
        with st.expander(result.document.filename, expanded=True):
            show_result(result)
    for thread in st.session_state.get('ai_thread_ids', []) or st.query_params.get_all('ai_workflow'):
        render_run(thread)


def render_stages(state):
    import streamlit as st
    from src.evaluation_store import workflow_status
    st.markdown(f"**{state['input_filename']} | {workflow_status(state)}**")
    steps = ['extract','validate','match_pr','arithmetic','rules','decide','human_review','finalize']
    rows = []
    for name in steps:
        if state.get('error') and name in state.get('trace', []) and name != 'finalize':
            # Only the last executed non-finalize node failed.
            last = [item for item in state['trace'] if item != 'finalize'][-1]
            status = 'Failed' if name == last else 'Completed'
        elif name in state.get('trace', []):
            status = 'Completed'
        elif name == 'human_review' and state.get('review_status') == 'pending':
            status = 'Interrupted | awaiting reviewer'
        elif name == 'human_review' and state.get('review_status') == 'not_required':
            status = 'Not required'
        else:
            status = 'Not reached'
        if state.get('error') and name == 'finalize':
            status = 'Error recorded | no supplier outcome'
        rows.append({'Workflow stage': name.replace('_',' ').title(), 'State': status})
    st.dataframe(rows, hide_index=True, width='stretch')
