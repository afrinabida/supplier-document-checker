"""Immutable transcription of company-rules.md; percentages are percentage points."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

@dataclass(frozen=True)
class RulesConfig:
    price_excess_percent: Decimal = Decimal('3')
    advance_max_percent: Decimal = Decimal('20')
    quantity_difference_percent: Decimal = Decimal('5')
    payment_min_days: int = 30
    payment_max_days: int = 60
    delivery_review_min_days: int = 31
    delivery_max_days: int = 45
    vat_percent: Decimal = Decimal('15')
    validity_min_days: int = 30
    penalty_min_percent_per_week: Decimal = Decimal('0.5')
    penalty_basis: str = 'order value'
    penalty_period: str = 'week'

@dataclass(frozen=True)
class RuleDefinition:
    rule_id: str
    category: Literal['Reject', 'Needs a person to check']
    wording: str

RULES = (
    RuleDefinition('R1', 'Reject', 'Unit price is more than 3% above the maximum unit price in the purchase request.'),
    RuleDefinition('R2', 'Reject', 'Advance payment is more than 20%.'),
    RuleDefinition('R3', 'Reject', 'The contract has no late-delivery penalty clause.'),
    RuleDefinition('R4', 'Reject', "The supplier's TIN or BIN is missing."),
    RuleDefinition('R5', 'Reject', 'Delivery is promised later than 45 days after the purchase order.'),
    RuleDefinition('H1', 'Needs a person to check', 'Unit price is above the maximum, but by no more than 3%.'),
    RuleDefinition('H2', 'Needs a person to check', 'Quantity differs from the quantity requested by more than 5%.'),
    RuleDefinition('H3', 'Needs a person to check', 'Payment terms are unclear, or outside 30 to 60 days after delivery.'),
    RuleDefinition('H4', 'Needs a person to check', 'Delivery is promised in 31 to 45 days.'),
    RuleDefinition('H5', 'Needs a person to check', 'VAT is not shown separately at 15%.'),
    RuleDefinition('H6', 'Needs a person to check', 'The numbers do not add up (for example line total or subtotal is wrong).'),
    RuleDefinition('H7', 'Needs a person to check', 'The quotation is valid for less than 30 days.'),
    RuleDefinition('N1', 'Needs a person to check', 'The late-delivery penalty must be at least 0.5% of order value per week of delay.'),
)
UNCERTAINTY_WORDING = 'If you are not sure, the answer is "Needs a person to check". Never guess.'
CONFIG = RulesConfig()
RULE_BY_ID = {rule.rule_id: rule for rule in RULES}
