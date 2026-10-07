"""Explore byte features on train/val. Statistical evidence, never structural INVALID."""
import argparse
from collections import Counter
import csv
from datetime import datetime
import json
from pathlib import Path
import zipfile
import numpy as np

FEATURES = ['entropy','printable_fraction','zero_fraction','high_byte_fraction','equal_neighbor_fraction','mean_absolute_byte_delta']


def features(x):
    out=[]
    for row in x:
        hist=np.bincount(row,minlength=256)/len(row)
        p=hist[hist>0]
        out.append([-float((p*np.log2(p)).sum()),float(((row>=32)&(row<=126)).mean()),
                    float((row==0).mean()),float((row>=128).mean()),
                    float((row[1:]==row[:-1]).mean()),float(np.abs(np.diff(row.astype(np.int16))).mean())])
    return np.asarray(out,dtype=float)


def sample_split(root, split, per_class, seed):
    path=root/(split+'.npz')
    with np.load(path,allow_pickle=False) as z:
        y=z['y']
    if y.ndim!=1 or y.dtype.kind not in 'iu':
        raise ValueError('invalid labels')
    rng=np.random.default_rng(seed)
    chosen=np.sort(np.concatenate([rng.choice(np.flatnonzero(y==label),min(per_class,int((y==label).sum())),replace=False) for label in np.unique(y)]))
    names={}; sample_meta=[]; n=0
    with (root/(split+'_meta.csv')).open(newline='') as f:
        for i,m in enumerate(csv.DictReader(f)):
            if i>=len(y) or int(m['row'])!=i or int(m['label'])!=int(y[i]):
                raise ValueError(f'{split}: metadata/labels mismatch at {i}')
            label=int(y[i]); name=m['ground_truth_type'].lower()
            if label in names and names[label]!=name:
                raise ValueError('label-name conflict')
            names[label]=name
            if n<len(chosen) and i==chosen[n]:
                sample_meta.append(m);n+=1
        count=i+1 if 'i' in locals() else 0
    if count!=len(y) or n!=len(chosen):
        raise ValueError('metadata count mismatch')
    with zipfile.ZipFile(path) as archive, archive.open('x.npy') as f:
        version=np.lib.format.read_magic(f)
        if version==(1,0):
            shape,order,dtype=np.lib.format.read_array_header_1_0(f)
        elif version==(2,0):
            shape,order,dtype=np.lib.format.read_array_header_2_0(f)
        else:
            raise ValueError('unsupported NPY header')
        if order or dtype!=np.dtype('uint8') or shape!=(len(y),512):
            raise ValueError('requires C-order uint8 (N,512)')
        sampled=np.empty((len(chosen),512),dtype=np.uint8)
        for start in range(0,len(y),8192):
            end=min(start+8192,len(y)); needed=(end-start)*512
            raw=f.read(needed)
            if len(raw)!=needed:
                raise ValueError('truncated x.npy')
            lo,hi=np.searchsorted(chosen,[start,end])
            if hi>lo:
                sampled[lo:hi]=np.frombuffer(raw,dtype=np.uint8).reshape(-1,512)[chosen[lo:hi]-start]
    print(f'{split}: {len(y):,}개 중 {len(chosen):,}개 표본 추출',flush=True)
    return features(sampled),np.array([names[int(v)] for v in y[chosen]]),sample_meta


def evaluate(train, train_types, val, val_types):
    result={}
    for fmt in ('png','zip'):
        target=train[train_types==fmt]
        if len(target)<20 or not np.any(val_types==fmt):
            raise ValueError(f'{fmt}: insufficient samples')
        # Single-feature ranges only: no tuning on val and no feature conjunction.
        entries=[]
        for j,name in enumerate(FEATURES):
            low,high=np.quantile(target[:,j],[.005,.995])
            retain=(val[:,j]>=low)&(val[:,j]<=high)
            by_type={str(t):{'n':int((val_types==t).sum()),'outside':int((~retain & (val_types==t)).sum())} for t in np.unique(val_types)}
            own=val_types==fmt
            entries.append(dict(feature=name,train_lower=float(low),train_upper=float(high),
                val_target_n=int(own.sum()),val_target_outside=int((~retain & own).sum()),
                val_other_n=int((~own).sum()),val_other_outside=int((~retain & ~own).sum()),by_type=by_type))
        result[fmt]=entries
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',required=True,type=Path)
    p.add_argument('--per-class',type=int,default=256)
    p.add_argument('--seed',type=int,default=20261007)
    p.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'clue-output'/('features-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    a=p.parse_args()
    if a.per_class<20:p.error('--per-class must be >=20')
    train,tt,tm=sample_split(a.data_dir,'train',a.per_class,a.seed)
    val,vt,vm=sample_split(a.data_dir,'val',a.per_class,a.seed+1)
    result=evaluate(train,tt,val,vt)
    a.output.mkdir(parents=True,exist_ok=False)
    overlap=set(m['source_file_id'] for m in tm)&set(m['source_file_id'] for m in vm)
    for split,xx,types,meta in [('train',train,tt,tm),('val',val,vt,vm)]:
        with (a.output/(split+'_features.csv')).open('w',newline='') as f:
            w=csv.writer(f);w.writerow(['row','block_id','source_file_id','truth']+FEATURES)
            for feature,t,m in zip(xx,types,meta):w.writerow([m['row'],m['block_id'],m['source_file_id'],t]+feature.tolist())
    report=dict(inputs=str(a.data_dir.resolve()),seed=a.seed,per_class=a.per_class,
        sampled_source_file_overlap=len(overlap),results=result,
        note='Exploratory single-feature train quantile ranges. Outside is statistical atypicality, NOT structural INVALID. Other-label blocks can embed PNG/ZIP. Not FFC-conditioned rejection rates. Test not used. Sampled file overlap only; no whole-split independence claim.')
    (a.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('\n포맷 | 특징 | val 해당 포맷 범위 밖 | val 다른 라벨 범위 밖')
    for fmt,entries in result.items():
        for r in entries:
            print(f'{fmt} | {r["feature"]} | {r["val_target_outside"]}/{r["val_target_n"]} | {r["val_other_outside"]}/{r["val_other_n"]}')
    print(f'표본 내 train/val 공통 원본 파일: {len(overlap)}')
    print('범위 밖 = 통계적 비전형성. 구조 위반·FFC 오분류 제거 성공으로 해석하지 않음.')
    print(f'저장: {a.output}')


if __name__=='__main__':main()
