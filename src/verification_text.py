"""Conservative quote matching with original offsets, no fuzzy digit substitution."""
import re

PUNCTUATION=str.maketrans({'\u2018':"'",'\u2019':"'",'\u201c':'"','\u201d':'"',
    '\u2010':'-','\u2011':'-','\u2012':'-','\u2013':'-','\u2014':'-','\u2212':'-'})


def canonical_with_offsets(text):
    chars=[];starts=[];ends=[]
    for index,char in enumerate(text):
        value=' ' if char.isspace() else char.translate(PUNCTUATION)
        if value==' ' and chars and chars[-1]==' ':
            ends[-1]=index+1
            continue
        chars.append(value);starts.append(index);ends.append(index+1)
    return ''.join(chars),starts,ends


def canonical(text):
    return canonical_with_offsets(text)[0].strip()


def quote_spans(source,quote,maximum):
    target=canonical(quote)
    if not target:
        return []
    normalized,starts,ends=canonical_with_offsets(source)
    matches=[];offset=0
    while len(matches)<=maximum:
        index=normalized.find(target,offset)
        if index<0:
            break
        start,end=starts[index],ends[index+len(target)-1]
        matches.append((start,end,'EXACT' if source[start:end]==quote else 'WHITESPACE_PUNCTUATION'))
        offset=index+1
    return matches


def enclosing_context(text,start,end):
    left=text.rfind('\n',0,start)+1
    right=text.find('\n',end)
    right=len(text) if right<0 else right
    previous=text[:max(0,left-1)].splitlines()
    if previous and previous[-1].rstrip().endswith(':'):
        left=max(0,left-1-len(previous[-1]))
    return canonical(text[left:right])


def contains_value(text,value):
    needle=canonical(value)
    if not needle:
        return False
    left=r'(?<![\w.])' if needle[0].isdigit() else r'(?<!\w)'
    right=r'(?!\w|\.\d)' if needle[-1].isdigit() else r'(?!\w)'
    return bool(re.search(left+re.escape(needle)+right,canonical(text)))
