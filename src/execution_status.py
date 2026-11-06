"""Presentation status only. Never computes or changes procurement decisions."""
TECHNICAL_FAILURE = 'Technical Processing Failed'
HUMAN_REVIEW = 'Human Review Required'
BUSINESS_DECISIONS = ('Approve', 'Reject', 'Needs a person to check')


def presentation_outcome(state, *, assessment=None):
    if state.get('safe_error') or state.get('error'):
        return {'status': TECHNICAL_FAILURE, 'business_decision': None}
    # A detailed view uses the actual decision-engine assessment. History uses
    # its already sanitized metadata; neither source is rewritten here.
    decision = assessment.get('decision') if assessment is not None else state.get('automated_decision')
    if decision not in BUSINESS_DECISIONS:
        return {'status': 'In progress', 'business_decision': None}
    return {'status': HUMAN_REVIEW if decision == 'Needs a person to check' else decision,
            'business_decision': decision}
