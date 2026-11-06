"""Conservative guards for the legacy extractor, not a general semantic parser."""
import re
from decimal import Decimal

REGISTRATION_LABELS = {
    'tin': r'e[- ]?TIN|TIN|Tax ID|Taxpayer Identification Number',
    'bin': r'BIN|VAT Reg(?:istration)?\.?[ \t]+(?:No\.?|Number)|Business Identification Number',
}

# An unclassified identifier-like label prevents an absence claim. It never
# supplies an identifier, and is kept separate from the supported alias parser.
REGISTRATION_HINTS = {
    'tin': r'\b(?:taxpayer|tax[ \t]+(?:id|identification))\b',
    'bin': r'\b(?:VAT[ \t]+reg\w*|business[ \t]+identification)\b',
}


def extract_registrations(text, page, add, warn):
    labels = '|'.join(REGISTRATION_LABELS.values())
    matches = list(re.finditer(r'(?i)(?<!\w)('+labels+r')[ \t]*:[ \t]*', text))
    for index, match in enumerate(matches):
        end = text.find('\n', match.end())
        end = len(text) if end < 0 else end
        if index+1 < len(matches):
            end = min(end, matches[index+1].start())
        raw = text[match.end():end].strip()
        quote = text[match.start():end].rstrip()
        name = next(key for key, pattern in REGISTRATION_LABELS.items()
                    if re.fullmatch(pattern, match[1], re.I))
        prefix = text[text.rfind('\n', 0, match.start())+1:match.start()]
        if re.search(r'\b(buyer|customer)\b', prefix, re.I):
            add(name+'_presence', raw, None, page, quote)
            warn(name, 'Registration ownership is not verified as supplier data.')
        elif re.fullmatch(r'\d+(?:-\d+)*', raw) and re.search('[1-9]', raw):
            add(name, raw, raw, page, quote)
            add(name+'_presence', raw, True, page, quote)
        elif not raw:
            add(name+'_presence', quote, None, page, quote)
            warn(name, 'Registration label has no supported same-line value; inspect the source.')
        elif re.fullmatch(r'[-_\s]+|N/?A|not[- ](?:provided|available)|none|missing', raw, re.I):
            add(name+'_presence', raw or quote, False, page, quote)
        else:
            add(name+'_presence', raw, None, page, quote)
            warn(name, 'Registration value is unsupported or unverified; absence is not proven.')
    for name, pattern in REGISTRATION_HINTS.items():
        supported = r'(?i)(?<!\w)(?:'+REGISTRATION_LABELS[name]+r')[ \t]*:'
        for line in text.splitlines():
            if re.search(pattern, line, re.I) and not re.search(supported, line):
                add(name+'_presence', line, None, page, line)
                warn(name, 'Registration-like wording is unclassified; absence is not proven.')


def is_address(text):
    return bool(re.search(r'(?i)^\s*\d|\b(?:road|street|avenue|lane|postal|P\.?O\.? box|address)\b', text))


def extract_supplier(text, page, add, warn):
    heading = re.search(r'(?m)^(?:QUOTATION|SUPPLIER QUOTATION & TERMS - FICTIONAL TEST DATA)\s*$', text)
    if not heading:
        return
    candidates = []
    for line in text[:heading.start()].splitlines():
        raw = line.strip()
        name = re.sub(r'(?i)^supplier\s*:\s*', '', raw)
        if name and re.search('[A-Za-z]', name) and not is_address(name):
            candidates.append((raw, name))
    for raw, name in candidates:
        add('supplier', raw, name, page, raw)
    if not candidates or len({name.casefold() for _,name in candidates}) != 1:
        warn('supplier', 'Supplier identity is missing or competing in the quotation header.')


def extract_penalty(clause, page, quote, add, warn):
    flat = re.sub(r'\s+', ' ', clause)
    relevant = re.search(r'penalt|liquidated damages|deduct|late.delivery.*(?:shall pay|must pay)', flat, re.I)
    weekly = re.search(r'per week|each (?:full )?week|every (?:7|seven) days', flat, re.I)
    if not relevant and not (weekly and '%' in flat):
        # The caller has identified a Delay section. A financial obligation here
        # cannot establish absence merely because its wording/period is unknown.
        obligation = (re.search(r'\b(?:supplier|seller)\b.*\b(?:shall|must|may|will|is required to)\s+(?:not\s+)?pay\b', flat, re.I)
                      and re.search(r'\d+(?:\.\d+)?\s*%|\b(?:BDT|USD|amount)\b', flat, re.I))
        if obligation:
            add('late_penalty_clause', clause, flat, page, quote)
            add('late_penalty_presence', clause, None, page, quote)
            warn('late_penalty_clause', 'Monetary delay obligation has unsupported structure; absence is not proven.')
        return
    add('late_penalty_clause', clause, flat, page, quote)
    negative = re.search(r'\bno\s+(?:(?:late[- ]delivery|weekly|monthly)\s+)?(?:penalt|liquidated damages)|\b(?:penalty|damages)\b.{0,100}(?:not apply|not be applied|(?<!not )waived)|shall not (?:apply|pay)', flat, re.I)
    uncertain = re.search(r'may be agreed|to be agreed|proposed|subject to|unless|except', flat, re.I)
    if negative:
        add('late_penalty_presence', clause, False if not uncertain else None, page, quote)
        return
    add('late_penalty_presence', clause, None if uncertain else True, page, quote)
    if uncertain:
        warn('late_penalty_clause', 'Penalty applicability is conditional or unverified.')
        return
    for rate in re.finditer(r'(\d+(?:\.\d+)?)%', flat):
        prefix = flat[max(0,rate.start()-35):rate.start()]
        name = 'late_penalty_cap' if re.search(r'(?:capped at|cap of|maximum of|up to|not exceed)\s*$', prefix, re.I) else 'late_penalty_percentage'
        add(name, rate[1], str(Decimal(rate[1])), page, quote)
    # A cap's order-value basis cannot establish the base of the weekly rate.
    operative = re.split(r'\b(?:capped at|cap of|maximum of|up to|not exceed)\b', flat, flags=re.I)[0]
    if weekly and re.search(r'of (?:the )?order value', operative, re.I):
        add('late_penalty_basis', clause, {'basis':'order value','period':'week',
            'full_weeks_only':bool(re.search(r'each full week',flat,re.I))}, page, quote)
    else:
        warn('late_penalty_basis', 'Penalty calculation basis is not fully specified.')


def has_unparsed_advance_wording(text):
    """Financial pre-delivery language, without treating advance notice as money."""
    for sentence in re.split(r'(?<=[.!?])\s+|\n', text):
        if (re.search(r'\bin advance\b', sentence, re.I) and re.search(
                r'\b(?:pay(?:ment|able)?|paid|instalments?|installments?|deposit|remit(?:tance)?)\b', sentence, re.I)):
            return True
        if re.search(r'\badvance\s*(?:payment|instalment|installment)?\s*(?:of|is|totals?)?\s*[:=]?\s*\d+(?:\.\d+)?\s*%', sentence, re.I):
            return True
        if (re.search(r'\b(?:instalment|installment)s?\b', sentence, re.I)
                and re.search(r'\b(?:before|prior to)\s+(?:delivery|dispatch|shipping|shipment)\b|\b(?:with|on|at)\s+(?:the\s+)?(?:PO|purchase order)\b', sentence, re.I)):
            return True
    return False


def guard_upfront_payments(flat, page, quote, add, warn):
    advances = list(re.finditer(r'(\d+(?:\.\d+)?)%\s*advance\b', flat, re.I))
    affirmative = []
    for match in advances:
        prefix = flat[max(0,match.start()-30):match.start()]
        suffix = flat[match.end():].split('.')[0]
        if re.search(r'\b(?:no|not|without)\s*$',prefix,re.I) or re.search(r'\bnot required|\bnot payable',suffix,re.I):
            continue
        affirmative.append(match)
    if affirmative:
        # A known individual violation remains actionable even if other obligations
        # are ambiguous. This is a lower bound, never an invented sum.
        minimum = max(Decimal(m[1]) for m in affirmative)
        add('advance_minimum_percentage', str(minimum), str(minimum), page, quote)
    deposit = re.search(r'\b(?:deposit|upfront|up-front|down payment)\b', flat, re.I)
    events = set()
    for match in advances:
        tail = flat[match.end():].split('.')[0].split(',')[0]
        for event in re.findall(r'\bPO\b|purchase order|shipping|delivery',tail,re.I):
            events.add('order' if event.lower() in ('po','purchase order') else event.lower())
    multiple = len(advances)>1 and (len(events)>1 or bool(re.search(r'\b(?:another|additional|separate|plus|second)\b|\band\b[^.]*%\s*advance',flat,re.I)))
    if deposit or multiple or len(affirmative)!=len(advances) or has_unparsed_advance_wording(flat):
        warn('advance_percentage', 'Deposit classification or multiple payment obligations require policy review; no sum inferred.')
