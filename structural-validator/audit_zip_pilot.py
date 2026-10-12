"""Oracle coverage audit only: source offsets/IDs MUST NOT be used as prediction edges."""
import argparse
from collections import Counter,defaultdict
import csv,json,struct
from pathlib import Path
import numpy as np


def references(b):
    for p in range(len(b)-3):
        sig=b[p:p+4]
        if sig==b'PK\x03\x04':
            if p+30>len(b):yield ('local',p,None,'partial_header');continue
            flags=int.from_bytes(b[p+6:p+8],'little')
            cs=int.from_bytes(b[p+18:p+22],'little')
            nl,xl=struct.unpack_from('<HH',b,p+26)
            if flags & (1|8|64|8192) or cs==0xffffffff:
                yield ('zip_next',p,None,'unsupported');continue
            yield ('zip_next',p,p+30+nl+xl+cs,'relative')
        elif sig==b'PK\x01\x02':
            if p+46>len(b):yield ('cd_local',p,None,'partial_header');continue
            disk=int.from_bytes(b[p+34:p+36],'little')
            off=int.from_bytes(b[p+42:p+46],'little')
            yield ('cd_local',p,off,'unsupported' if disk or off==0xffffffff else 'archive_relative')
        elif sig==b'PK\x05\x06':
            if p+22>len(b):yield ('eocd',p,None,'partial_header');continue
            disk,sd,here,total,size,off=struct.unpack_from('<4H2I',b,p+4)
            yield ('eocd',p,off,'unsupported' if disk or sd or here!=total or total in (0,65535) or 0xffffffff in (size,off) else 'archive_relative')


def audit(root):
    root=Path(root);x=np.load(root/'blocks.npy',mmap_mode='r',allow_pickle=False)
    with (root/'truth.csv').open() as f:meta=list(csv.DictReader(f))
    if x.dtype!=np.uint8 or x.shape!=(len(meta),512):raise ValueError('shape mismatch')
    lookup={};counts=Counter();byfmt=defaultdict(Counter); details=[]
    for i,m in enumerate(meta):
        if int(m['sample_id'])!=i:raise ValueError('sample order mismatch')
        key=(m['source_file_id'],int(m['source_byte_offset']))
        if key in lookup:raise ValueError('duplicate source position')
        lookup[key]=i
    for i,m in enumerate(meta):
        for rule,pos,target,mode in references(x[i].tobytes()):
            status=mode;tid=None
            if mode in ('relative','archive_relative'):
                # archive_relative assumes ZIP archive base equals original file offset 0.
                address=target+int(m['source_byte_offset']) if mode=='relative' else target
                tid=lookup.get((m['source_file_id'],address//512*512))
                if tid is None:status='target_block_absent'
                else:
                    within=address%512
                    if within+4>512:status='target_signature_crosses_block'
                    else:
                        actual=x[tid,within:within+4].tobytes()
                        expected={'zip_next':(b'PK\x03\x04',b'PK\x01\x02'), 'cd_local':(b'PK\x03\x04',),'eocd':(b'PK\x01\x02',)}[rule]
                        status='target_signature_matches' if actual in expected else 'target_signature_mismatch'
            counts[f'{rule} | {status}']+=1;byfmt[m['format']][f'{rule} | {status}']+=1
            details.append(dict(sample_id=i,rule=rule,offset=pos,status=status,target_sample_id=tid,
                                distinct_block=tid is not None and tid!=i))
    result=dict(blocks=len(x),counts=dict(counts),by_format=dict(byfmt),
        matching_cross_block_relations=sum(d['status']=='target_signature_matches' and d['distinct_block'] for d in details),
        note='Oracle diagnostic uses true file ID and offsets. Not predicted edges, not precision, not grouping input. CD/EOCD assume archive base at source offset zero; embedded/SFX bases can invalidate that assumption. Matches only check target signature, not full record validity.')
    out=root/'zip-coverage.json'
    if out.exists():raise FileExistsError(out)
    out.write_text(json.dumps(dict(result,relations=details),ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2));print('저장:',out)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pilot',required=True)
    audit(p.parse_args().pilot)
