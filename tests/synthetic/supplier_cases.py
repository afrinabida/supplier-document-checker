"""Independent fictional documents authored from the contract, not evaluation PDFs.

Values are authored inputs; Gemini replies are hand-built mocks, never legacy
extraction. An explicit synthetic PR tax basis supports the positive control.
"""
from dataclasses import dataclass
from decimal import Decimal
import json
import pymupdf
import httpx
from google import genai

from src.pdf_ingestion import ingest_pdf
from src.models import PurchaseRequestItem
from src.semantic_models import (SemanticDocument,SemanticField,SemanticEvidence,
    SupplierIdentity,RegistrationIdentifier,SemanticLineItem,DeliveryTerms,
    PaymentObligation,TaxTreatment,DocumentAmounts,PenaltyClause,ValidityTerms)
from src.ingestion_models import SourceReference
from src.gemini_minimal_contract import encode_minimal
from src.gemini_provider import GeminiProvider


@dataclass
class Case:
    pdf: bytes
    document: object
    claims: SemanticDocument
    purchase_request: list

    def response(self):
        return encode_minimal(self.claims)


def supplier_case(*,layout='ledger',multiple=False,price='7.25',quantity='8',
                  payment_days='42',vat='15',advance='5',delivery='18',
                  tax_basis='separate',supplier='Lantern Orchard Supplies',
                  explicit_no_penalty=False,conflicting_supplier=False):
    rows=[('DEV-ORB-72','Ceramic inspection markers',quantity,price,'8','7.25')]
    if multiple: rows.append(('DEV-VEL-39','Reusable sample sleeves','3','4.40','3','4.40'))
    amounts=[Decimal(q)*Decimal(p) for _,_,q,p,_,_ in rows]
    subtotal=sum(amounts,Decimal(0));tax=subtotal*Decimal(vat)/100
    values={
        'supplier':f'Supplier legal name: {supplier}',
        'tin':'Supplier TIN: 9765423108',
        'bin':'Supplier BIN: 8642097531',
        'number':'Quote number: DEV-OFFER-734',
        'date':'Quotation date: 2032-04-17',
        'reference':'PR reference: DEV-REQ-314',
        'delivery':f'Delivery {delivery} calendar days from PO.',
        'payment':f'Payment {payment_days} calendar days after delivery; {advance}% advance.',
        'tax':f'VAT excluded; VAT {vat}%; Tax amount: {tax} BDT',
        'penalty':('No late delivery penalty applies.' if explicit_no_penalty else
                   'Late delivery liquidated damages 0.8% of order value each week.'),
        'validity':'Validity 38 calendar days.',
        'subtotal':f'Subtotal: {subtotal} BDT',
        'total':f'Grand total: {subtotal+tax} BDT',
    }
    for i,(code,description,q,p,_,_) in enumerate(rows):
        cells=[f'Item code: {code}',f'Description: {description}',f'Quantity: {q}',
            'Unit: pieces',f'Unit cost: {p} BDT','Currency: BDT',f'Line amount: {amounts[i]} BDT']
        if layout=='reordered': cells=[cells[j] for j in (4,0,6,2,1,5,3)]
        values[f'row{i}']=(' | ' if layout=='reordered' else '; ').join(cells)
    order=['supplier','tin','bin','number','date','reference',
        *[f'row{i}' for i in range(len(rows))],'delivery','payment','tax','penalty','validity','subtotal','total']
    if conflicting_supplier:
        values['alternate_supplier']='Supplier legal name: Riverglass Workshop'
        order.append('alternate_supplier')
    if layout=='reordered': order=order[6:]+order[:6]
    if layout=='multipage': groups=[order[:6],order[6:]]
    elif layout in ('ledger','reordered'): groups=[order]
    else: raise ValueError('Unknown authored synthetic layout')
    with pymupdf.open() as pdf:
        page_numbers={}
        for page_number,keys in enumerate(groups,1):
            page=pdf.new_page(width=1400,height=1000)
            for j,key in enumerate(keys):
                page.insert_text((24,32+24*j),values[key],fontsize=9)
                page_numbers[key]=page_number
        data=pdf.tobytes()
    document=ingest_pdf(data,filename='independent-development.pdf')
    def c(value,key,hint=None):
        page=page_numbers[key];quote=values[key]
        assert quote in document.pages[page-1].text
        return SemanticField(state='UNVERIFIED',raw_values=(str(value),),
            evidence=(SemanticEvidence(source=SourceReference(document_id=document.metadata.document_id,page_number=page),quote=quote),),
            proposed_normalization=hint)
    items=tuple(SemanticLineItem(item_id=f'development-row-{i}',code=c(code,f'row{i}'),
        description=c(description,f'row{i}'),quantity=c(q,f'row{i}'),unit=c('pieces',f'row{i}'),
        unit_price=c(p,f'row{i}'),currency=c('BDT',f'row{i}'),line_total=c(amounts[i],f'row{i}'))
        for i,(code,description,q,p,_,_) in enumerate(rows))
    penalty=PenaltyClause(clause_id='development-delay',wording=c(values['penalty'],'penalty'),
        polarity=c(values['penalty'],'penalty','negated' if explicit_no_penalty else 'operative'),
        **({} if explicit_no_penalty else dict(rate_percent=c('0.8','penalty'),basis=c('order value','penalty'),period=c('week','penalty'))))
    claims=SemanticDocument(document_id=document.metadata.document_id,
        ingestion_completeness=document.completeness,interpretation_method='FUTURE_PROVIDER',
        supplier=SupplierIdentity(name=c(supplier,'supplier'),registrations=(
            RegistrationIdentifier(kind='TIN',value=c('9765423108','tin')),
            RegistrationIdentifier(kind='BIN',value=c('8642097531','bin')))),
        quotation_number=c('DEV-OFFER-734','number'),quotation_date=c('2032-04-17','date'),
        purchase_reference=c('DEV-REQ-314','reference'),line_items=items,
        delivery=DeliveryTerms(wording=c(values['delivery'],'delivery'),duration=c(delivery,'delivery'),anchor=c('PO','delivery')),
        payments=(PaymentObligation(obligation_id='development-payment',wording=c(values['payment'],'payment'),
            kind=c('advance','payment'),percent=c(advance,'payment'),
            timing=DeliveryTerms(duration=c(payment_days,'payment'),anchor=c('delivery','payment'))),),
        tax=TaxTreatment(treatment=c('VAT excluded','tax'),rate_percent=c(vat,'tax'),amount=c(tax,'tax')),
        amounts=DocumentAmounts(subtotal=c(subtotal,'subtotal'),total=c(subtotal+tax,'total')),
        penalties=(penalty,),validity=ValidityTerms(days=c('38','validity')))
    if conflicting_supplier:
        first=claims.supplier.name;second=c('Riverglass Workshop','alternate_supplier')
        name=first.model_copy(update={'raw_values':(*first.raw_values,*second.raw_values),
            'evidence':(*first.evidence,*second.evidence)})
        claims=claims.model_copy(update={'supplier':claims.supplier.model_copy(update={'name':name})})
    pr=[PurchaseRequestItem(pr_number='DEV-REQ-314',item_code=code,description=description,
        unit='pieces',quantity=qmax,maximum_unit_price=pmax,price_tax_basis=tax_basis)
        for code,description,q,p,qmax,pmax in rows]
    return Case(data,document,claims,pr)


def mocked_gemini(payload=None,*,status=200):
    calls=[]
    def handler(request):
        calls.append(json.loads(request.content))
        if status!=200:
            return httpx.Response(status,json={'error':{'code':status,'status':'DEADLINE_EXCEEDED',
                'message':'Synthetic provider deadline exceeded.'}})
        text=payload if isinstance(payload,str) else json.dumps(payload)
        return httpx.Response(200,json={'candidates':[{'content':{'role':'model','parts':[{'text':text}]},'finishReason':'STOP'}]})
    def factory(**kwargs):
        assert kwargs['http_options'].retry_options.attempts==1
        kwargs['http_options']=kwargs['http_options'].model_copy(update={
            'httpx_client':httpx.Client(transport=httpx.MockTransport(handler))})
        return genai.Client(**kwargs)
    return GeminiProvider(client_factory=factory),calls
