"""Search saved uploaded-document evaluations and review checkpoints."""
from ui.components import page_header, saved_evaluations,mode_filter
from src.run_history import filter_runs as filter_evaluations, rows as evaluation_rows


def render() -> None:
    import streamlit as st
    page_header('All evaluations & human review', 'Find a saved supplier assessment, examine the evidence and resolve a pending review without changing the original automated decision.')
    records = saved_evaluations()
    if records is None:
        return
    records=mode_filter(records)
    pending = sum(row.get('review_status')=='pending' for row in records)
    st.markdown(f'**{len(records)} saved evaluations  |  {pending} pending review runs**')
    if not records:
        st.info('No saved workflow evaluations yet. Upload supplier PDFs, then process them to populate this workspace.')
        return
    search = st.text_input('Search supplier, document or thread',placeholder='Search by supplier, uploaded filename or saved thread ID')
    left,right = st.columns(2)
    with left:
        decision = st.selectbox('Automated decision filter',['All decisions','Approve','Reject','Needs a person to check'])
    with right:
        status = st.selectbox('Workflow state filter',['All states','Awaiting review','Completed','Failed','In progress'])
    latest = st.checkbox('Latest evaluation per document',value=True,
                         help='Turn off to inspect every saved run, including older pending reviews for the same document.')
    filtered = filter_evaluations(records,search,decision,status,latest)
    st.caption(f'{len(filtered)} matching evaluation(s). Decisions in this table are the original automated assessments.')
    if not filtered:
        st.info('No evaluations match these filters. Clear the search or include older runs.')
        return
    st.dataframe(evaluation_rows(filtered),hide_index=True,width='stretch')
    options = [row['thread_id'] for row in filtered]
    selected = st.session_state.get('selected_thread_id')
    thread = st.selectbox('Select a saved evaluation',options,
                          index=options.index(selected) if selected in options else 0,
                          format_func=lambda value: next(f"{row['input_filename']}  |  {row.get('automated_decision','Not decided')}  |  {value[-8:]}" for row in filtered if row['thread_id']==value))
    from ui.results import select_saved_evaluation
    from ui.processing import show_result
    try:
        extraction = select_saved_evaluation(thread)
        if extraction:
            st.divider()
            supplier = extraction.fields.get('supplier')
            st.markdown(f"**{supplier.normalized if supplier else 'Supplier not extracted'}**  |  {extraction.document.filename}")
            show_result(extraction)
    except (ValueError,OSError) as exc:
        st.error(str(exc))

