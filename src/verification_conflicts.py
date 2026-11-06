"""Source-wide supported-fact comparisons; no precedence or policy adjudication."""
from collections import defaultdict
from decimal import Decimal, localcontext, ROUND_HALF_UP, ROUND_HALF_EVEN
from hashlib import sha256
import re

from src.ingestion_models import SourceReference
from src.verification_models import SourceObservation, ConflictRecord
from src.verification_text import canonical
from src.verification_values import (labelled, amount_parts, dates_in, date_value,
    duration_parts, temporal_anchor, penalty_parts, tax_basis, decimal_text,
    role_segments, role_dates, penalty_segments, REGISTRATION_LABELS,registration_label_pattern)


def source_item_code(context):
    values=labelled(context,'code')
    if values: return values[0]
    match=re.search(r'(?i)(?:^|[;|])\s*item\s+([^:;|]+)\s*(?:[;|]|$)',context)
    return match[1].strip() if match else None


def item_scope(code):
    return sha256(canonical(code).encode('utf-8')).hexdigest()


def _comparison(key,values,classification,reason,paths=()):
    review=classification not in ('REPEATED_EQUIVALENT','COMPATIBLE_DETAIL')
    return ConflictRecord(key=key,classification=classification,observations=tuple(values),
        field_paths=tuple(paths),reason_code=reason,requires_human_review=review,
        explanation='Compare supported source observations without selecting a preferred clause or changing procurement policy.')


def source_observations(document):
    observations=[];arith=[]
    for page in document.pages:
        if page.has_unreadable_content: continue
        page_precedence=bool(re.search(r'(?i)amendment|supersed|precedence|revised|alternative|optional|joint supplier',page.text))
        lines=page.text.splitlines()
        for index,line in enumerate(lines):
            if line.rstrip().endswith(':') and index+1<len(lines): line+='\n'+lines[index+1]
            context=canonical(line)
            if not context: continue
            ref=SourceReference(document_id=document.metadata.document_id,page_number=page.page_number)
            uncertain_scope=bool(re.search(r'(?i)\bbetween\b|\bwindow\b|\brange\b|\bif\b|\bunless\b|provided that|\bfrom\b.*\bto\b',context))
            code=source_item_code(context)
            prefix=f'item[{item_scope(code)}].' if code else ''
            def add(key,value):
                if value is not None:
                    observations.append(SourceObservation(key=key,value=decimal_text(value) if isinstance(value,Decimal) else str(value),reference=ref,quote=line,
                        precedence_or_alternative=page_precedence,scope_uncertain=uncertain_scope))
            for name in labelled(context,r'supplier(?: legal name| name)?'):
                add('supplier.name',canonical(name))
            if not re.search(r'(?i)\bbuyer\b',context):
                for kind in REGISTRATION_LABELS:
                    for number in labelled(context,registration_label_pattern(kind)):
                        if re.fullmatch(r'\d+(?:-\d+)*',number): add('registration.'+kind,number)
            if re.search(r'(?i)\bdelivery\b',context):
                for value in role_dates(context,'delivery'): add('delivery.calendar_date',date_value(value))
                for segment in role_segments(context,'delivery'):
                    duration=duration_parts(segment)
                    if duration:
                        n,unit,calendar=duration
                        with localcontext() as ctx:
                            ctx.prec=256
                            if unit=='week' and calendar=='calendar': n*=7;unit='day'
                        add('delivery.duration|'+str(temporal_anchor(segment)),f'{decimal_text(n)} {calendar or "unspecified"} {unit}')
            if re.search(r'(?i)\bpayment|\bbalance|\bpayable',context):
                for segment in role_segments(context,'payment'):
                    duration=duration_parts(segment)
                    if duration:
                        n,unit,calendar=duration
                        kind='advance' if re.search(r'(?i)\badvance',segment) else 'deposit' if re.search(r'(?i)\bdeposit',segment) else 'balance'
                        key=f'payment.duration|{kind}|{temporal_anchor(segment) or "unknown"}'
                        add(key,f'{decimal_text(n)} {calendar or "unspecified"} {unit}')
            basis=tax_basis(context)
            if basis=='conflicting':
                add(prefix+'tax.treatment','included');add(prefix+'tax.treatment','separate')
            elif basis: add(prefix+'tax.treatment',basis)
            if not code:
                for raw in labelled(context,r'vat amount|tax amount'):
                    parsed=amount_parts(raw)
                    if parsed: add('tax.amount',f'{decimal_text(parsed[0])}|{parsed[1]}')
            for segment in penalty_segments(context):
                penalty=penalty_parts(segment)
                if penalty and penalty['late_delivery']:
                    add(prefix+'penalty.polarity',penalty['polarity'])
                    if penalty['polarity']=='operative':
                        for value in penalty['rates']: add(prefix+'penalty.rate',value)
                        for value in penalty['caps']: add(prefix+'penalty.cap',value)
                        add(prefix+'penalty.period',penalty['period'])
            if code:
                fields={}
                for name in ('quantity','unit_price','line_total'):
                    parsed=labelled(context,name)
                    fields[name]=amount_parts(parsed[0]) if len(parsed)==1 else None
                    if fields[name]: add(prefix+name,fields[name][0])
                    if fields[name] and fields[name][1]: add(prefix+name+'.currency',fields[name][1])
                for name in ('unit','description'):
                    for raw in labelled(context,name): add(prefix+name,canonical(raw))
                q,p,total=(fields[name] for name in ('quantity','unit_price','line_total'))
                if q and p and total and p[1]==total[1] and p[1] is not None:
                    with localcontext() as ctx:
                        # Two bounded 128-character inputs can produce a 256-digit
                        # integer; allow extra digits for presentation quantization.
                        ctx.prec=512
                        expected=q[0]*p[0]
                        # Printed precision is a possible presentation, not an
                        # authorized currency policy. Plausible rounding stays
                        # incomparable; only clear arithmetic differences conflict.
                        quantum=Decimal(1).scaleb(total[0].as_tuple().exponent)
                        rounded={expected.quantize(quantum,rounding=mode) for mode in (ROUND_HALF_UP,ROUND_HALF_EVEN)}
                    if total[0]!=expected:
                        obs=[SourceObservation(key=prefix+'arithmetic',value=str(x),reference=ref,quote=line) for x in (total[0],expected)]
                        arith.append(_comparison(prefix+'arithmetic',obs,
                            'INCOMPARABLE' if total[0] in rounded else 'CONTRADICTION',
                            'ROUNDING_POLICY_REQUIRED' if total[0] in rounded else 'LINE_ARITHMETIC_CONFLICT'))
            else:
                for name in ('subtotal','total'):
                    for raw in labelled(context,name):
                        parsed=amount_parts(raw)
                        if parsed: add('amounts.'+name,f'{decimal_text(parsed[0])}|{parsed[1]}')
    return observations,arith


def affected_paths(key,field_context):
    result=[]
    for path,meta in field_context.items():
        if key=='supplier.name' and path=='supplier.name': result.append(path)
        elif key.startswith('registration.') and '.registrations[' in path and meta.get('registration_kind')==key.split('.')[1]: result.append(path)
        elif key.startswith('delivery.') and path.startswith('delivery.'): result.append(path)
        elif key.startswith('payment.') and path.startswith('payments['): result.append(path)
        elif key.startswith('penalty.') and path.startswith('penalties['): result.append(path)
        elif key=='tax.treatment' and path.startswith('tax.'): result.append(path)
        elif key.startswith('amounts.') and path.startswith('amounts.'): result.append(path)
        elif key.startswith('item['):
            code=key.split(']',1)[0][5:]
            if path.startswith('line_items[') and meta.get('item_code') and item_scope(meta['item_code'])==code: result.append(path)
    return tuple(result)


def detect_source_conflicts(document,field_context):
    observations,math_results=source_observations(document)
    groups=defaultdict(list)
    for observation in observations: groups[observation.key].append(observation)
    results=[]
    for key,values in groups.items():
        distinct={v.value for v in values}
        if len(values)<2: continue
        if len(distinct)==1:
            kind='REPEATED_EQUIVALENT';reason='REPEATED_SOURCE_FACT'
        elif any(v.precedence_or_alternative for v in values):
            kind='PRECEDENCE_REVIEW';reason='AMENDMENT_OR_ALTERNATIVE_REQUIRES_REVIEW'
        elif any(v.scope_uncertain for v in values):
            kind='INCOMPARABLE';reason='RANGE_OR_CONDITIONAL_SCOPE_REQUIRES_REVIEW'
        elif key.endswith('penalty.polarity') and 'conditional' in distinct:
            kind='INCOMPARABLE';reason='PENALTY_CONDITIONS_REQUIRE_REVIEW'
        else:
            kind='CONTRADICTION';reason='INCOMPATIBLE_SOURCE_FACTS'
        results.append(_comparison(key,values,kind,reason,affected_paths(key,field_context)))
    for entry in math_results:
        results.append(entry.model_copy(update={'field_paths':affected_paths(entry.key,field_context)}))
    # Different payment anchors/obligations are not declared contradictory.
    payments=[v for v in observations if v.key.startswith('payment.duration')]
    if len({v.key for v in payments})>1:
        results.append(_comparison('payment.scopes',payments,'INCOMPARABLE',
            'DISTINCT_PAYMENT_SCOPES_REQUIRE_INTERPRETATION',affected_paths('payment.scopes',field_context)))
    deliveries=[v for v in observations if v.key.startswith('delivery.duration|')]
    if len({v.key for v in deliveries})>1:
        results.append(_comparison('delivery.scopes',deliveries,'INCOMPARABLE',
            'DISTINCT_DELIVERY_ANCHORS_REQUIRE_INTERPRETATION',affected_paths('delivery.scopes',field_context)))
    headers=[v for v in observations if v.key=='amounts.subtotal']
    totals=[v for v in observations if v.key=='amounts.total']
    rows=[v for v in observations if v.key.startswith('item[') and v.key.endswith('.line_total')]
    if headers and rows:
        # Without a proven complete item inventory/tax/charge scope, a difference
        # is a review signal, not an invented arithmetic violation.
        unique={v.key:v for v in rows}
        if len(unique)==len(rows):
            with localcontext() as ctx:
                ctx.prec=256
                summed=sum((Decimal(v.value) for v in rows),Decimal(0))
            if any(Decimal(v.value.split('|')[0])!=summed for v in headers):
                results.append(_comparison('amounts.header_lines',headers+rows,'INCOMPARABLE',
                    'HEADER_LINE_SCOPE_UNPROVEN',affected_paths('amounts.header_lines',field_context)))
    if totals and rows and len({v.key for v in rows})==len(rows):
        currencies=[v.value for v in observations if v.key.startswith('item[') and v.key.endswith('.line_total.currency')]
        taxes=[v for v in observations if v.key=='tax.amount']
        tax_bases={v.value for v in observations if v.key=='tax.treatment'}
        known_currency=len(currencies)==len(rows) and len(set(currencies))==1
        with localcontext() as ctx:
            ctx.prec=256
            expected=sum((Decimal(v.value) for v in rows),Decimal(0))
            if taxes and tax_bases=={'separate'}:
                expected+=sum((Decimal(v.value.split('|')[0]) for v in taxes),Decimal(0))
        for total in totals:
            amount,currency=total.value.split('|')
            bases_known=not taxes or len(tax_bases)==1
            aligned=known_currency and currency==currencies[0] and all(v.value.split('|')[1]==currency for v in taxes)
            compatible=bases_known and aligned and Decimal(amount)==expected
            results.append(_comparison('amounts.total_lines',[total,*rows,*taxes],
                'COMPATIBLE_DETAIL' if compatible else 'INCOMPARABLE',
                'EXPLICIT_TOTAL_RELATION_COMPATIBLE' if compatible else 'TOTAL_LINE_TAX_SCOPE_UNPROVEN',
                affected_paths('amounts.total_lines',field_context)))
    return tuple(results)
