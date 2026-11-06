"""Original procurement rules, exact thresholds, PR and documented ambiguities."""
from ui.components import page_header
from src.workflow import SAMPLE_DIR,read_purchase_request
from src.rules_config import CONFIG


def render() -> None:
    import streamlit as st
    page_header('Procurement rules', 'The supplied company policy is the source of truth. Reject findings take priority; uncertainty is routed to a person.')
    company,thresholds,request,ambiguities = st.tabs(['Original company rules','Threshold reference','Purchase request','Policy ambiguities'])
    with company:
        try:
            text=(SAMPLE_DIR/'company-rules.md').read_text(encoding='utf-8')
            st.markdown(text)
            st.download_button('Download original company rules',text,file_name='company-rules.md',mime='text/markdown')
        except OSError as exc:
            st.error(str(exc))
    with thresholds:
        st.caption('Read-only values from the existing typed configuration. These controls do not change the rules.')
        st.dataframe([
            {'Requirement':'Price excess','Threshold':f'> {CONFIG.price_excess_percent}% rejects; > 0% through {CONFIG.price_excess_percent}% requires review'},
            {'Requirement':'Advance','Threshold':f'> {CONFIG.advance_max_percent}% rejects'},
            {'Requirement':'Quantity difference','Threshold':f'> {CONFIG.quantity_difference_percent}% requires review'},
            {'Requirement':'Payment','Threshold':f'{CONFIG.payment_min_days}–{CONFIG.payment_max_days} days after delivery; unclear/outside requires review'},
            {'Requirement':'Delivery','Threshold':f'{CONFIG.delivery_review_min_days}–{CONFIG.delivery_max_days} days requires review; > {CONFIG.delivery_max_days} rejects'},
            {'Requirement':'VAT','Threshold':f'Must be shown separately at {CONFIG.vat_percent}%'},
            {'Requirement':'Quotation validity','Threshold':f'< {CONFIG.validity_min_days} days requires review'},
            {'Requirement':'Late delivery penalty','Threshold':f'At least {CONFIG.penalty_min_percent_per_week}% of {CONFIG.penalty_basis} per {CONFIG.penalty_period}'},
            {'Requirement':'Registration','Threshold':'Supplier TIN and BIN must both be present'},
            {'Requirement':'Arithmetic','Threshold':'Printed numbers must add up; no rounding tolerance is supplied'},
        ],hide_index=True,width='stretch')
    with request:
        try:
            st.dataframe(read_purchase_request(),hide_index=True,width='stretch')
        except ValueError as exc:
            st.error(str(exc))
    with ambiguities:
        st.info('These are documented interpretations and unresolved policy details, not additional company rules.')
        st.markdown('''
- **Payment after delivery and acceptance:** an unspecified acceptance deadline requires clarification.
- **Penalty adequacy:** the note requires at least 0.5% of order value per week, but assigns no Reject category for an inadequate rate or unclear basis. The existing engine retains human review; confirmed absence of a clause is a Reject condition.
- **VAT-inclusive pricing:** the purchase request does not state its tax basis. No net price or VAT amount is invented.
- **Rounding:** no monetary tolerance is supplied. The existing Decimal checks retain discrepancies for review.
- **Relative delivery:** no calendar deadline is inferred without a PO date. Clear weeks remain normalized to seven days each.
''')

