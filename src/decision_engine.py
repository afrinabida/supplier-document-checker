"""Priority reduction after ALL rules have run; no short-circuit evaluation."""
from src.models import RuleEvaluation, SupplierDecision
from src.rules_config import RULE_BY_ID


def decide(evaluation: RuleEvaluation) -> SupplierDecision:
    required = set(RULE_BY_ID) | {'MATCH', 'INPUT'}
    if not required.issubset({r.rule_id for r in evaluation.results}):
        raise ValueError('Incomplete evaluation: all company rules, matching and input checks must run before deciding.')
    reasons = [r for r in evaluation.results if r.status in ('fail', 'unknown')]
    rejects = [r for r in reasons if r.status == 'fail' and r.category == 'Reject']
    decision = 'Reject' if rejects else 'Needs a person to check' if reasons else 'Approve'
    return SupplierDecision(filename=evaluation.filename, decision=decision,
                            evaluation=evaluation, reasons=reasons)
