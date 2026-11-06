"""Diagnostic-only shallow supplier transport; lossless mapping to strict models."""
import json
from src.semantic_models import SemanticDocument,_semantic_fields
from src.gemini_isolation import full_schema,schema_analysis

COMPAT_VARIANT='GROUP_JSON_C'
MAPPING_VERSION='supplier-group-json-v1'
DIRECT_FIELDS=frozenset(('schema_version','document_id','ingestion_completeness','interpretation_method'))


def compatibility_schema():
    """All top-level fields remain required; no refs, unions or long descriptions."""
    return {'type':'object','properties':{name:{'type':'string'} for name in SemanticDocument.model_fields},
        'required':list(SemanticDocument.model_fields),'additionalProperties':False,
        'description':'Keep metadata as strings. Encode every other complete supplier group as JSON text. Do not summarize or omit fields.'}


def compatibility_context(document):
    from src.semantic_extraction import _claims_context
    context=json.loads(_claims_context(document))
    context['diagnostic_output_mapping']={
        'version':MAPPING_VERSION,
        'direct_metadata_fields':sorted(DIRECT_FIELDS),
        'json_encoded_group_fields':[name for name in SemanticDocument.model_fields if name not in DIRECT_FIELDS],
        'instructions':'Return every top-level field. Metadata values are unchanged strings. Other values are JSON strings '
            'encoding the ENTIRE original group, including all items, raw values, missing/ambiguous states, assumptions and evidence. '
            'Preserve inner null as JSON null. Missing extraction remains MISSING, never zero, false or clause absence. '
            'Do not invent normalized values or verifier authority.',
        'complete_inner_contract':full_schema()}
    return json.dumps(context,ensure_ascii=False,separators=(',',':'))


def encode_grouped(claims):
    """Offline mapping proof for schema-validated untrusted claims, not trusted facts."""
    if claims.interpretation_method!='FUTURE_PROVIDER': raise ValueError('Untrusted provider claims required.')
    for field in _semantic_fields(claims):
        if field.state=='VERIFIED' or field.normalized_value is not None or field.normalizer_version is not None:
            raise ValueError('Local authority cannot be encoded as provider claims.')
        if any(e.verification!='UNCHECKED' or e.verifier_id is not None for e in field.evidence):
            raise ValueError('Local authority cannot be encoded as provider claims.')
    data=claims.model_dump(mode='json')
    return {name:value if name in DIRECT_FIELDS else json.dumps(value,ensure_ascii=False,separators=(',',':'))
        for name,value in data.items()}


def parse_grouped(text,document):
    """Decode every field without projection; keep the original trust boundary."""
    from src.semantic_extraction import _unique_object,_structural_claim_check
    def parse(value):
        return json.loads(value,object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
    outer=parse(text)
    expected=set(SemanticDocument.model_fields)
    if not isinstance(outer,dict) or set(outer)!=expected or not all(isinstance(value,str) for value in outer.values()):
        raise ValueError('Every mapped supplier field must be present exactly once as a string.')
    data={name:value if name in DIRECT_FIELDS else parse(value) for name,value in outer.items()}
    claims=SemanticDocument.model_validate(data)
    _structural_claim_check(claims,document)
    return claims


def compatibility_analysis():
    """Deterministic schema/field preservation and description-size evidence."""
    from src.gemini_isolation import isolation_schema
    schemas={'full':full_schema(),'envelope':isolation_schema('REDUCED_C'),'grouped':compatibility_schema()}
    result={}
    for name,schema in schemas.items():
        descriptions=[]
        def visit(value):
            if isinstance(value,dict):
                if isinstance(value.get('description'),str): descriptions.append(len(value['description']))
                for child in value.values(): visit(child)
            elif isinstance(value,list):
                for child in value: visit(child)
        visit(schema)
        result[name]={**schema_analysis(schema),'description_count':len(descriptions),
            'description_total_characters':sum(descriptions),'longest_description_characters':max(descriptions,default=0)}
    return dict(mapping_version=MAPPING_VERSION,schemas=result,
        original_fields=list(SemanticDocument.model_fields),candidate_fields=list(compatibility_schema()['properties']),
        field_set_preserved=set(compatibility_schema()['properties'])==set(SemanticDocument.model_fields),
        nullability='Outer groups are encoded strings; inner JSON null values and original model types remain unchanged.',
        provider_acceptance='UNVERIFIED; offline JSON Schema checks are not a provider compiler.')
