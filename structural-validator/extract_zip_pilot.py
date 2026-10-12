"""Extract a small, explicitly nonrepresentative consecutive-block pilot."""
import argparse
import csv
import json
from pathlib import Path
from datetime import datetime
import numpy as np

TARGETS={'jar':32,'apk':5,'docx':21,'pptx':65,'xlsx':83}


def verify_group(g,codes,indices,types,representations,kind):
    if len(g)!=32 or np.any(g<0) or np.any(g>=len(codes)):
        raise ValueError('group indices outside range')
    if len(np.unique(g))!=32 or not np.all(codes[g]==codes[g[0]]):
        raise ValueError('group spans files or duplicates blocks')
    if not np.all(np.diff(indices[g])==1) or not np.all(types[g]==kind):
        raise ValueError('nonconsecutive group or mixed type')
    if not np.all(representations[g]==representations[g[0]]):
        raise ValueError('mixed representations')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--cache',type=Path,required=True)
    p.add_argument('--files-per-format',type=int,default=10)
    p.add_argument('--seed',type=int,default=20261012)
    p.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'pair-pilot-output'/datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    a=p.parse_args()
    if a.files_per_format<1:p.error('files-per-format must be positive')
    data=a.root/'data'
    def read(path):return np.load(path,mmap_mode='r',allow_pickle=False)
    groups=read(a.root/'splits/val_16k_groups.npy')
    codes=read(a.cache/'file_code.npy'); table=read(a.cache/'uuid_table.npy')
    indices=read(data/'index.npy');types=read(data/'filetype_id.npy');reps=read(data/'data_representation.npy')
    if groups.ndim!=2 or groups.shape[1]!=32 or groups.dtype.kind not in 'iu':raise ValueError('invalid groups')
    if not len(codes)==len(indices)==len(types)==len(reps):raise ValueError('metadata lengths differ')
    selected=[]; seen={k:set() for k in TARGETS}; reverse={v:k for k,v in TARGETS.items()}
    for start in range(0,len(groups),8192):
        part=groups[start:start+8192]; first=part[:,0]
        if np.any(first<0) or np.any(first>=len(types)):raise ValueError('invalid first index')
        for local in np.flatnonzero(np.isin(types[first],list(reverse))):
            g=np.asarray(part[local]);kind=int(types[g[0]]);name=reverse[kind];code=int(codes[g[0]])
            if len(seen[name])>=a.files_per_format or code in seen[name]:continue
            verify_group(g,codes,indices,types,reps,kind)
            if not 0<=code<len(table):raise ValueError('invalid file code')
            selected.append((start+int(local),name,code,g.copy()))
            seen[name].add(code)
        if all(len(s)>=a.files_per_format for s in seen.values()):break
    if not selected:raise ValueError('no target groups found')
    manifest=json.loads((data/'shard_manifest.json').read_text())
    entry=next(e for e in manifest if e['filename']=='block.npy')
    shards=[read(data/f'block_shard_{i:05d}.npy') for i in range(entry['n_shards'])]
    if any(s.ndim!=2 or s.shape[1]!=512 or s.dtype!=np.uint8 for s in shards):raise ValueError('invalid shards')
    offsets=np.concatenate(([0],np.cumsum([len(s) for s in shards])))
    if int(offsets[-1])!=len(codes) or list(entry['shape'])!=[len(codes),512]:raise ValueError('manifest/metadata mismatch')
    blocks=[];truth=[]
    for group_row,name,code,g in selected:
        for gid in g:
            sid=int(np.searchsorted(offsets,gid,side='right')-1)
            blocks.append(np.array(shards[sid][int(gid-offsets[sid])],copy=True))
            truth.append(dict(source_file_id=bytes(table[code]).decode('ascii'),format=name,
                source_block_index=int(indices[gid]),source_byte_offset=int(indices[gid])*512,
                global_block_id=int(gid),group_row=group_row,data_representation=int(reps[gid])))
    order=np.random.default_rng(a.seed).permutation(len(blocks))
    a.output.mkdir(parents=True,exist_ok=False)
    np.save(a.output/'blocks.npy',np.asarray(blocks,dtype=np.uint8)[order])
    with (a.output/'truth.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['sample_id']+list(truth[0]));w.writeheader()
        for sample_id,i in enumerate(order):w.writerow(dict(sample_id=sample_id,**truth[int(i)]))
    report=dict(root=str(a.root),cache=str(a.cache),split='val',group_size=32,block_size=512,seed=a.seed,
        files_per_format={k:len(v) for k,v in seen.items()},blocks=len(blocks),
        selection='first eligible group of first distinct files per format; convenience pilot, NOT random representative sample',
        note='Not whole files. Metadata used for construction/evaluation only. Do not expose truth.csv or source order to grouping. ZIP target coverage and cache UUID provenance require separate checks. Representation flags preserved without interpretation.')
    (a.output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2));print('저장:',a.output)


if __name__=='__main__':main()
