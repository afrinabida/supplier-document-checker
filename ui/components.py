"""Shared visual language for the six Streamlit pages."""
from html import escape
import json

CSS = '''
<style>
:root { --ink:#14213b; --muted:#536179; --accent:#635bdf; }
.stApp { background:#f5f7fb; color:var(--ink); font-family:"Segoe UI",Arial,sans-serif; }
[data-testid="stAppViewContainer"] > .main { background:#f5f7fb; }
[data-testid="stSidebar"] { background:#101c33; }
[data-testid="stSidebar"] * { color:#e7edf9; }
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color:#aebdd7; }
[data-testid="stSidebar"] label { font-weight:500; }
.block-container { padding-top:3.4rem; padding-bottom:3rem; max-width:1440px; }
[data-testid="stAppDeployButton"] { display:none; }
h1,h2,h3 { color:#14213b; letter-spacing:-.035em; }
h1 { font-size:2.1rem !important; font-weight:750 !important; }
h2 { font-size:1.4rem !important; }
h3 { font-size:1.13rem !important; }
[data-testid="stMetric"] { background:white; border:1px solid #dfe5ef; border-radius:14px; padding:18px 20px; }
[data-testid="stMetricLabel"] { color:#536179; font-size:.83rem; }
[data-testid="stMetricValue"] { color:#172b4d; font-size:2rem; }
[data-testid="stExpander"] { background:white; border:1px solid #dfe5ef; border-radius:12px; }
[data-testid="stDataFrame"] { border-radius:12px; overflow:hidden; border:1px solid #dfe5ef; }
.stButton > button, .stDownloadButton > button { border-radius:9px; font-weight:600; }
.stButton > button[kind="primary"] { background:linear-gradient(110deg,#365edb,#635bdf); color:white; border:0; }
.stButton > button:focus-visible { outline:3px solid #aaa4f5; outline-offset:2px; }
.hero-note { color:#536179; max-width:900px; margin:0 0 1.5rem; line-height:1.65; }
.eyebrow { color:#665fbd; letter-spacing:.12em; font-size:.73rem; text-transform:uppercase; font-weight:750; margin-bottom:.4rem; }
.decision-badge { display:inline-block; padding:7px 13px; border-radius:30px; font-weight:700; font-size:.85rem; margin:4px 0 12px; }
.badge-approve { background:#e1f4ea; color:#15583a; }
.badge-reject { background:#fce7eb; color:#963147; }
.badge-review { background:#fff0d6; color:#80510c; }
.badge-neutral { background:#e9e9fa; color:#48429a; }
.rule-heading { display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:.4rem; }
.rule-id { color:#655dc9; font-size:.8rem; font-weight:750; }
.source-label { color:#536179; font-size:.8rem; margin-top:10px; }
.brand { font-weight:800; font-size:1.35rem; letter-spacing:-.04em; margin-bottom:4px; }
.brand-subtitle { color:#aebdd7; font-size:.78rem; line-height:1.5; margin-bottom:1.5rem; }
@media(max-width:700px) { .block-container { padding:4rem 1rem 1rem; } h1 { font-size:1.7rem !important; } }
</style>
'''


def apply_theme():
    import streamlit as st
    st.markdown(CSS, unsafe_allow_html=True)


def page_header(title, description, eyebrow='SUPPLIER DOCUMENT CHECKER'):
    import streamlit as st
    st.markdown(f'<div class="eyebrow">{escape(eyebrow)}</div>', unsafe_allow_html=True)
    st.title(title)
    st.markdown(f'<div class="hero-note">{escape(description)}</div>', unsafe_allow_html=True)
    st.caption('Upload → Choose mode → Process → Review evidence → Decision → Saved evaluation')


def badge(label):
    import streamlit as st
    style = {'Approve':'approve', 'Reject':'reject', 'Needs a person to check':'review',
             'Human Review Required':'review','Technical Processing Failed':'reject'}.get(label, 'neutral')
    st.markdown(f'<span class="decision-badge badge-{style}">{escape(label)}</span>', unsafe_allow_html=True)


def display_value(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str) if isinstance(value, (dict,list)) else 'Not established' if value is None else str(value)


def finding_label(check):
    if check.status == 'pass':
        return 'PASS'
    if check.status == 'unknown':
        return 'UNKNOWN'
    if check.status == 'not_applicable':
        return 'NOT APPLICABLE'
    return 'FAIL' if check.category == 'Reject' else 'REVIEW'


def navigate(page, thread_id=None):
    import streamlit as st
    st.session_state['navigate_to'] = page
    if thread_id:
        st.session_state['selected_thread_id'] = thread_id


def saved_evaluations():
    import streamlit as st
    from src.run_history import list_runs
    try:
        return list_runs()
    except ValueError as exc:
        st.error(str(exc))
        return None


def mode_filter(records):
    import streamlit as st
    mode=st.selectbox('Processing mode',['All modes','Deterministic','AI-assisted'])
    return records if mode=='All modes' else [r for r in records if r.get('mode','Deterministic')==mode]
