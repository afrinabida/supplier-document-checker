"""Evidence-led assessment details for the current batch or a saved evaluation."""
from ui.components import page_header, saved_evaluations,mode_filter


def select_saved_evaluation(thread_id):
    import streamlit as st
    from src.run_history import AI_PREFIX
    if thread_id.startswith(AI_PREFIX):
        from ui.ai_processing import render_run
        render_run(thread_id[len(AI_PREFIX):])
        return None
    st.caption('Processing mode: Deterministic | Local extraction and rules')
    from src.graph_workflow import sqlite_service, automatic_records
    with sqlite_service() as service:
        state = service.inspect(thread_id)
    if state.get('error'):
        st.error(state['error'])
        from ui.processing import render_stages
        render_stages(state)
        return None
    extraction, decision = automatic_records(state)
    # Presentation context only. The database state and automated record are untouched.
    st.session_state.setdefault('supplier_decisions', {})[state['input_filename']] = decision
    st.session_state.setdefault('graph_states', {})[state['input_filename']] = state
    return extraction


def render() -> None:
    import streamlit as st
    from ui.processing import show_result, restore_graph_runs
    page_header('Evaluation results', 'Inspect the automated assessment, actual values, procurement requirements and verbatim source evidence.')
    restore_graph_runs()
    selected = st.session_state.get('selected_thread_id')
    if selected:
        try:
            extraction = select_saved_evaluation(selected)
            if extraction:
                st.markdown(f'**{extraction.document.filename}**')
                show_result(extraction)
        except (ValueError,OSError) as exc:
            st.error(str(exc))
        if st.button('Return to current batch'):
            st.session_state.pop('selected_thread_id',None)
            st.rerun()
        return
    results = st.session_state.get('extraction_results', [])
    if not results:
        records = saved_evaluations()
        if records is None:
            return
        records=mode_filter(records)
        if not records:
            st.info('No evaluated documents yet. Choose PDFs in Upload, then run the workflow.')
            return
        thread = st.selectbox('Saved evaluation', [row['thread_id'] for row in records],
                              format_func=lambda value: next(f"{row['input_filename']} · {row.get('automated_decision','Not decided')} · {value[-8:]}" for row in records if row['thread_id']==value))
        try:
            extraction = select_saved_evaluation(thread)
            if extraction:
                show_result(extraction)
        except (ValueError,OSError) as exc:
            st.error(str(exc))
    for result in results:
        supplier = result.fields.get('supplier')
        label = f"{result.document.filename} · {supplier.normalized if supplier else 'Supplier not extracted'}"
        with st.expander(label):
            show_result(result)

