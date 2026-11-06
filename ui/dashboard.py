"""Dashboard of actual saved automated assessments and open review runs."""
from ui.components import page_header, saved_evaluations, navigate
from src.run_history import statistics as dashboard_statistics,latest_runs as latest_documents,rows as evaluation_rows


def render() -> None:
    import streamlit as st
    import pandas as pd
    page_header('Procurement overview', 'A clear view of supplier document checks, source evidence and the reviews that need your attention.')
    records = saved_evaluations()
    if records is None:
        st.info('Saved workflow data is unavailable. No dashboard totals are reported.')
        return
    stats = dashboard_statistics(records)
    columns = st.columns(4)
    for column, label, value in zip(columns, ['Evaluated packs', 'Approved', 'Rejected', 'Needs a person to check'],
                                    [stats['total_evaluated'], stats['approved'], stats['rejected'], stats['needs_review']]):
        column.metric(label, value)
    st.caption(f"Automated decisions from the latest saved evaluation per document version and processing mode. {stats['saved_runs']} saved runs; {stats['failed_documents']} latest document failures. Human outcomes do not change these counts.")
    if not records:
        st.info('No saved evaluations yet. Upload supplier PDFs to start a new evaluation.')
        st.button('Check supplier documents', on_click=navigate, args=('Upload',), type='primary')
        return
    left, right = st.columns([1.45, 1])
    with left:
        st.header('Decision distribution')
        chart = pd.DataFrame({'Decision':['Approve','Reject','Needs a person to check'],
                              'Documents':[stats['approved'], stats['rejected'], stats['needs_review']]})
        st.bar_chart(chart.set_index('Decision'), color='#635BDF', height=230, horizontal=True, sort=False)
    with right:
        st.header('Human review queue')
        st.metric('Pending review runs', stats['pending_reviews'])
        st.caption('Includes all saved interrupted runs, including repeat evaluations. No reviewer action is assumed.')
        st.button('Open review queue', on_click=navigate, args=('Evaluations',), type='primary')
    st.header('Recent evaluations')
    recent = latest_documents(records)[:8]
    st.dataframe(evaluation_rows(recent), hide_index=True, width='stretch')
    selected = st.selectbox('Open evaluation details', [row['thread_id'] for row in recent],
                            format_func=lambda thread: next(f"{row['input_filename']} Ãƒâ€šÃ‚Â· {row.get('automated_decision', 'Not decided')}" for row in recent if row['thread_id']==thread))
    st.button('View selected evaluation', on_click=navigate, args=('Results', selected))
    pending = [row for row in records if row.get('review_status') == 'pending']
    if pending:
        with st.expander(f'Pending review details Ãƒâ€šÃ‚Â· {len(pending)} saved runs'):
            st.dataframe(evaluation_rows(pending), hide_index=True, width='stretch')

