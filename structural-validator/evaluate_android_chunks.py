"""Exploratory APK hints from CRC-checked Android PNG chunks in an all-formats run."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path
import zlib
import numpy as np

KINDS = (b'npTc', b'npOl', b'npLb')


def detect(data):
    found = []
    for kind in KINDS:
        start = 0
        while True:
            at = data.find(kind, start)
            if at < 0:
                break
            start = at+1
            if at < 4:
                continue
            size = int.from_bytes(data[at-4:at], 'big')
            end = at+4+size
            if end+4 > len(data):
                continue
            payload = data[at+4:end]
            if (zlib.crc32(kind+payload)&0xffffffff) != int.from_bytes(data[end:end+4], 'big'):
                continue
            layout = size == (24 if kind == b'npOl' else 16) if kind != b'npTc' else size >= 32 and size == 32+4*sum(payload[1:4])
            found.append(dict(kind=kind.decode(), offset=at-4, length=size, crc_matches=True,
                              layout_matches=layout, counts=list(payload[1:4]) if kind == b'npTc' else None))
    return found


def evaluate(run, output):
    run, output = Path(run), Path(output)
    summary = json.loads((run/'summary.json').read_text(encoding='utf-8'))
    if summary['selection_mode'] != 'all-formats' or summary['limit'] != 0:
        raise ValueError('complete all-formats run required')
    with np.load(summary['inputs']['npz'], allow_pickle=False) as z:
        x, y = z['x'], z['y']
    if x.dtype != np.uint8 or x.shape != (summary['total_rows'],summary['block_size']) or y.shape != (len(x),):
        raise ValueError('NPZ mismatch')
    output.mkdir(parents=True, exist_ok=False)
    groups = defaultdict(Counter)
    sources = defaultdict(set)
    counts = Counter()
    with (run/'blocks.csv').open(encoding='utf-8-sig',newline='') as blocks, open(summary['inputs']['metadata'],encoding='utf-8-sig',newline='') as metas, (output/'hits.jsonl').open('w',encoding='utf-8') as hits:
        from itertools import zip_longest
        for i, (b,m) in enumerate(zip_longest(csv.DictReader(blocks),csv.DictReader(metas))):
            if b is None or m is None or i >= len(x) or int(b['row']) != i or int(m['row']) != i or b['block_id'] != m['block_id'] or b['ground_truth_type'] != m['ground_truth_type'] or int(m['label']) != int(y[i]):
                raise ValueError('input alignment mismatch')
            # All blocks scanned; labels and FFC predictions are only read afterwards.
            chunks = detect(x[i].tobytes())
            truth, pred = b['ground_truth_type'].lower(), b['predicted_type'].lower()
            groups[truth]['total'] += 1
            groups[truth]['ffc_wrong'] += pred != truth
            counts['inspected'] += 1
            if not chunks:
                continue
            qualified = any(c['layout_matches'] for c in chunks)
            groups[truth]['crc_hit_blocks'] += 1
            groups[truth]['layout_hit_blocks'] += qualified
            if qualified:
                sources[truth].add(m['source_file_id'])
                counts['apk_candidate_blocks'] += 1
                counts['candidate_truth_apk'] += truth == 'apk'
                counts['ffc_wrong_apk_recovered_if_overridden'] += truth == 'apk' and pred != 'apk'
                counts['ffc_correct_non_apk_harmed_if_overridden'] += truth != 'apk' and pred == truth
                counts['accuracy_delta_if_overridden'] += int(truth == 'apk' and pred != truth)-int(truth != 'apk' and pred == truth)
            hits.write(json.dumps(dict(row=i,block_id=b['block_id'],truth=truth,prediction=pred,
                                      source_file_id=m['source_file_id'],apk_candidate=qualified,chunks=chunks),ensure_ascii=False)+'\n')
    if counts['inspected'] != len(x):
        raise ValueError('row count mismatch')
    result = dict(counts=dict(counts),per_truth_label=dict(groups),
                  unique_source_files={k:len(v) for k,v in sources.items()},
                  candidate_precision=counts['candidate_truth_apk']/counts['apk_candidate_blocks'] if counts['apk_candidate_blocks'] else None,
                  apk_coverage=groups['apk']['layout_hit_blocks']/groups['apk']['total'] if groups['apk']['total'] else None,
                  reclassification_applied=False,
                  note='Exploratory val-selected rule. Android resource hint, not proof of APK origin. Layout checks only fixed sizes and npTc count/size relation; full Android semantics not validated. Override metrics are hypothetical.')
    (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f'저장: {output}')
    return result


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',required=True)
    p.add_argument('--output',default=str(Path(__file__).resolve().parent/'android-output'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    a=p.parse_args();evaluate(a.run,a.output)
