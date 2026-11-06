"""Diagnostic-only schema/config isolation with a lossless supplier JSON envelope."""
from collections import Counter
from hashlib import sha256
import json
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from src.gemini_schema import gemini_response_schema
from src.semantic_models import SemanticDocument

VARIANTS=('TINY_C','FULL_MIN','REDUCED_C')
MAPPING_VERSION='supplier-json-envelope-v1'


class SupplierEnvelope(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    claims_json: StrictStr = Field(min_length=1)


def full_schema(): return gemini_response_schema(SemanticDocument.model_json_schema())


def isolation_schema(variant):
    if variant not in VARIANTS: raise ValueError('Select a supported isolation variant.')
    if variant=='TINY_C':
        return {'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
    if variant=='FULL_MIN': return full_schema()
    # The complete original contract stays in a description, not a recursively
    # constrained server grammar. Local parsing below validates the full object.
    description=('Encode the COMPLETE supplier extraction object as JSON text in claims_json. '
        'Preserve every supplied observation, item, obligation, exception and source quote/page. '
        'Do not summarize or project fields. The inner object must match this complete schema: '
        +json.dumps(full_schema(),separators=(',',':')))
    return {'type':'object','properties':{'claims_json':{'type':'string','description':description}},
        'required':['claims_json'],'additionalProperties':False}


def parse_supplier_reply(text,document,*,enveloped=False):
    """Unwrap only; no defaults, field projection, normalization or approval here."""
    from src.semantic_extraction import _unique_object,_structural_claim_check
    def parse(value):
        return json.loads(value,object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
    payload=parse(text)
    if enveloped:
        envelope=SupplierEnvelope.model_validate(payload)
        payload=parse(envelope.claims_json)
    claims=SemanticDocument.model_validate(payload)
    _structural_claim_check(claims,document)
    return claims


def schema_analysis(schema):
    """Structural inventory, NOT a prediction of the provider's grammar states."""
    definitions=schema.get('$defs',{});keywords=Counter();types=Counter()
    findings=dict(object_count=0,array_count=0,enum_count=0,enum_values=0,nullable_unions=0,
        required_properties=0,optional_properties=0,closed_objects=0,references=0,
        unresolved_references=[],undeclared_required_properties=[],cycles=[],expanded_nodes=0,expanded_max_depth=0)
    reviewed={'type','properties','required','additionalProperties','items','enum','anyOf','oneOf','allOf',
        '$ref','$defs','description','title','default','const','minimum','maximum','exclusiveMinimum',
        'exclusiveMaximum','minLength','maxLength','pattern','format','minItems','maxItems','prefixItems',
        'uniqueItems','contains','minContains','maxContains','minProperties','maxProperties'}
    def children(node):
        yield from node.get('properties',{}).values()
        for key in ('items','additionalProperties'):
            if isinstance(node.get(key),dict): yield node[key]
        for key in ('anyOf','oneOf','allOf','prefixItems'): yield from node.get(key,[])
    def inventory(node):
        if not isinstance(node,dict): return
        keywords.update(node.keys())
        kind=node.get('type')
        if isinstance(kind,str): types[kind]+=1
        if kind=='object':
            findings['object_count']+=1
            required=node.get('required',[]);properties=node.get('properties',{})
            findings['required_properties']+=len(required)
            findings['undeclared_required_properties'].extend(sorted(set(required)-set(properties)))
            findings['optional_properties']+=len(set(properties)-set(required))
            findings['closed_objects']+=node.get('additionalProperties') is False
        findings['array_count']+=kind=='array'
        if 'enum' in node:
            findings['enum_count']+=1;findings['enum_values']+=len(node['enum'])
        if any(child.get('type')=='null' for child in node.get('anyOf',[])): findings['nullable_unions']+=1
        if '$ref' in node:
            findings['references']+=1
            ref=node['$ref']
            if not ref.startswith('#/$defs/') or ref[8:] not in definitions: findings['unresolved_references'].append(ref)
        for child in children(node): inventory(child)
    inventory(schema)
    for definition in definitions.values(): inventory(definition)
    def expand(node,depth=1,stack=()):
        if not isinstance(node,dict): return
        findings['expanded_nodes']+=1;findings['expanded_max_depth']=max(findings['expanded_max_depth'],depth)
        if '$ref' in node:
            ref=node['$ref']
            if ref in stack:
                findings['cycles'].append(ref);return
            target=definitions.get(ref[8:]) if ref.startswith('#/$defs/') else None
            if target: expand(target,depth,(*stack,ref))
        for child in children(node): expand(child,depth+1,stack)
    expand(schema)
    data=json.dumps(schema,separators=(',',':')).encode()
    return dict(bytes=len(data),sha256=sha256(data).hexdigest(),definitions=len(definitions),
        keyword_counts=dict(sorted(keywords.items())),types=dict(sorted(types.items())),
        unreviewed_keywords=sorted(set(keywords)-reviewed),**findings)
