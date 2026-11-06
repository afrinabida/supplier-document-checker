"""Bounded field-role grammars, independent of the legacy extractor and policy."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import re

from src.verification_models import VerifiedValue
from src.verification_text import canonical, contains_value

NUMBER=r'[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)'
NUMBER_TOKEN=re.compile(r'(?<![\w.,])('+NUMBER+r')(?![\w.,])')
WORDS={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,
       'nine':9,'ten':10,'eleven':11,'twelve':12}
MONTHS={name:i+1 for i,name in enumerate(('january','february','march','april','may','june','july','august','september','october','november','december'))}
CURRENCIES=frozenset(('BDT','USD','EUR','GBP','JPY','CNY','INR','CAD','AUD','CHF','SGD','HKD','AED','SAR','NZD'))
REGISTRATION_LABELS={'TIN':r'e-TIN|TIN|Tax ID|Taxpayer Identification Number',
                     'BIN':r'BIN|VAT Reg(?:istration)?\.?[ \t]+(?:No\.?|Number)|Business Identification Number'}
LABELS={
 'unit_price':r'unit price|unit cost|price per unit',
 'quantity':r'quantity|qty', 'unit':r'unit', 'description':r'description',
 'line_total':r'line total|line amount', 'subtotal':r'subtotal(?: \(sum of line totals\))?',
 'total':r'grand total|total', 'amount':r'amount|tax amount|vat amount',
 'code':r'item code|item', 'currency':r'currency',
 'quotation_number':r'quotation (?:number|no\.?)|quote (?:number|no\.?)',
 'purchase_reference':r'purchase reference|pr reference|reference',
}


@dataclass(frozen=True)
class Support:
    supported: bool
    value: VerifiedValue | None = None
    reason: str = 'FIELD_ROLE_UNSUPPORTED'
    policy_review: bool = False
    context_key: str | None = None


def decimal_value(text):
    value=canonical(text)
    if len(value)>128 or not re.fullmatch(NUMBER,value):
        return None
    try:
        number=Decimal(value.replace(',',''))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def decimal_text(number):
    text=format(number,'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def role_segments(text,domain):
    patterns={'delivery':r'delivery\b','payment':r'(?:payment|full payment|balance(?: payment)?)\b',
              'valid':r'(?:valid|validity|quotation valid|expires)\b',
              'quotation_date':r'(?:quotation date|quote date)\b'}
    segments=re.split(r'\s*[;|]\s*|(?<=[.!?])\s+',canonical(text))
    return [s for s in segments if re.match(patterns[domain],s,re.I)]


def role_dates(text,domain):
    values=[]
    for segment in role_segments(text,domain):
        values.extend(dates_in(segment))
    return values


def labelled(text,label):
    pattern=LABELS.get(label,label)
    text='\n'.join(canonical(line) for line in text.splitlines())
    return [m[1].strip() for m in re.finditer(r'(?i)(?:^|[;|\n])\s*(?:'+pattern+
        r')(?:[ \t]*[:=][ \t]*|[ \t]*\n[ \t]*)([^;|\n]+)',text)]


def registration_label_pattern(kind):
    base=REGISTRATION_LABELS.get(kind)
    return r"(?:supplier(?:'s)?\s+)?(?:"+base+')' if base else None


def amount_parts(text):
    value=canonical(text).strip()
    match=re.fullmatch(r'(?:(?P<before>[A-Z]{3})\s*)?(?P<number>'+NUMBER+r')(?:\s*(?P<after>[A-Z]{3}))?',value)
    if not match or (match['before'] and match['after'] and match['before']!=match['after']):
        return None
    number=decimal_value(match['number'])
    return (number,match['before'] or match['after']) if number is not None else None


def date_value(text):
    value=canonical(text)
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            return date.fromisoformat(value).isoformat()
        m=re.fullmatch(r'(\d{1,2}) ([A-Za-z]+) (\d{4})',value)
        if m and m[2].lower() in MONTHS:
            return date(int(m[3]),MONTHS[m[2].lower()],int(m[1])).isoformat()
    except ValueError:
        return None
    return None


def dates_in(text):
    return [m[0] for m in re.finditer(r'\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2} [A-Za-z]+ \d{4}\b',text)]


def duration_parts(text):
    match=re.search(r'(?i)(?<![\w.,])('+NUMBER+'|'+ '|'.join(WORDS)+r')\s+(?:(calendar|business|working)\s+)?(days?|weeks?|months?)\b',canonical(text))
    if not match:
        return None
    number=Decimal(WORDS[match[1].lower()]) if match[1].lower() in WORDS else decimal_value(match[1])
    return (number,match[3].lower().rstrip('s'),match[2].lower() if match[2] else None)


def temporal_anchor(text):
    value=canonical(text).lower()
    if re.search(r'delivery\s+and\s+acceptance',value): return 'delivery and acceptance'
    if re.search(r'\b(?:after|of|from) delivery\b',value): return 'delivery'
    if re.search(r'\b(?:after|of|from|with) (?:the )?(?:po|purchase order)\b',value): return 'PO'
    if re.search(r'\b(?:after|of|from) invoice\b',value): return 'invoice'
    return None


def penalty_parts(text):
    value=canonical(text)
    if not re.search(r'(?i)penalt|liquidated damages|deduct',value): return None
    conditional=bool(re.search(r'(?i)\bif\b|\bunless|\bexcept|provided that|contingent|subject to|may be agreed|to be agreed|may be waived|proposed',value))
    waived=bool(re.search(r'(?i)\bwaived\b',value)) and not re.search(r'(?i)\bnot (?:be )?waived\b',value)
    negative=waived or bool(re.search(r'(?i)\bno (?:late-delivery |late delivery )?(?:penalt|liquidated damages)|\bnot (?:be )?(?:apply|applicable|payable)\b|shall not pay\b',value))
    polarity='conditional' if conditional else 'negated' if negative else 'operative'
    rates=[];caps=[]
    for match in re.finditer('('+NUMBER+r')\s*%',value):
        prefix=value[max(0,match.start()-35):match.start()]
        target=caps if re.search(r'(?i)(?:capped at|cap of|maximum of|up to|not exceed)\s*$',prefix) else rates
        target.append(decimal_value(match[1]))
    operative=re.split(r'(?i)\b(?:capped at|cap of|maximum of|up to|not exceed)\b',value)[0]
    weekly=bool(re.search(r'(?i)per week|each (?:full )?week|every (?:7|seven) days',operative))
    monthly=bool(re.search(r'(?i)per month|each month',operative))
    period='week' if weekly and not monthly else 'month' if monthly and not weekly else None
    basis='order value' if re.search(r'(?i)of (?:the )?order value',operative) else None
    return dict(polarity=polarity,rates=rates,caps=caps,period=period,basis=basis,
                late_delivery=bool(re.search(r'(?i)late[- ]delivery|\bdelay\b',value)),ambiguous_period=weekly and monthly)


def penalty_segments(text):
    segments=re.split(r'(?<=[.!?])\s+|;\s*(?=(?:no |late |penalty|liquidated |buyer))',canonical(text),flags=re.I)
    return [s for s in segments if re.search(r'(?i)penalt|liquidated damages|deduct',s)]


def tax_basis(text):
    value=canonical(text).lower()
    if re.search(r'\b(?:not|no)\b.{0,12}vat|vat (?:is )?not ',value): return None
    included=bool(re.search(r'vat[- ]inclusive|vat included|including vat|tax[- ]inclusive',value))
    separate=bool(re.search(r'vat[- ]exclusive|vat excluded|excluding vat|vat (?:is )?separate|plus vat',value))
    return 'conflicting' if included and separate else 'included' if included else 'separate' if separate else None


def _item_matches(context,code):
    if not code: return False
    anchors=re.findall(r'(?i)\bitem(?: code)?\s*(?::|=|\s)\s*([^\s:;|]+)',context)
    if not anchors or any(canonical(anchor)!=canonical(code) for anchor in anchors):
        return False
    values=labelled(context,'code')
    plain=re.search(r'(?i)(?:^|[;|])\s*item\s+([^:;|]+)\s*(?:[;|]|$)',context)
    return any(canonical(v)==canonical(code) for v in values) or bool(plain and canonical(plain[1])==canonical(code))


def _number_support(path,raw,context,code,currency):
    leaf=path.rsplit('.',1)[-1]
    label='amount' if leaf=='amount' else leaf
    if path.startswith('line_items[') and not _item_matches(context,code):
        return Support(False,reason='ITEM_SCOPE_UNPROVEN')
    if ('tax.' in path and not re.search(r'(?i)\bVAT|\btax',context)) or (path.startswith('payments[') and not re.search(r'(?i)\bpayment|\badvance|\bdeposit',context)):
        return Support(False,reason='AMOUNT_ROLE_UNPROVEN')
    candidates=labelled(context,label)
    requested=amount_parts(raw)
    if requested is None: return Support(False,reason='NUMBER_FORMAT_UNSUPPORTED')
    for candidate in candidates:
        parsed=amount_parts(candidate)
        if parsed and parsed[0]==requested[0]:
            bound=parsed[1]
            if bound and bound not in CURRENCIES: return Support(False,reason='CURRENCY_UNSUPPORTED')
            if requested[1] and requested[1]!=bound:
                return Support(False,reason='CURRENCY_MISMATCH')
            if leaf!='quantity' and currency and currency!=bound:
                return Support(False,reason='CURRENCY_MISMATCH' if bound else 'CURRENCY_UNPROVEN')
            return Support(True,VerifiedValue(kind='DECIMAL',decimal_value=parsed[0],
                currency=bound,unit='quantity' if leaf=='quantity' else None),context_key=code)
    return Support(False,reason='LABEL_VALUE_MISMATCH')


def support_value(path,raw,context,*,item_code=None,currency=None,registration_kind=None):
    """Prove a narrow source claim, otherwise return uncertainty, never a guess."""
    context='\n'.join(canonical(line) for line in context.splitlines())
    value=canonical(raw);leaf=path.rsplit('.',1)[-1]
    if path.startswith('line_items[') and not _item_matches(context,item_code):
        return Support(False,reason='ITEM_SCOPE_UNPROVEN')
    if path.startswith('delivery.') or '.timing.' in path or path.startswith('validity.') or path=='quotation_date':
        domain='delivery' if path.startswith('delivery.') else 'payment' if '.timing.' in path else 'quotation_date' if path=='quotation_date' else 'valid'
        segments=[s for s in role_segments(context,domain) if contains_value(s,raw)]
        if len(set(segments))!=1:
            return Support(False,reason='TEMPORAL_SCOPE_UNPROVEN',policy_review=True)
        context=segments[0]
    if not contains_value(context,raw):
        return Support(False,reason='RAW_VALUE_NOT_IN_CONTEXT')
    if path=='supplier.name' or path=='supplier.address':
        label=r'supplier(?: legal name| name)?' if leaf=='name' else r'supplier address|address'
        matches=labelled(context,label)
        if ':' in value:
            return Support(False,reason='PARTY_FIELD_BOUNDARIES_UNPROVEN')
        if leaf=='name' and (re.match(r'^\d',value) or re.search(r'(?i)\broad|street|avenue|postal|address\b',value)):
            return Support(False,reason='SUPPLIER_IS_ADDRESS')
        if not any(canonical(m)==value for m in matches):
            return Support(False,reason='SUPPLIER_ROLE_UNPROVEN')
        return Support(True,VerifiedValue(kind='TEXT',text_value=value))
    if '.registrations[' in path:
        aliases=registration_label_pattern(registration_kind)
        if aliases is None:
            return Support(False,reason='REGISTRATION_TYPE_UNSUPPORTED')
        if re.search(r'(?i)\bbuyer\b',context): return Support(False,reason='REGISTRATION_OWNER_UNPROVEN')
        if leaf=='value':
            if not re.fullmatch(r'\d+(?:-\d+)*',value) or not re.search('[1-9]',value):
                return Support(False,reason='IDENTIFIER_PLACEHOLDER_OR_FORMAT')
            if not any(canonical(m)==value for m in labelled(context,aliases)):
                return Support(False,reason='IDENTIFIER_ROLE_UNPROVEN')
            return Support(True,VerifiedValue(kind='IDENTIFIER',text_value=value))
        if leaf=='label' and re.fullmatch(aliases,value,re.I):
            return Support(True,VerifiedValue(kind='TEXT',text_value=value))
        if leaf=='owner_role' and re.search(r'(?i)\bsupplier\b',context) and value.lower()=='supplier':
            return Support(True,VerifiedValue(kind='TEXT',text_value='supplier'))
        return Support(False,reason='REGISTRATION_ROLE_UNSUPPORTED')
    if leaf in ('unit_price','line_total','subtotal','total','quantity','amount'):
        return _number_support(path,raw,context,item_code,currency)
    if leaf=='currency':
        codes=set(re.findall(r'(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])',context))
        if path.startswith('line_items[') and not _item_matches(context,item_code):
            return Support(False,reason='ITEM_SCOPE_UNPROVEN')
        if len(codes&CURRENCIES)>1:
            return Support(False,reason='MULTIPLE_CURRENCY_SCOPES',policy_review=True)
        if value in codes and value in CURRENCIES and re.search(r'(?i)\bcurrency|\bprice|\bamount|\btotal',context):
            return Support(True,VerifiedValue(kind='TEXT',text_value=value))
        return Support(False,reason='CURRENCY_UNPROVEN')
    if path.startswith('line_items[') and leaf in ('code','description','unit'):
        if not _item_matches(context,item_code): return Support(False,reason='ITEM_SCOPE_UNPROVEN')
        if leaf=='code' and value==item_code: return Support(True,VerifiedValue(kind='TEXT',text_value=value),context_key=item_code)
        if any(canonical(m)==value for m in labelled(context,leaf)):
            return Support(True,VerifiedValue(kind='TEXT',text_value=value),context_key=item_code)
        return Support(False,reason='ITEM_FIELD_ROLE_UNPROVEN')
    if path.startswith('penalties['):
        segments=[s for s in penalty_segments(context) if contains_value(s,raw)]
        if len(set(segments))!=1:
            return Support(False,reason='PENALTY_CLAUSE_SCOPE_AMBIGUOUS',policy_review=True)
        context=segments[0]
        parts=penalty_parts(context)
        if not parts: return Support(False,reason='PENALTY_CONTEXT_UNPROVEN')
        policy=parts['polarity']=='conditional' or parts['ambiguous_period']
        if not parts['late_delivery']:
            return Support(False,reason='LATE_DELIVERY_SCOPE_UNPROVEN',policy_review=True)
        if leaf in ('wording','conditions'):
            return Support(True,VerifiedValue(kind='TEXT',text_value=value),policy_review=policy)
        if leaf=='polarity':
            return Support(True,VerifiedValue(kind='POLARITY',text_value=parts['polarity']),policy_review=policy)
        if leaf in ('rate_percent','cap_percent'):
            if parts['polarity']!='operative': return Support(False,reason='NEGATED_OR_CONDITIONAL_RATE',policy_review=True)
            number=decimal_value(value.rstrip('%').strip())
            candidates=parts['rates'] if leaf=='rate_percent' else parts['caps']
            if number is not None and len(set(candidates))==1 and number in candidates:
                return Support(True,VerifiedValue(kind='DECIMAL',decimal_value=number,unit='percent',anchor=parts['period']),policy_review=bool(parts['caps']))
            return Support(False,reason='PENALTY_RATE_CAP_SCOPE')
        if leaf in ('period','basis'):
            actual=parts[leaf]
            if actual and (actual in value.lower() or value.lower()==actual):
                return Support(True,VerifiedValue(kind='TEXT',text_value=actual),policy_review=policy)
            return Support(False,reason='PENALTY_BASIS_UNSUPPORTED')
    if '.tax.' in path or path.startswith('tax.'):
        if leaf=='treatment':
            basis=tax_basis(context)
            if basis in ('included','separate'):
                return Support(True,VerifiedValue(kind='TAX_BASIS',text_value=basis))
            return Support(False,reason='TAX_BASIS_AMBIGUOUS',policy_review=True)
        if leaf=='rate_percent':
            matches=re.findall(r'(?i)\b(?:VAT|tax)\s+('+NUMBER+r')\s*%',context)
            number=decimal_value(value.rstrip('%').strip())
            if number is not None and any(decimal_value(m)==number for m in matches):
                return Support(True,VerifiedValue(kind='DECIMAL',decimal_value=number,unit='percent'))
            return Support(False,reason='TAX_RATE_UNPROVEN')
    if path.startswith('payments[') and leaf in ('kind','percent'):
        security=bool(re.search(r'(?i)security deposit',context))
        deposit=bool(re.search(r'(?i)\bdeposit\b',context))
        advance=bool(re.search(r'(?i)\badvance\b',context))
        if leaf=='kind':
            kind='security deposit' if security else 'deposit' if deposit else 'advance' if advance else None
            if kind: return Support(True,VerifiedValue(kind='TEXT',text_value=kind),policy_review=deposit)
        else:
            nums=re.findall('('+NUMBER+r')\s*%\s*(?:refundable\s+)?(?:security deposit|deposit|advance)',context,re.I)
            number=decimal_value(value.rstrip('%').strip())
            if number is not None and len(set(nums))==1 and any(decimal_value(m)==number for m in nums):
                return Support(True,VerifiedValue(kind='DECIMAL',decimal_value=number,unit='percent'),policy_review=deposit)
        return Support(False,reason='PAYMENT_TYPE_OR_PERCENT_UNPROVEN',policy_review=True)
    if leaf in ('calendar_date','quotation_date','expires_on'):
        marker=r'\bdelivery\b' if leaf=='calendar_date' and path.startswith('delivery.') else r'\bpayment|payable|due\b' if '.timing.' in path else r'\bquotation date|quote date\b' if leaf=='quotation_date' else r'\bvalid|expires\b'
        parsed=date_value(value)
        if re.search(r'(?i)\bnot|\bunless|\bif\b|subject to|provided that|tentative|estimated',context):
            return Support(False,reason='DATE_COMMITMENT_UNCERTAIN',policy_review=True)
        if parsed and re.search(marker,context,re.I) and any(date_value(m)==parsed for m in dates_in(context)):
            return Support(True,VerifiedValue(kind='DATE',text_value=parsed))
        return Support(False,reason='DATE_ROLE_OR_FORMAT_AMBIGUOUS',policy_review=True)
    if leaf in ('duration','duration_unit','days','anchor'):
        domain='payment' if '.timing.' in path else 'delivery' if path.startswith('delivery.') else 'valid'
        if not re.search(r'(?i)\b'+domain,context): return Support(False,reason='DURATION_ROLE_UNPROVEN')
        parts=duration_parts(context);anchor=temporal_anchor(context)
        if leaf=='anchor':
            if anchor and value.lower()==anchor.lower():
                return Support(True,VerifiedValue(kind='TEXT',text_value=anchor),policy_review=anchor=='delivery and acceptance')
            return Support(False,reason='ANCHOR_SCOPE_UNSUPPORTED',policy_review=True)
        if parts:
            number,unit,calendar=parts
            expected=decimal_value(value)
            if expected is None and value.lower() in WORDS: expected=Decimal(WORDS[value.lower()])
            if leaf=='duration_unit' and value.lower().rstrip('s')==unit:
                return Support(True,VerifiedValue(kind='TEXT',text_value=unit),policy_review=calendar in ('business','working'))
            if leaf in ('duration','days') and (expected==number or duration_parts(value)==parts):
                return Support(True,VerifiedValue(kind='DURATION',decimal_value=number,unit=unit,
                    anchor=anchor,text_value=calendar),policy_review=calendar in ('business','working') or anchor=='delivery and acceptance')
        return Support(False,reason='DURATION_UNPROVEN',policy_review=True)
    if leaf=='wording':
        domain='payment' if path.startswith('payments[') else 'delivery' if path.startswith('delivery.') else 'valid' if path.startswith('validity.') else None
        if domain and re.search(r'(?i)\b'+domain,context):
            return Support(True,VerifiedValue(kind='TEXT',text_value=value),policy_review='acceptance' in context.lower() or 'deposit' in context.lower())
    if leaf in ('quotation_number','purchase_reference') and value in labelled(context,leaf):
        return Support(True,VerifiedValue(kind='TEXT',text_value=value))
    return Support(False,reason='SEMANTIC_ROLE_NOT_DETERMINISTICALLY_CHECKABLE')


def normalize_supported(support,proposal,config):
    """Return (supported, value, reason). Never trust provider normalization tags."""
    value=support.value
    if value is None: return False,None,support.reason
    if proposal is None: return True,value,'DIRECT_SOURCE_VALUE'
    proposed=canonical(str(proposal))
    if value.kind=='DURATION':
        parsed=duration_parts(proposed)
        number=decimal_value(proposed)
        if number is not None and number==value.decimal_value:
            return True,value,'EXACT_DURATION'
        if parsed:
            count,unit,_=parsed
            if count==value.decimal_value and unit==value.unit: return True,value,'EXACT_DURATION'
            if value.unit=='week' and value.text_value=='calendar' and unit=='day':
                with localcontext() as ctx:
                    ctx.prec=256
                    if count==value.decimal_value*7:
                        return True,VerifiedValue(kind='DURATION',decimal_value=count,unit='day',anchor=value.anchor,text_value='calendar'),'CALENDAR_WEEKS_TO_DAYS'
        return False,None,'DURATION_CONVERSION_UNSUPPORTED'
    if value.kind=='DECIMAL':
        parsed=amount_parts(proposed.rstrip('%').strip())
        if not parsed: return False,None,'NUMERIC_INTERPRETATION_UNSUPPORTED'
        number,currency=parsed
        if currency and currency!=value.currency: return False,None,'CURRENCY_MISMATCH'
        if number==value.decimal_value: return True,value,'EXACT_DECIMAL'
        if value.unit is None and config.money_decimal_places is not None:
            with localcontext() as ctx:
                ctx.prec=256
                rounded=value.decimal_value.quantize(Decimal(1).scaleb(-config.money_decimal_places),rounding=config.rounding_mode)
                if number==rounded:
                    return True,VerifiedValue(kind='DECIMAL',decimal_value=number,currency=value.currency),'EXPLICIT_PRESENTATION_ROUNDING'
        return False,None,'NUMERIC_VALUE_CHANGED'
    if value.kind=='DATE':
        return (True,value,'UNAMBIGUOUS_DATE') if date_value(proposed)==value.text_value else (False,None,'DATE_INTERPRETATION_CHANGED')
    if value.kind=='TAX_BASIS':
        target=tax_basis(proposed) or (proposed if proposed in ('included','separate') else None)
        return (True,value,'TAX_BASIS_PRESERVED') if target==value.text_value else (False,None,'TAX_BASIS_CHANGED')
    if value.kind in ('TEXT','IDENTIFIER','POLARITY'):
        target=proposed
        if value.text_value in ('week','month'):
            target='week' if proposed.lower() in ('week','per week','weekly','every 7 days') else 'month' if proposed.lower() in ('month','per month','monthly') else proposed
        return (True,value,'DIRECT_TEXT_INTERPRETATION') if target==value.text_value else (False,None,'INTERPRETATION_CHANGED')
    return False,None,'NORMALIZATION_UNSUPPORTED'
