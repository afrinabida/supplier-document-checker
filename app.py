"""Streamlit entry point. Business rules remain in src/."""
import streamlit as st
from ui import dashboard, evaluations, processing, results, rules, upload
from ui.components import apply_theme


def main() -> None:
    st.set_page_config(page_title='AI Supplier Document Checker', page_icon=':material/fact_check:', layout='wide', initial_sidebar_state='expanded')
    apply_theme()
    pages = {'Dashboard':dashboard, 'Upload':upload, 'Processing':processing,
             'Results':results, 'Evaluations':evaluations, 'Rules':rules}
    requested = st.session_state.pop('navigate_to', None)
    if requested in pages:
        st.session_state['page'] = requested
    if st.session_state.get('page') not in pages:
        st.session_state['page'] = 'Dashboard'
    with st.sidebar:
        st.markdown('<div class="brand">AI Supplier Document Checker</div><div class="brand-subtitle">SUPPLIER QUOTATION EVALUATION<br>Procurement workspace</div>', unsafe_allow_html=True)
        selected = st.radio('Navigation', list(pages), key='page', format_func=lambda page: 'Upload Supplier Documents' if page == 'Upload' else page)
        st.divider()
        st.caption('LOCAL DOCUMENT WORKFLOW')
        st.caption('PDF parsing | deterministic checks | human review')
        st.caption('Local checks by default. Experimental AI requires explicit cloud consent; availability is unverified.')
    pages[selected].render()


if __name__ == '__main__':
    main()

