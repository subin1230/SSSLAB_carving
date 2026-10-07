"""Inspect every PNG block whose strongest clue is an unanchored, crossing candidate."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import numpy as np
from audit_clues import describe


def candidate_details(data, candidate):
    pos = candidate['offset']
    length_bytes = data[pos:pos+4]
    context = data[max(0,pos-32):min(len(data),pos+64)]
    payload = data[pos+8:min(len(data),pos+8+max(0,candidate['declared_bytes']-12))]
    return dict(candidate, length=int.from_bytes(length_bytes,'big'),
                length_hex=length_bytes.hex(), length_ascii=repr(length_bytes),
                length_bytes_all_printable=all(32 <= x < 127 for x in length_bytes),
                context_printable_fraction=sum(x in (9,10,13) or 32 <= x < 127 for x in context)/len(context),
                context_repr=repr(context), payload_prefix_hex=payload[:16].hex(),
                available_payload_bytes=len(payload))


def analyze(run, output):
    run, output = Path(run), Path(output)
    s=json.loads((run/'summary.json').read_text())
    with np.load(s['inputs']['npz'],allow_pickle=False) as z:
        x,y=z['x'],z['y']
    if x.dtype != np.uint8 or x.shape != (s['total_rows'],s['block_size']) or y.shape != (len(x),):
        raise ValueError('NPZ shape/dtype mismatch')
    output.mkdir(parents=True,exist_ok=False)
    kinds, lengths, truths, signals=Counter(),Counter(),Counter(),Counter()
    blocks=0
    seen=set()
    previews=[]
    preview_groups=Counter()
    with (run/'evidence.jsonl').open() as f, (output/'candidates.jsonl').open('w') as out:
        for line in f:
            b=json.loads(line)
            i=b['row']
            if i in seen or not 0 <= i < len(x) or int(b['metadata']['label']) != int(y[i]):
                raise ValueError('row/label mismatch')
            seen.add(i)
            if b['validation_format'] != 'png':
                continue
            data=x[i].tobytes()
            category, cs=describe(data,'png',b['results'])
            if category != 'declared_extent_outside_block':
                continue
            blocks+=1
            truths[b['ground_truth_type']]+=1
            for c in cs:
                if c['category'] != 'declared_extent_outside_block':
                    continue
                detail=candidate_details(data,c)
                kinds[c['kind']]+=1
                lengths[f'{c["kind"]} | {detail["length"]}']+=1
                signals['printable_length_bytes' if detail['length_bytes_all_printable'] else 'nonprintable_length_bytes']+=1
                record=dict(block_id=b['block_id'],row=i,truth=b['ground_truth_type'],prediction=b['predicted_type'],**detail)
                out.write(json.dumps(record,ensure_ascii=False)+'\n')
                group=(c['kind'],detail['length_bytes_all_printable'])
                if preview_groups[group]<2 and len(previews)<12:
                    previews.append(record);preview_groups[group]+=1
    if len(seen)!=s['inspected_counts']['inspected']:
        raise ValueError('evidence count mismatch')
    result=dict(source_run=str(run.resolve()),selected_blocks=blocks,candidates=sum(kinds.values()),
                chunk_types=dict(kinds.most_common()),truth_blocks=dict(truths.most_common()),
                declared_lengths=dict(lengths.most_common()),text_signals=dict(signals),
                note='ASCII flags are inspection aids, not rejection rules. Unanchored candidate lengths may be false interpretations. All selected candidates are saved; printed examples are not representative.')
    (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:result[k] for k in ('selected_blocks','candidates','chunk_types','truth_blocks','text_signals')},ensure_ascii=False,indent=2))
    print('\n[선언 길이 상위 10개]')
    for label,n in lengths.most_common(10):
        print(f'{label}: {n}')
    print('\n[주변 바이트 예시 — 단서 확인용, 실제 청크 확정 아님]')
    for p in previews:
        print(f'{p["block_id"]} | truth={p["truth"]} | {p["kind"]} | offset={p["offset"]} | length={p["length"]} | length_bytes={p["length_ascii"]}')
        print(p['context_repr'])
    print(f'\n전체 후보 저장: {output}')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',required=True)
    p.add_argument('--output',default=str(Path(__file__).resolve().parent/'clue-output'/('png-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))))
    a=p.parse_args();analyze(a.run,a.output)
