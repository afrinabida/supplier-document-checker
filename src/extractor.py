"""Pattern extraction for the inspected provided combined quotation/terms layout."""
import re
from datetime import date
from decimal import Decimal
from src.extraction_safety import (extract_registrations, extract_supplier,
                                   extract_penalty, guard_upfront_payments, has_unparsed_advance_wording)
from src.models import (PDFDocument, ExtractionResult, ExtractedField,
                        SourceEvidence, ExtractionWarning, MissingField)

NUMBER = r'\d[\d,]*(?:\.\d+)?'
EXPECTED_FIELDS = (
    'supplier', 'quote_number', 'quote_date', 'reference', 'item_code',
    'item_description', 'quantity', 'unit', 'currency', 'unit_price',
    'line_total', 'subtotal', 'total', 'delivery_terms', 'delivery_period',
    'payment_terms', 'payment_period', 'advance_percentage', 'validity_days',
    'vat_treatment', 'vat_percentage', 'vat_amount', 'tin', 'bin',
    'late_penalty_clause', 'late_penalty_percentage', 'late_penalty_basis',
)

def decimal(raw: str) -> str:
    return str(Decimal(raw.replace(',', '')))

def extract_fields(document: PDFDocument) -> ExtractionResult:
    result = ExtractionResult(document=document)

    def warn(name, message):
        result.warnings.append(ExtractionWarning(field=name, message=message))
        if name in result.fields:
            result.fields[name].status = 'ambiguous'
            result.fields[name].normalized = None

    def add(name, raw, normalized, page, quote):
        evidence = SourceEvidence(filename=document.filename, page_number=page.page_number, quote=quote)
        if name not in result.fields:
            result.fields[name] = ExtractedField(raw_values=[raw], normalized=normalized, evidence=[evidence])
        else:
            field = result.fields[name]
            field.raw_values.append(raw)
            field.evidence.append(evidence)
            if field.normalized != normalized:
                warn(name, 'Conflicting values found; no single value selected.')

    patterns = {
        'quote_number': (r'Quotation no\.\s*\n([^\n]+)', str),
        'quote_date': (r'(?m)^Date\n(\d{4}-\d{2}-\d{2})$', lambda s: date.fromisoformat(s).isoformat()),
        'delivery_date': (r'(?i)Delivery (?:by|on) (\d{4}-\d{2}-\d{2})', lambda s: date.fromisoformat(s).isoformat()),
        'reference': (r'Reference\n([^\n]+)', str),
        'subtotal': (rf'(?m)^Subtotal(?: \(BDT\))?\n({NUMBER})$', decimal),
        'total': (rf'(?m)^Total(?: \(all taxes included\)| \(BDT\))?\n({NUMBER})$', decimal),
        'vat_percentage': (rf'VAT ({NUMBER})%', decimal),
        'vat_amount': (rf'VAT {NUMBER}%(?: \(BDT\))?\n({NUMBER})', decimal),
        'validity_days': (r'valid for (\d+) days', int),
    }
    for page in document.pages:
        text = page.text
        if not text.strip():
            warn('layout', 'A page has no extractable text; document coverage is incomplete.')
        extract_supplier(text, page, add, warn)
        extract_registrations(text, page, add, warn)
        for name, (pattern, normalize) in patterns.items():
            for match in re.finditer(pattern, text):
                raw = match.group(1)
                try:
                    value = normalize(raw)
                except ValueError:
                    value = None
                add(name, raw, value, page, match.group())
                if value is None:
                    warn(name, 'Value could not be normalized.')
        # Column order is taken from the actual sample PDF text, not inferred from PR data.
        table = re.compile(rf'Item\nDescription\nQty\nUnit\n(?:Unit price \(BDT(?:, VAT included)?\)\s*Amount \(BDT\)|Price BDT)\n(?P<rows>(?:[A-Z]\n[^\n]+\n{NUMBER}\n[^\n]+\n{NUMBER}\n(?:Line amount \(BDT\)\n)?{NUMBER}\n)+)')
        for table_match in table.finditer(text):
            add('currency', 'BDT', 'BDT', page, table_match.group())
            row_pattern = rf'([A-Z])\n([^\n]+)\n({NUMBER})\n([^\n]+)\n({NUMBER})\n(?:Line amount \(BDT\)\n)?({NUMBER})\n'
            for row in re.finditer(row_pattern, table_match.group('rows')):
                for index, name in enumerate(('item_code', 'item_description', 'quantity', 'unit', 'unit_price', 'line_total'), 1):
                    raw = row.group(index)
                    value = decimal(raw) if name in ('quantity', 'unit_price', 'line_total') else raw
                    add(name, raw, value, page, row.group())
        for match in re.finditer(r'(?ms)^\d+\. (Payment|Delivery|Delay|Validity)\. (.*?)(?=^\d+\. |\Z)', text):
            kind, clause = match.group(1), match.group(2).rstrip()
            quote = match.group().rstrip()
            flat = re.sub(r'\s+', ' ', clause)
            if kind == 'Payment':
                add('payment_terms', clause, flat, page, quote)
                advances = re.findall(r'(\d+(?:\.\d+)?)% advance', flat, re.I)
                if 'No advance payment required' in clause:
                    add('advance_percentage', 'No advance payment required', '0', page, quote)
                for value in advances:
                    add('advance_percentage', value, decimal(value), page, quote)
                guard_upfront_payments(flat, page, quote, add, warn)
                periods = list(re.finditer(r'within (\d+) days (?:after|of) delivery', flat))
                for period in periods:
                    anchor = 'delivery and acceptance' if 'delivery and acceptance' in flat else 'delivery'
                    add('payment_period', period.group(), {'days': int(period[1]), 'anchor': anchor}, page, quote)
                if not periods:
                    add('payment_period', clause, None, page, quote)
                    warn('payment_period', 'No single payment period anchored to delivery; retain full terms for review.')
                elif len(re.findall(r'\bwithin\b', flat, re.I)) != len(periods):
                    warn('payment_period', 'Additional unsupported payment timing found; inspect all terms.')
            elif kind == 'Delivery':
                add('delivery_terms', clause, flat, page, quote)
                periods = list(re.finditer(r'within (\d+) (days|weeks) (?:of|from|after) (receiving the purchase order|the date of PO|PO)', flat))
                for period in periods:
                    days = int(period[1]) * (7 if period[2] == 'weeks' else 1)
                    add('delivery_period', period.group(), {'days': days, 'anchor': period[3]}, page, quote)
                if not periods and not re.search(r'Delivery (?:by|on) \d{4}-\d{2}-\d{2}', flat, re.I):
                    warn('delivery_period', 'Unrecognized delivery wording; no calendar date inferred.')
                elif periods and len(re.findall(r'\bwithin\b', flat, re.I)) != len(periods):
                    warn('delivery_period', 'Additional unsupported delivery timing found; inspect all terms.')
            elif kind == 'Delay':
                extract_penalty(clause, page, quote, add, warn)
        for match in re.finditer(r'VAT \d+(?:\.\d+)?%(?: \(BDT\))?\n'+NUMBER+r'|Total \(all taxes included\)|Unit price \(BDT, VAT included\)', text):
            value = 'included' if 'included' in match.group() else 'separate'
            add('vat_treatment', match.group(), value, page, match.group())
        outside_payment = re.sub(r'(?ms)^\d+\. Payment\. .*?(?=^\d+\. |\Z)', '', text)
        for line in outside_payment.splitlines():
            if (re.search(r'\b(?:deposit|upfront|up-front|down payment|advance payment)\b', line, re.I)
                    or has_unparsed_advance_wording(line)):
                add('payment_terms', line, None, page, line)
                warn('advance_percentage', 'Payment obligations outside recognized payment clauses require review.')
    if 'item_code' not in result.fields:
        warn('layout', 'Expected item table was not recognized; inspect original PDF.')
    elif len(result.fields['item_code'].raw_values) > 1:
        warn('item_code', 'Multiple item rows or repeated tables found; inspect candidates before matching items.')
    return result

def validate_structure(result: ExtractionResult) -> ExtractionResult:
    """Check extraction completeness, without applying company rules or arithmetic."""
    result.missing_fields = [MissingField(field=name) for name in EXPECTED_FIELDS if name not in result.fields]
    return result
