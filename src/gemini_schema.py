"""Compact untrusted-claims wire schema; local Pydantic validation stays strict.

The provider must not emit local normalization/verifier authority. Those optional
fields are omitted from the wire contract and keep their original local defaults.
"""
from copy import deepcopy
import json

WIRE_SCHEMA_VERSION='gemini-claims-wire-v1'


def gemini_response_schema(schema):
    result=deepcopy(schema)
    def compact(node):
        if isinstance(node,list):
            for value in node: compact(value)
        elif isinstance(node,dict):
            node.pop('title',None);node.pop('default',None)
            if 'const' in node: node['enum']=[node.pop('const')]
            properties=node.get('properties')
            if isinstance(properties,dict):
                if {'state','raw_values','evidence'}<=properties.keys():
                    for name in ('normalized_value','normalizer_version'): properties.pop(name,None)
                    states=properties['state'].get('enum',[])
                    if 'VERIFIED' in states: states.remove('VERIFIED')
                if {'source','quote','verification'}<=properties.keys():
                    for name in ('verification','verifier_id'): properties.pop(name,None)
                if 'interpretation_method' in properties:
                    properties['interpretation_method']={'type':'string','enum':['FUTURE_PROVIDER']}
                if 'required' in node:
                    node['required']=[name for name in node['required'] if name in properties]
            for value in node.values(): compact(value)
    compact(result)
    definitions=result.pop('$defs',{})
    mapping={};unique={};signatures={}
    for name,value in definitions.items():
        signature=json.dumps(value,sort_keys=True,separators=(',',':'))
        short=signatures.get(signature)
        if short is None:
            short=f'D{len(unique)}';signatures[signature]=short;unique[short]=value
        mapping['#/$defs/'+name]='#/$defs/'+short
    def references(value):
        if isinstance(value,dict):
            if '$ref' in value: value['$ref']=mapping.get(value['$ref'],value['$ref'])
            for entry in value.values(): references(entry)
        elif isinstance(value,list):
            for entry in value: references(entry)
    if unique: result['$defs']=unique
    references(result)
    return result
