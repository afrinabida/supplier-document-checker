"""Session-local upload ownership; never removes persisted sources or checkpoints."""
from hashlib import sha256
from uuid import uuid4


def document_key(name, data):
    return name, sha256(data).hexdigest()


def ingest_uploads(state, uploads):
    records = dict(state.get('active_uploads', {}))
    consumed = dict(state.get('upload_consumed_tokens', {}))
    # Adopt an older in-memory queue when this UI is first opened after upgrade.
    # Never re-adopt a stale queue once active ownership has been initialized.
    first_visit = 'active_uploads' not in state
    if first_visit:
        for index, (name, data) in enumerate(state.get('pending_documents', [])):
            identifier = uuid4().hex
            digest = document_key(name, data)[1]
            records[identifier] = dict(id=identifier, name=name, data=data, sha256=digest)
            consumed[('legacy', name, digest, index)] = identifier
    occurrences = {}
    for file in uploads or []:
        data = file.getvalue()
        key = document_key(file.name, data)
        occurrences[key] = occurrences.get(key, 0) + 1
        source_id = getattr(file, 'file_id', None)
        token = ('streamlit', str(source_id)) if source_id is not None else ('content', *key, occurrences[key])
        if token in consumed:
            continue
        identifier = uuid4().hex
        consumed[token] = identifier
        records[identifier] = dict(id=identifier, name=file.name, data=data, sha256=key[1])
    state['active_uploads'] = records
    state['upload_consumed_tokens'] = consumed
    if first_visit and state.get('pending_documents'):
        bind_pending(state, state['pending_documents'])
    return records


def bind_pending(state, documents):
    """Associate legacy (filename, bytes) queue entries with stable session IDs."""
    records = state.get('active_uploads', {})
    by_key = {document_key(r['name'], r['data']): r['id'] for r in records.values()}
    identifiers = [by_key.get(document_key(name, data)) for name, data in documents]
    if identifiers and all(identifiers):
        state['pending_upload_ids'] = identifiers
    else:
        state.pop('pending_upload_ids', None)


def active_pending_documents(state):
    pending = state.get('pending_documents', [])
    identifiers = state.get('pending_upload_ids')
    if identifiers is None:
        return pending  # Existing saved/legacy clients are unchanged.
    records = state.get('active_uploads', {})
    # Reconstruct from owned bytes, never a stale copy of an earlier queue.
    pending = [(records[i]['name'], records[i]['data']) for i in identifiers if i in records]
    state['pending_documents'] = pending
    state['pending_upload_ids'] = [i for i in identifiers if i in records]
    return pending


def remove_uploads(state, identifiers, query_params=None):
    """Detach active/session data only. No filesystem or persistence operations."""
    records = dict(state.get('active_uploads', {}))
    removed = [records.pop(i) for i in identifiers if i in records]
    if not removed:
        state.pop('upload_removal_confirmation', None)
        return
    state['active_uploads'] = records
    removed_keys = {document_key(r['name'], r['data']) for r in removed}
    remaining_keys = {document_key(r['name'], r['data']) for r in records.values()}
    if state.get('pending_upload_ids') is not None:
        active_pending_documents(state)
    else:
        state['pending_documents'] = [(name, data) for name, data in state.get('pending_documents', [])
                                      if document_key(name, data) not in removed_keys - remaining_keys]
    # Filename-keyed legacy previews cannot safely represent two same-name inputs.
    removed_names = {r['name'] for r in removed}
    state['extraction_results'] = [r for r in state.get('extraction_results', [])
                                   if r.document.filename not in removed_names]
    state['extraction_errors'] = [e for e in state.get('extraction_errors', [])
                                  if not any(e.startswith(name + ':') for name in removed_names)]
    state['evaluation_start_errors'] = [e for e in state.get('evaluation_start_errors', [])
                                        if not any(e.startswith(name + ':') for name in removed_names)]
    detached_threads = set()
    for key in ('supplier_decisions', 'graph_states'):
        values = dict(state.get(key, {}))
        for name in removed_names:
            value = values.pop(name, None)
            if key == 'graph_states' and value:
                detached_threads.add(value.get('thread_id'))
        state[key] = values
    state['graph_thread_ids'] = [i for i in state.get('graph_thread_ids', []) if i not in detached_threads]
    if state.get('selected_thread_id') in detached_threads:
        state.pop('selected_thread_id', None)
    if query_params is not None and detached_threads:
        threads = [i for i in query_params.get_all('workflow') if i not in detached_threads]
        if threads:
            query_params['workflow'] = threads
        else:
            query_params.pop('workflow', None)
    # Removing anything cancels automatic quick-run consent; retained inputs need an explicit action.
    state.pop('process_queued_documents', None)
    state.pop('upload_removal_confirmation', None)
    for key in ('upload_preview_id', 'upload_preview_page', 'upload_render_page'):
        state.pop(key, None)
    state['upload_widget_epoch'] = state.get('upload_widget_epoch', 0) + 1
