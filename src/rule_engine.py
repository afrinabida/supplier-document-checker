"""Independent, deterministic checks. Unknown never counts as a pass."""
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP, ROUND_HALF_EVEN
from contextlib import contextmanager
import re
from src.evidence import collect_evidence
from src.models import (ExtractionResult, PurchaseRequestItem, RuleEvaluation,
                        RuleResult, SourceEvidence)
from src.rules_config import CONFIG, RULE_BY_ID, UNCERTAINTY_WORDING


def _value(result, name):
    field = result.fields.get(name)
    return field.normalized if field and field.status == 'found' else None


def _number(value):
    if value is None or isinstance(value, (bool, float, dict)):
        return None
    try:
        value = Decimal(value)
        return value if value.is_finite() and value >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _text(value):
    return ' '.join(str(value).casefold().split()) if value is not None else None


def _price_basis_comparable(result, item):
    if item is None:
        return False
    treatment = _value(result, 'vat_treatment')
    if treatment == 'included':
        return item.price_tax_basis == 'included'
    # Preserve nominal price comparisons for separately stated VAT. An explicit
    # conflicting PR basis, missing treatment or conflicting quote bases blocks it.
    return treatment == 'separate' and item.price_tax_basis in (None, 'separate')


def _money_status(actual, expected):
    if actual is None or expected is None:
        return 'unknown'
    if actual == expected:
        return 'pass'
    # This is decimal currency presentation, not a mismatch tolerance. Preserve
    # extra precision and never round an already printed sub-cent discrepancy away.
    if actual.as_tuple().exponent != -2:
        return 'fail'
    rounded = expected.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    even = expected.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
    if rounded != even and actual in (rounded, even):
        return 'unknown'  # Tie-breaking policy is not supplied.
    return 'pass' if actual == rounded else 'fail'


def match_purchase_request(result: ExtractionResult, items: list[PurchaseRequestItem]):
    """Require one exact identifier/reference match and corroborating description/unit.

    Quantity and price are deliberately compared later; mismatches must not cause
    a different item to be selected. No fuzzy description or unit conversions.
    """
    code, reference = _value(result, 'item_code'), _value(result, 'reference')
    candidates = [item for item in items if item.item_code == code and item.pr_number == reference]
    if len(candidates) != 1:
        return None, 'No unique purchase-request row matches item identifier and PR reference.'
    item = candidates[0]
    if (_text(_value(result, 'item_description')) != _text(item.description) or
            _text(_value(result, 'unit')) != _text(item.unit) or
            _value(result, 'currency') != 'BDT'):
        return None, 'Identifier matches, but description, unit, or currency is missing or inconsistent; no conversion inferred.'
    if len(result.fields['item_code'].raw_values) != 1:
        return None, 'Multiple item rows or repeated tables are unsupported for automatic matching.'
    return item, 'Unique identifier/reference, description, unit and BDT currency match; quantity and price checked separately.'


def _complete_sample_layout(result):
    """Absence is actionable only within the recognized, complete text layout.

    This is a bounded layout check, not proof that every arbitrary PDF was read.
    Other layouts require review instead of asserting a clause is absent.
    """
    text = result.document.pages[0].text if len(result.document.pages) == 1 else ''
    required = ('QUOTATION', 'Quotation no.', 'TERMS AND CONDITIONS',
                'Payment.', 'Delivery.', 'Validity.', 'Quality.', 'Disputes.')
    return (all(token in text for token in required) and
            any(token in text for token in ('Authorised signatory', 'Authorized signatory')) and
            _value(result, 'item_code') is not None and
            not any(w.field == 'layout' for w in result.warnings))


def evaluate_rules(result: ExtractionResult, items: list[PurchaseRequestItem], *,
                   arithmetic_results: list[RuleResult] | None = None,
                   match_result: tuple | None = None,
                   infer_layout_absence: bool = True) -> RuleEvaluation:
    with _precision_context(result, items):
        return _evaluate_rules(result, items, arithmetic_results, match_result, infer_layout_absence)


def _precision_context(result, items):
    # Decimal's default precision can round unusually long inputs during products
    # and subtraction. Allocate precision from the inputs, not from a money tolerance.
    values = [_number(field.normalized) for field in result.fields.values()]
    values += [n for item in items for n in (item.quantity, item.maximum_unit_price)]
    values += [CONFIG.price_excess_percent, CONFIG.quantity_difference_percent,
               CONFIG.vat_percent, CONFIG.advance_max_percent, CONFIG.penalty_min_percent_per_week]
    numbers = [value for value in values if value is not None]
    span = max((value.adjusted() for value in numbers), default=0) - min(
        (value.as_tuple().exponent for value in numbers), default=0)
    context = localcontext()
    return _configured_context(context, max(28, 4 * (span + 1) + 16))


def _evaluate_rules(result: ExtractionResult, items: list[PurchaseRequestItem],
                    arithmetic_results=None, match_result=None, infer_layout_absence=True) -> RuleEvaluation:
    collect_evidence(result)  # Never evaluate a record with fabricated source quotes.
    checks = []
    def evidence(names, context=False):
        return _source_evidence(result, names, context)

    def emit(rule_id, status, actual, expected, names, explanation, context=False):
        definition = RULE_BY_ID.get(rule_id.split('.')[0])
        checks.append(RuleResult(
            rule_id=rule_id,
            wording=definition.wording if definition else UNCERTAINTY_WORDING,
            category=definition.category if definition else 'Needs a person to check',
            status=status, actual_value=actual, expected_value=expected,
            evidence=evidence(names, context), explanation=explanation))

    def condition(rule_id, violation, actual, expected, names, explanation):
        emit(rule_id, 'unknown' if violation is None else ('fail' if violation else 'pass'),
             actual, expected, names, explanation)

    item, match_explanation = match_result if match_result is not None else match_purchase_request(result, items)
    emit('MATCH', 'pass' if item else 'unknown',
         {name: _value(result, name) for name in ('reference', 'item_code', 'item_description', 'unit', 'currency')},
         item.model_dump(mode='json') if item else [i.model_dump(mode='json') for i in items],
         ['reference', 'item_code', 'item_description', 'unit', 'currency'], match_explanation)
    price = _number(_value(result, 'unit_price'))
    quantity = _number(_value(result, 'quantity'))
    price_ready = item is not None and price is not None and _price_basis_comparable(result, item)
    excess = (price - item.maximum_unit_price) if price_ready else None
    price_high = (excess * 100 > item.maximum_unit_price * CONFIG.price_excess_percent) if price_ready else None
    price_small = (0 < excess * 100 <= item.maximum_unit_price * CONFIG.price_excess_percent) if price_ready else None
    expected_price = {'maximum_unit_price': str(item.maximum_unit_price),
                      'excess_percent_boundary': str(CONFIG.price_excess_percent)} if item else 'A verified PR match'
    for rule_id, violation in [('R1', price_high), ('H1', price_small)]:
        condition(rule_id, violation, price, expected_price, ['unit_price', 'item_code'],
                  'Compare excess * 100 against PR maximum * percent only on compatible price bases; inclusive prices require an explicit matching PR tax basis. No tax conversion inferred.')
    quantity_bad = (abs(quantity-item.quantity) * 100 > item.quantity * CONFIG.quantity_difference_percent) if item and quantity is not None else None
    condition('H2', quantity_bad, quantity,
              {'requested': str(item.quantity), 'max_difference_percent': str(CONFIG.quantity_difference_percent)} if item else 'A verified PR match',
              ['quantity', 'item_code'], 'Absolute difference is checked in both directions; exact boundary is allowed.')

    advance = _number(_value(result, 'advance_percentage'))
    minimum_field = result.fields.get('advance_minimum_percentage')
    minimum_values = [_number(raw) for raw in minimum_field.raw_values] if minimum_field else []
    known_violation = any(value is not None and value > CONFIG.advance_max_percent for value in minimum_values)
    condition('R2', True if known_violation else (advance > CONFIG.advance_max_percent if advance is not None else None),
              advance, {'maximum_percent': CONFIG.advance_max_percent}, ['advance_percentage', 'advance_minimum_percentage', 'payment_terms'],
              'Only an unambiguous advance percentage (including explicit no advance) can pass.')

    # AI supplies independently verified facts, never template-derived absence.
    # The legacy entry retains its historical behavior explicitly by default.
    complete = _complete_sample_layout(result) if infer_layout_absence else False
    full_text = '\n'.join(p.text for p in result.document.pages)
    clause = _value(result, 'late_penalty_clause')
    # A parsed Delay section without penalty wording or a weekly rate is absent.
    # Preserve review for unsupported penalty wording elsewhere in the document.
    outside_delay = re.sub(r'(?ms)^\d+\. Delay\. .*?(?=^\d+\. |\Z)', '', full_text) if infer_layout_absence else ''
    presence = _value(result, 'late_penalty_presence')
    if presence is False:
        penalty_absent = True
    elif presence is True:
        penalty_absent = False
    elif 'late_penalty_presence' in result.fields:
        penalty_absent = None
    else:
        penalty_absent = (True if complete and not re.search(r'penalty|liquidated damages|deduct|late.delivery|\bdelay\b', outside_delay, re.I) else None) if clause is None else False
    # Keep absence context explicit rather than claiming that a search result is a quotation.
    emit('R3', 'unknown' if penalty_absent is None else ('fail' if penalty_absent else 'pass'),
         clause, 'A late-delivery penalty clause', ['late_penalty_clause', 'late_penalty_presence'],
         'Absent in complete recognized terms: Reject. Unrecognized wording/layout: review. Full-page evidence is search context, not a quote asserting absence.',
         context=clause is None)
    identifiers = {name: _value(result, name) for name in ('tin', 'bin')}
    missing_ids = [name for name, value in identifiers.items() if value is None]
    # Labels with unparsed contents indicate unsupported/ambiguous values, not proven absence.
    explicit_absent = [name for name in missing_ids if _value(result, name+'_presence') is False]
    absent_ids = [name for name in missing_ids if name not in result.fields and
                  name+'_presence' not in result.fields and
                  not re.search(r'\b'+name+r'\b', full_text, re.I)]
    id_violation = True if explicit_absent or (complete and absent_ids) else (None if missing_ids else False)
    emit('R4', 'unknown' if id_violation is None else ('fail' if id_violation else 'pass'), identifiers,
         'Both supplier TIN and BIN present', ['tin', 'bin', 'tin_presence', 'bin_presence'],
         'Absent identifiers in a recognized complete pack reject; ambiguous or unsupported registration data requires review. Page context supports the search.',
         context=bool(missing_ids))

    period = _value(result, 'delivery_period')
    days = _number(period.get('days')) if isinstance(period, dict) else None
    if not isinstance(period, dict) or period.get('anchor') not in ('PO', 'the date of PO', 'receiving the purchase order'):
        days = None
    if days is not None and days != days.to_integral_value():
        days = None
    condition('R5', days > CONFIG.delivery_max_days if days is not None else None, period,
              {'maximum_days_after_PO': CONFIG.delivery_max_days}, ['delivery_period', 'delivery_terms', 'delivery_date'],
              'PO-relative periods only. A calendar delivery date without a PO date cannot establish elapsed days.')
    condition('H4', CONFIG.delivery_review_min_days <= days <= CONFIG.delivery_max_days if days is not None else None,
              period, {'review_days_inclusive': [CONFIG.delivery_review_min_days, CONFIG.delivery_max_days]},
              ['delivery_period', 'delivery_terms'], '31 through 45 days requires review; beyond 45 is retained as a separate Reject finding.')

    payment = _value(result, 'payment_period')
    payment_days = _number(payment.get('days')) if isinstance(payment, dict) else None
    payment_clear = payment_days is not None and payment.get('anchor') == 'delivery'
    condition('H3', not (CONFIG.payment_min_days <= payment_days <= CONFIG.payment_max_days) if payment_clear else None,
              payment, {'days_after_delivery_inclusive': [CONFIG.payment_min_days, CONFIG.payment_max_days]},
              ['payment_period', 'payment_terms'],
              'Unclear ranges or delivery-and-acceptance anchors require review; no acceptance deadline is assumed.')

    treatment = _value(result, 'vat_treatment')
    vat_rate = _number(_value(result, 'vat_percentage'))
    vat_amount = _number(_value(result, 'vat_amount'))
    if treatment == 'included':
        vat_bad = True
    elif treatment == 'separate' and vat_rate is not None:
        vat_bad = vat_rate != CONFIG.vat_percent or vat_amount is None
    else:
        vat_bad = None
    condition('H5', vat_bad, {'treatment': treatment, 'percentage': vat_rate, 'amount': vat_amount},
              {'treatment': 'separate', 'percentage': CONFIG.vat_percent, 'amount': 'explicit'},
              ['vat_treatment', 'vat_percentage', 'vat_amount'], 'Tax-inclusive prices are not treated as separately shown VAT; no VAT is back-calculated.')

    checks.extend(arithmetic_results if arithmetic_results is not None else _arithmetic_checks(result))
    validity = _number(_value(result, 'validity_days'))
    condition('H7', validity < CONFIG.validity_min_days if validity is not None else None,
              validity, {'minimum_days': CONFIG.validity_min_days}, ['validity_days'], 'Exactly 30 days satisfies the validity minimum.')

    rate = _number(_value(result, 'late_penalty_percentage'))
    basis = _value(result, 'late_penalty_basis')
    basis_ok = isinstance(basis, dict) and basis.get('basis') == CONFIG.penalty_basis and basis.get('period') == CONFIG.penalty_period
    penalty_bad = (rate < CONFIG.penalty_min_percent_per_week) if rate is not None and basis_ok else None
    if 'late_penalty_cap' in result.fields and penalty_bad is False:
        penalty_bad = None  # Policy does not establish capped adequacy over all delays.
    condition('N1', penalty_bad, {'rate_percent': rate, 'basis': basis, 'cap_percent': _value(result,'late_penalty_cap')},
              {'minimum_percent': CONFIG.penalty_min_percent_per_week, 'basis': CONFIG.penalty_basis, 'period': CONFIG.penalty_period},
              ['late_penalty_clause', 'late_penalty_percentage', 'late_penalty_basis', 'late_penalty_cap'],
              'The note specifies a minimum but no Reject severity for an inadequate clause. Capped adequacy is unresolved. Route inadequate/unclear clauses to review; R3 separately rejects confirmed absence.')
    essential = ('supplier', 'quote_number', 'quote_date', 'reference', 'item_code', 'item_description', 'quantity', 'unit', 'currency')
    unresolved = [name for name in essential if _value(result, name) is None]
    if _value(result, 'delivery_date') is not None and period is not None:
        unresolved.append('calendar date and PO-relative delivery period cannot be reconciled without a PO date')
    warnings = [w.model_dump() for w in result.warnings]
    emit('INPUT', 'unknown' if unresolved or warnings else 'pass',
         {'unresolved_fields': unresolved, 'extraction_warnings': warnings}, 'Unambiguous supported extraction',
         list(result.fields), 'Extraction warnings and missing essential metadata prevent silent approval; specific missing rule fields are handled by their own checks.')
    # Stable company-rule order first; arithmetic detail and safety findings follow.
    order = {key: i for i, key in enumerate(RULE_BY_ID)}
    checks.sort(key=lambda check: order.get(check.rule_id, len(order)))
    return RuleEvaluation(filename=result.document.filename, matched_item=item, results=checks)


@contextmanager
def _configured_context(context, precision):
    with context as active:
        active.prec = precision
        yield


def _source_evidence(result, names, context=False):
    found = [e for name in names for e in (result.fields[name].evidence if name in result.fields else [])]
    if not found or context:
        # Absence has no invented quote. Entire inspected pages are search context.
        found.extend(SourceEvidence(filename=result.document.filename, page_number=p.page_number,
                                    quote=p.text) for p in result.document.pages if p.text)
    return list({(e.filename, e.page_number, e.quote): e for e in found}.values())


def _arithmetic_checks(result: ExtractionResult) -> list[RuleResult]:
    checks = []
    price = _number(_value(result, 'unit_price'))
    quantity = _number(_value(result, 'quantity'))
    treatment = _value(result, 'vat_treatment')
    vat_rate = _number(_value(result, 'vat_percentage'))
    vat_amount = _number(_value(result, 'vat_amount'))
    def emit(rule_id, status, actual, expected, names, explanation):
        checks.append(RuleResult(
            rule_id=rule_id, wording=RULE_BY_ID['H6'].wording,
            category=RULE_BY_ID['H6'].category, status=status,
            actual_value=actual, expected_value=expected,
            evidence=_source_evidence(result, names), explanation=explanation))
    def condition(rule_id, violation, actual, expected, names, explanation):
        emit(rule_id, 'unknown' if violation is None else ('fail' if violation else 'pass'),
             actual, expected, names, explanation)
    # Arithmetic checks compare exact printed decimals. No tolerance/rounding policy is invented.
    line = _number(_value(result, 'line_total'))
    subtotal = _number(_value(result, 'subtotal'))
    total = _number(_value(result, 'total'))
    code_field = result.fields.get('item_code')
    single_item = bool(code_field and code_field.status == 'found' and len(code_field.raw_values) == 1)
    arithmetic = []
    def arithmetic_check(suffix, actual, expected, names, applicable=True):
        status = ('not_applicable' if not applicable else
                  _money_status(actual, expected))
        emit('H6.'+suffix, status, actual, expected, names,
             'Compare Decimal amounts, accepting two-decimal BDT presentation rounding; ambiguous half-cent tie policy requires review. Printed sub-cent discrepancies remain failures.' if applicable else
             'Not checkable from separately stated amounts in this tax-inclusive layout; H5 retains the VAT issue.')
        arithmetic.append(status)
    arithmetic_check('line', line, quantity * price if single_item and quantity is not None and price is not None else None,
                     ['quantity', 'unit_price', 'line_total'])
    included = treatment == 'included'
    arithmetic_check('subtotal', subtotal, line if single_item else None,
                     ['line_total', 'subtotal'], not included or subtotal is not None)
    arithmetic_check('vat', vat_amount, subtotal * vat_rate / 100 if subtotal is not None and vat_rate is not None else None,
                     ['subtotal', 'vat_percentage', 'vat_amount'], not included)
    expected_total = line if included and single_item else (subtotal + vat_amount if subtotal is not None and vat_amount is not None else None)
    arithmetic_check('total', total, expected_total, ['line_total', 'subtotal', 'vat_amount', 'total'])
    condition('H6', True if 'fail' in arithmetic else None if 'unknown' in arithmetic else False,
              dict(zip(('line', 'subtotal', 'vat', 'total'), arithmetic)), 'All supported printed amounts add up',
              ['quantity', 'unit_price', 'line_total', 'subtotal', 'vat_percentage', 'vat_amount', 'total'],
              'All supported arithmetic findings are retained below H6; inconsistent line values are never silently corrected.')
    return checks


def evaluate_arithmetic(result: ExtractionResult) -> list[RuleResult]:
    collect_evidence(result)
    with _precision_context(result, []):
        return _arithmetic_checks(result)

