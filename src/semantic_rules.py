"""Verified-only bridge to the unchanged procurement rules and reducer."""
from decimal import Decimal, localcontext
from src.models import (PDFDocument, PDFPage, ExtractionResult, ExtractedField,
    SourceEvidence, RuleResult, RuleEvaluation)
from src.rule_engine import evaluate_rules, _money_status
from src.decision_engine import decide
from src.rules_config import CONFIG, RULE_BY_ID
from src.trusted_facts import fact_at, value_at
from src.ingestion_models import Completeness
from src.verification_text import canonical
from src.verification_conflicts import source_item_code
import re


def _item_source_tax(document,code):
    return code is not None and any(source_item_code(line)==code and re.search(r'(?i)\bvat\b|\btax\b',line)
        for page in document.pages for line in page.text.splitlines())


def _evidence(facts,paths,filename):
    values=[]
    for path in paths:
        f=fact_at(facts,path)
        if f:
            values.extend(SourceEvidence(filename=filename,page_number=e.source.page_number,quote=e.quote) for e in f.evidence)
    return list({(e.page_number,e.quote):e for e in values}.values())


def _coherent(facts,paths):
    """Compound interpretations require a shared verified clause, not joined facts."""
    groups=[]
    for path in paths:
        f=fact_at(facts,path)
        if not f or not f.authoritative: return False
        groups.append({(e.source.page_number,canonical(e.quote)) for e in f.evidence})
    return bool(groups and set.intersection(*groups))


def _rule(rule_id,status,explanation,*,evidence=(),actual=None,expected=None):
    definition=RULE_BY_ID.get(rule_id.split('.')[0])
    return RuleResult(rule_id=rule_id,wording=definition.wording if definition else 'Unresolved verified input requires review.',
        category=definition.category if definition else 'Needs a person to check',status=status,
        actual_value=actual,expected_value=expected,evidence=list(evidence),explanation=explanation)


def _record(facts,document,index):
    filename='document.pdf'  # Caller filename is untrusted and unnecessary for policy.
    result=ExtractionResult(document=PDFDocument(filename=filename,pages=[PDFPage(page_number=p.page_number,text=p.text) for p in document.pages]))
    def put(name,paths,value=None):
        if isinstance(paths,str): paths=[paths]
        if value is None:
            if len(paths)!=1: return
            value=value_at(facts,paths[0])
        if value is None: return
        evidence=_evidence(facts,paths,filename)
        if not evidence: return
        result.fields[name]=ExtractedField(raw_values=[str(value)],normalized=str(value) if isinstance(value,Decimal) else value,evidence=evidence)
    # Missing claims stay missing. The caller disables layout-based absence
    # inference explicitly; unrelated quotes need not act as presence sentinels.
    for name,path in {'supplier':'supplier.name','quote_number':'quotation_number','quote_date':'quotation_date',
        'reference':'purchase_reference','subtotal':'amounts.subtotal','total':'amounts.total',
        'vat_treatment':'tax.treatment','vat_percentage':'tax.rate_percent','vat_amount':'tax.amount',
        'validity_days':'validity.days'}.items(): put(name,path)
    validity=fact_at(facts,'validity.days')
    if validity and validity.normalized and validity.normalized.unit!='day': result.fields.pop('validity_days',None)
    for j,registration in enumerate(facts.supplier.registrations):
        if registration.kind not in ('TIN','BIN'): continue
        paths=[f'supplier.registrations[{j}].value']
        siblings=[k for k,r in enumerate(facts.supplier.registrations) if r.kind==registration.kind]
        if len(siblings)==1: put(registration.kind.lower(),paths)
    p=f'line_items[{index}]'
    for name,leaf in {'item_code':'code','item_description':'description','quantity':'quantity',
        'unit':'unit','currency':'currency','unit_price':'unit_price','line_total':'line_total'}.items(): put(name,p+'.'+leaf)
    # Item-specific basis overrides document basis; unknown explicit item tax must
    # not be erased by a document-level default.
    tax_facts=[f for f in facts.line_items[index].tax.facts if f.raw_values]
    if tax_facts or _item_source_tax(document,value_at(facts,p+'.code')):
        for name in ('vat_treatment','vat_percentage','vat_amount'):
            result.fields.pop(name,None)
        put('vat_treatment',p+'.tax.treatment')
        put('vat_percentage',p+'.tax.rate_percent')
        put('vat_amount',p+'.tax.amount')
    # Currency of a price must independently agree with its item currency.
    for leaf in ('unit_price','line_total'):
        f=fact_at(facts,p+'.'+leaf)
        if not f or not f.normalized or f.normalized.currency!=value_at(facts,p+'.currency'):
            result.fields.pop(leaf,None)
    duration=fact_at(facts,'delivery.duration')
    if _coherent(facts,['delivery.duration','delivery.anchor']) and duration.normalized.unit=='day':
        put('delivery_period',['delivery.duration','delivery.anchor'],dict(days=str(duration.normalized.decimal_value),anchor=value_at(facts,'delivery.anchor')))
    put('delivery_date','delivery.calendar_date')
    advances=[];timings=[]
    for j,group in enumerate(facts.payments):
        prefix=f'payments[{j}]'
        if _coherent(facts,[prefix+'.kind',prefix+'.percent']) and value_at(facts,prefix+'.kind')=='advance':
            percentage=value_at(facts,prefix+'.percent')
            if percentage is not None and percentage>=0: advances.append((percentage,prefix))
        duration=fact_at(facts,prefix+'.timing.duration')
        if (_coherent(facts,[prefix+'.timing.duration',prefix+'.timing.anchor',prefix+'.wording'])
                and duration.normalized.unit=='day'):
            timings.append((duration,prefix))
    # A standalone verified violation survives other unknown obligations. Multiple
    # smaller obligations are never summed or assumed to be alternatives.
    violations=[pair for pair in advances if pair[0]>CONFIG.advance_max_percent]
    if violations or len(advances)==1:
        percentage,prefix=max(violations or advances,key=lambda x:x[0])
        put('advance_percentage',[prefix+'.kind',prefix+'.percent'],str(percentage))
    if len(timings)==1:
        duration,prefix=timings[0]
        put('payment_period',[prefix+'.timing.duration',prefix+'.timing.anchor'],
            dict(days=str(duration.normalized.decimal_value),anchor=value_at(facts,prefix+'.timing.anchor')))
    # Multiple clauses, caps, or conditions are never collapsed into a policy pass.
    if len(facts.penalties)==1:
        prefix='penalties[0]'
        if _coherent(facts,[prefix+'.wording',prefix+'.polarity']):
            polarity=value_at(facts,prefix+'.polarity')
            if polarity in ('operative','negated'):
                put('late_penalty_presence',[prefix+'.wording',prefix+'.polarity'],polarity=='operative')
                put('late_penalty_clause',prefix+'.wording')
            if polarity=='operative':
                put('late_penalty_percentage',prefix+'.rate_percent')
                if _coherent(facts,[prefix+'.rate_percent',prefix+'.basis',prefix+'.period']):
                    put('late_penalty_basis',[prefix+'.basis',prefix+'.period'],
                        dict(basis=value_at(facts,prefix+'.basis'),period=value_at(facts,prefix+'.period')))
                put('late_penalty_cap',prefix+'.cap_percent')
    return result


def _arithmetic(facts,matching):
    """Exact Decimal relational checks; the existing money presentation policy."""
    results=[];lines=[];currencies=[]
    def check(suffix,actual,expected,paths):
        status=_money_status(actual,expected)
        results.append(_rule('H6.'+suffix,status,'Verified Decimal amounts compared using the existing presentation rule.',
            evidence=_evidence(facts,paths,'document.pdf'),actual=actual,expected=expected))
        return status
    with localcontext() as ctx:
        ctx.prec=1024
        for i in range(len(facts.line_items)):
            p=f'line_items[{i}]'
            q,price,total=(value_at(facts,p+'.'+leaf) for leaf in ('quantity','unit_price','line_total'))
            currency=value_at(facts,p+'.currency');currencies.append(currency)
            pf,tf=(fact_at(facts,p+'.'+leaf) for leaf in ('unit_price','line_total'))
            compatible=bool(currency=='BDT' and pf and pf.normalized and tf and tf.normalized and
                pf.normalized.currency==tf.normalized.currency==currency and
                _coherent(facts,[p+'.quantity',p+'.unit_price',p+'.line_total']))
            check('line_'+str(i),total if compatible else None,q*price if compatible and q is not None and price is not None else None,
                [p+'.quantity',p+'.unit_price',p+'.line_total'])
            lines.append(total)
        subtotal=value_at(facts,'amounts.subtotal');tax=value_at(facts,'tax.amount');rate=value_at(facts,'tax.rate_percent');total=value_at(facts,'amounts.total')
        aligned=bool(lines and matching.inventory_complete and all(x is not None for x in lines) and set(currencies)=={'BDT'})
        for path in ('amounts.subtotal','amounts.total','tax.amount'):
            f=fact_at(facts,path)
            aligned=aligned and bool(f and f.normalized and f.normalized.currency=='BDT')
        basis=value_at(facts,'tax.treatment')=='separate'
        expected=sum(lines,Decimal(0)) if aligned else None
        check('subtotal',subtotal if aligned else None,expected,['amounts.subtotal'])
        check('vat',tax if aligned and basis else None,subtotal*rate/100 if aligned and basis and subtotal is not None and rate is not None else None,['tax.amount','tax.rate_percent','amounts.subtotal'])
        check('total',total if aligned and basis else None,subtotal+tax if aligned and basis and subtotal is not None and tax is not None else None,['amounts.total','amounts.subtotal','tax.amount'])
    statuses=[r.status for r in results]
    results.append(_rule('H6','fail' if 'fail' in statuses else 'unknown' if 'unknown' in statuses else 'pass',
        'All material item and document arithmetic checks are retained.',evidence=[e for r in results for e in r.evidence]))
    return results


def evaluate_trusted_rules(facts,document,purchase_request,matching):
    """Run all checks without aggregation, for explicit orchestration stages."""
    arithmetic=_arithmetic(facts,matching)
    evaluations=[];checks=[]
    for match in matching.items:
        record=_record(facts,document,match.supplier_index)
        item=purchase_request[match.pr_index] if match.status=='MATCHED' else None
        # Semantic comparisons require an explicit PR tax basis, even for separate VAT.
        if item and (item.price_tax_basis is None or record.fields.get('vat_treatment') is None or
                     record.fields['vat_treatment'].normalized!=item.price_tax_basis):
            record.fields.pop('unit_price',None)
        evaluation=evaluate_rules(record,purchase_request,arithmetic_results=arithmetic,
            match_result=(item,'Verified exact item match.' if item else 'Unresolved item identity, unit or currency.'),
            infer_layout_absence=False)
        evaluations.append(evaluation);checks.extend(evaluation.results)
    if not evaluations:
        # Run every required rule even when the provider supplied no items.
        empty=ExtractionResult(document=PDFDocument(filename='document.pdf',pages=[]))
        evaluation=evaluate_rules(empty,purchase_request,arithmetic_results=arithmetic,
            match_result=(None,'No verified items.'),infer_layout_absence=False)
        checks.extend(evaluation.results)
    uncertainties=[]
    if facts.completeness!=Completeness.COMPLETE or not facts.extraction_complete: uncertainties.append('INCOMPLETE_DOCUMENT_OR_EXTRACTION')
    if not matching.inventory_complete: uncertainties.append('ITEM_INVENTORY_UNPROVEN')
    if matching.missing_pr_indices or any(m.status!='MATCHED' for m in matching.items): uncertainties.append('UNRESOLVED_ITEM_MATCHING')
    if facts.conflicts: uncertainties.append('SOURCE_CONFLICTS_OR_SCOPE_QUESTIONS')
    # Item-specific tax cannot inherit document defaults or disappear from the
    # claims. Allocation between item taxes and document tax is not authorized.
    for i,item in enumerate(facts.line_items):
        if any(f.raw_values for f in item.tax.facts) or _item_source_tax(document,value_at(facts,f'line_items[{i}].code')):
            uncertainties.append('ITEM_TAX_ALLOCATION_REQUIRES_REVIEW')
    # Unknown optional attributes are preserved without making every optional null
    # a new policy requirement. Observed unsupported interpretations block approval.
    if any(f.raw_values and not f.authoritative for f in facts.facts): uncertainties.append('OBSERVED_UNRESOLVED_FACTS')
    # Source-bearing scope inventory: every nonblank line must be accounted for.
    # An unsupported clause cannot disappear merely because no known keyword was
    # detected. This conservative profile accepts explicit supported source forms;
    # it does not assert universal semantic completeness for arbitrary contracts.
    represented={canonical(e.quote) for f in facts.facts if f.authoritative for e in f.evidence}
    for page in document.pages:
        for line in page.text.splitlines():
            if canonical(line) and canonical(line) not in represented:
                uncertainties.append('UNACCOUNTED_MATERIAL_SOURCE_CLAUSE')
    # Multiple advance observations cannot pass by selecting a lower obligation.
    advance_count=sum(value_at(facts,f'payments[{j}].kind')=='advance' for j in range(len(facts.payments)))
    if advance_count!=1: uncertainties.append('ADVANCE_OBLIGATION_SCOPE_UNRESOLVED')
    uncertainties=list(dict.fromkeys(uncertainties))
    checks.append(_rule('SEMANTIC_SAFETY','unknown' if uncertainties else 'pass',
        'All unresolved scope and coverage findings are retained.',actual=uncertainties))
    return RuleEvaluation(filename='document.pdf',results=checks),tuple(evaluations),tuple(uncertainties)


def evaluate_trusted_facts(facts,document,purchase_request,matching):
    """Preserved Step 6 public API: checks followed by Reject-first reduction."""
    evaluation,evaluations,uncertainties=evaluate_trusted_rules(facts,document,purchase_request,matching)
    return decide(evaluation),evaluations,uncertainties
