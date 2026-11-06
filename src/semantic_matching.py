"""Exact multi-item matching with explicit inventory and comparability guards."""
from collections import Counter
from src.trusted_facts import value_at
from src.trusted_models import ItemMatch, MatchingResult
from src.verification_conflicts import source_item_code
from src.verification_text import canonical


def same_text(a,b):
    return a is not None and b is not None and canonical(str(a)).casefold()==canonical(str(b)).casefold()


def match_trusted_items(facts, purchase_request, document):
    """PR contract has implicit BDT currency; never infer foreign conversions."""
    matches=[]
    reference=value_at(facts,'purchase_reference')
    codes=[value_at(facts,f'line_items[{i}].code') for i in range(len(facts.line_items))]
    source_codes=[source_item_code(line) for page in document.pages for line in page.text.splitlines()]
    source_codes=[code for code in source_codes if code is not None]
    inventory=(bool(source_codes) and Counter(source_codes)==Counter(codes) and None not in codes)
    for i,code in enumerate(codes):
        prefix=f'line_items[{i}]'
        candidates=[j for j,row in enumerate(purchase_request) if row.item_code==code and row.pr_number==reference]
        status='MATCHED';pr_index=None;reasons=[]
        if code is not None and codes.count(code)>1:
            status='DUPLICATE';reasons=['DUPLICATE_SUPPLIER_CODE']
        elif len(candidates)>1:
            status='AMBIGUOUS';reasons=['MULTIPLE_EXACT_PR_ROWS']
        elif not candidates:
            status='UNMATCHED';reasons=['NO_EXACT_CODE_AND_REFERENCE_MATCH']
        else:
            pr_index=candidates[0];row=purchase_request[pr_index]
            if not same_text(value_at(facts,prefix+'.description'),row.description):
                status='INCOMPATIBLE';reasons.append('DESCRIPTION_MISMATCH_OR_UNKNOWN')
            if not same_text(value_at(facts,prefix+'.unit'),row.unit):
                status='INCOMPATIBLE';reasons.append('UNIT_MISMATCH_OR_UNKNOWN')
            if value_at(facts,prefix+'.currency')!='BDT':
                status='INCOMPATIBLE';reasons.append('CURRENCY_MISMATCH_OR_UNKNOWN')
        matches.append(ItemMatch(supplier_index=i,status=status,pr_index=pr_index,
            reason_codes=tuple(reasons or ['EXACT_VERIFIED_MATCH'])))
    used=[m.pr_index for m in matches if m.status=='MATCHED']
    # A PR row may not be consumed twice even through a malformed identity set.
    matches=[m.model_copy(update={'status':'DUPLICATE','reason_codes':('PR_ROW_REUSED',)})
             if m.pr_index is not None and used.count(m.pr_index)>1 else m for m in matches]
    used={m.pr_index for m in matches if m.status=='MATCHED'}
    return MatchingResult(items=tuple(matches),missing_pr_indices=tuple(j for j in range(len(purchase_request)) if j not in used),
        inventory_complete=inventory)
