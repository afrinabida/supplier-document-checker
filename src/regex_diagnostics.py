"""Optional read-only value comparison, never verification or a procurement veto."""
from collections import Counter
from typing import Literal

from pydantic import Field

from src.ingestion_models import ContractModel
from src.llm_contracts import StructuredExtractionResult
from src.semantic_models import SemanticDocument
from src.verification_values import decimal_value


class RegexFieldComparison(ContractModel):
    field_path: str
    legacy_field: str
    outcome: Literal['MATCHING','DIFFERING','MISSING_GEMINI','MISSING_LEGACY','MISSING_BOTH','INCOMPARABLE']
    reason: str
    gemini_value: str | None = Field(default=None,repr=False,exclude=True)
    legacy_value: str | None = Field(default=None,repr=False,exclude=True)


class RegexDiagnosticReport(ContractModel):
    document_id: str | None
    authoritative: Literal[False] = False
    comparisons: tuple[RegexFieldComparison,...]
    counts: dict[str,int]
    warning: str = 'Agreement is not independent proof; disagreement cannot veto a source-supported semantic claim.'


def _field_at(claims,path):
    current=claims
    for name in path.split('.'):
        if '[' in name:
            key,index=name.rstrip(']').split('[')
            values=getattr(current,key)
            if int(index)>=len(values): return None
            current=values[int(index)]
        else:
            current=getattr(current,name,None)
        if current is None: return None
    return current


def _claim_value(field):
    if field is None or field.state=='MISSING' or not field.raw_values: return None
    if field.state in ('CONFLICTING','AMBIGUOUS','UNSUPPORTED'): return None
    return field.proposed_normalization or (field.raw_values[0] if len(set(field.raw_values))==1 else None)


def compare_with_legacy(ingested_document,structured_extraction,legacy_extraction) -> RegexDiagnosticReport:
    """Consumes an already-produced legacy result; never runs the legacy parser.

    Legacy records have no binary source hash. Same page text is a diagnostic
    alignment check only, never independent identity/evidence verification.
    """
    claims=structured_extraction.validated_claims if isinstance(structured_extraction,StructuredExtractionResult) else structured_extraction if isinstance(structured_extraction,SemanticDocument) else None
    mapping={'supplier.name':'supplier','quotation_number':'quote_number','quotation_date':'quote_date',
        'purchase_reference':'reference','amounts.total':'total','amounts.subtotal':'subtotal',
        'tax.treatment':'vat_treatment','tax.rate_percent':'vat_percentage','validity.days':'validity_days'}
    item_map={'code':'item_code','description':'item_description','quantity':'quantity','unit':'unit',
              'unit_price':'unit_price','currency':'currency','line_total':'line_total'}
    for key,value in item_map.items(): mapping['line_items[0].'+key]=value
    aligned=([p.text for p in ingested_document.pages]==[p.text for p in legacy_extraction.document.pages])
    rows=[]
    for path,name in mapping.items():
        gf=_field_at(claims,path) if claims else None
        lf=legacy_extraction.fields.get(name)
        left=_claim_value(gf);right=str(lf.normalized) if lf and lf.normalized is not None else None
        reason='DIRECT_VALUE_COMPARISON'
        if not aligned:
            outcome='INCOMPARABLE';reason='DIFFERENT_SOURCE_TEXT'
        elif path.startswith('line_items[') and claims and len(claims.line_items)>1:
            outcome='INCOMPARABLE';reason='LEGACY_SINGLE_ITEM_ALIGNMENT_UNSUPPORTED'
        elif (gf is None or gf.state=='MISSING') and lf is None: outcome='MISSING_BOTH'
        elif gf is None or gf.state=='MISSING': outcome='MISSING_GEMINI'
        elif lf is None: outcome='MISSING_LEGACY'
        elif left is None or right is None or lf.status!='found':
            outcome='INCOMPARABLE';reason='AMBIGUOUS_OR_UNSUPPORTED_VALUE'
        elif path.endswith(('unit_price','line_total')):
            gb=_claim_value(claims.tax.treatment);lb=legacy_extraction.fields.get('vat_treatment')
            basis=str(lb.normalized) if lb else None
            if gb is None or basis is None or gb!=basis:
                outcome='INCOMPARABLE';reason='VAT_BASIS_UNKNOWN_OR_DIFFERENT'
            else:
                a,b=decimal_value(left),decimal_value(right)
                outcome='MATCHING' if (a is not None and b is not None and a==b) or left==right else 'DIFFERING'
                if outcome=='MATCHING' and left!=right: reason='NORMALIZATION_EQUIVALENT'
        else:
            a,b=decimal_value(left),decimal_value(right)
            outcome='MATCHING' if left==right or (a is not None and b is not None and a==b) else 'DIFFERING'
            if outcome=='MATCHING' and left!=right: reason='NORMALIZATION_EQUIVALENT'
            if path=='tax.treatment' and outcome=='DIFFERING': reason='VAT_BASIS_DIFFERENCE'
        rows.append(RegexFieldComparison(field_path=path,legacy_field=name,outcome=outcome,reason=reason,
            gemini_value=left,legacy_value=right))
    return RegexDiagnosticReport(document_id=ingested_document.metadata.document_id,
        comparisons=tuple(rows),counts=dict(Counter(row.outcome for row in rows)))
