"""Validate uploaded PDFs, then hand the batch to the real workflow."""
from collections import Counter
from ui.private_fixtures import (require_public_document, is_private_document,
    require_single_manual_document, manual_evaluation_enabled)
from src.evaluation_store import validate_upload
from ui.components import page_header, navigate
from ui.upload_state import ingest_uploads, bind_pending, remove_uploads


def queue_documents(documents):
    import streamlit as st
    require_single_manual_document(documents)
    for name,data in documents:
        require_public_document(data,name,allow_manual=True)
    st.session_state['pending_documents'] = documents
    bind_pending(st.session_state, documents)
    for key in ('extraction_results','extraction_errors','supplier_decisions','graph_states','graph_thread_ids','selected_thread_id','ai_thread_ids','evaluation_start_errors'):
        st.session_state.pop(key, None)
    st.query_params.pop('workflow', None)
    st.query_params.pop('ai_workflow', None)


def render() -> None:
    import streamlit as st
    page_header('Upload Supplier Documents', 'Upload supplier quotation PDFs, then continue to the fixed-step evaluation workflow.')
    if manual_evaluation_enabled():
        st.warning('Local manual evaluation enabled for the frozen engine. Upload one authorized evaluation PDF at a time. No evaluation files or labels are loaded automatically.')
    epoch = st.session_state.get('upload_widget_epoch', 0)
    uploads = st.file_uploader('Supplier quotation PDFs', type=['pdf'], accept_multiple_files=True,
                               key=f'supplier_pdf_chooser_{epoch}',
                               help='Text-based PDFs are supported. Choose files to add to the active document list below.')
    records = ingest_uploads(st.session_state, uploads)
    st.caption('The chooser adds files to the active list. Use Remove or Clear All below to remove active documents. Remaining files stay in the list when the chooser resets.')
    render_documents([(r['name'], r['data']) for r in records.values()], upload_records=list(records.values()))


def render_removal_controls(records, statuses):
    import streamlit as st
    st.subheader(f'Uploaded documents ({len(records)})')
    st.caption('Removal only affects this upload list and queued inputs. Saved evaluations, audit evidence and review checkpoints are retained. It does not permanently delete historical records.')
    for record, status in zip(records, statuses):
        name, size, state, action = st.columns([5, 1.5, 2.5, 1.4])
        name.text(record['name'])
        name.caption('Upload ID: ' + record['id'][:8])
        size.write(f"{len(record['data']) / 1024:.1f} KB")
        state.write(status)
        if action.button('Remove', icon=':material/delete:', key='remove-upload-' + record['id'],
                         help='Remove this PDF from the active upload list only'):
            st.session_state['upload_removal_confirmation'] = record['id']
    if st.button('Clear All', disabled=not records, icon=':material/delete_sweep:'):
        st.session_state['upload_removal_confirmation'] = '__all__'
    target = st.session_state.get('upload_removal_confirmation')
    if target:
        selected = records if target == '__all__' else [r for r in records if r['id'] == target]
        if not selected:
            st.session_state.pop('upload_removal_confirmation', None)
            return
        label = f"all {len(selected)} active documents" if target == '__all__' else selected[0]['name']
        st.warning(f'Remove {label} from the upload list? Saved evaluations and historical evidence will not be deleted.')
        confirm, cancel = st.columns(2)
        if confirm.button('Confirm removal', type='primary'):
            remove_uploads(st.session_state, [r['id'] for r in selected], st.query_params)
            st.rerun()
        if cancel.button('Cancel removal'):
            st.session_state.pop('upload_removal_confirmation', None)
            st.rerun()


def render_documents(documents, *, upload_records=None, empty_message='Upload at least one supplier PDF to begin.') -> None:
    """Validate active uploads and hand them to the existing processing workflows."""
    import streamlit as st
    validated, previews, errors, statuses = [], {}, [], []
    names = Counter(name for name,_ in documents)
    manual_batch_invalid = len(documents) != 1 and any(is_private_document(data,name) for name,data in documents)
    for name, data in documents:
        try:
            if names[name] > 1:
                raise ValueError('Duplicate filenames in one batch. Select each document once or give distinct uploads unique names.')
            require_public_document(data, name, allow_manual=True)
            if manual_batch_invalid:
                raise ValueError('Manual evaluation accepts exactly one individually uploaded PDF at a time.')
            document = validate_upload(name,data)
            validated.append((name,data))
            if not is_private_document(data,name):
                previews[name] = document
            statuses.append('Uploaded - ready')
        except ValueError as exc:
            errors.append(f'{name}: {exc}')
            statuses.append('Uploaded - cannot process')
    for error in dict.fromkeys(errors):
        st.error(error)
    if upload_records is not None:
        render_removal_controls(upload_records, statuses)
    enabled = bool(validated) and not errors
    st.caption('Input checks validate readability, not supplier compliance. Final checks run in the fixed-step LangGraph workflow in Processing.')
    if st.button('Continue to Processing',disabled=not enabled,type='primary'):
        queue_documents(validated)
        st.session_state.pop('process_queued_documents',None)
        navigate('Processing')
        st.rerun()
    if st.button('Queue selected documents', disabled=not enabled):
        queue_documents(validated)
        st.success(f'{len(validated)} document(s) queued. Open Processing to run the workflow.')
    if previews:
        with st.expander('Inspect the original document'):
            chosen = st.selectbox('Preview PDF', list(previews), key='upload_preview_id')
            data = next(data for name,data in validated if name==chosen)
            st.download_button('Download original PDF', data, file_name=chosen, mime='application/pdf')
            page_number = st.selectbox('Original page', range(1,len(previews[chosen].pages)+1), key='upload_preview_page')
            st.code(previews[chosen].pages[page_number-1].text, language=None)
            if st.checkbox('Render original PDF page', key='upload_render_page'):
                import pymupdf
                with pymupdf.open(stream=data,filetype='pdf') as pdf:
                    st.image(pdf[page_number-1].get_pixmap().tobytes('png'))
    else:
        st.info(empty_message + ' No documents are sent to an external AI service.')
