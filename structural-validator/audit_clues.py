"""Audit single-block clue coverage from an existing v3/v4 run; no rejection."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import struct
import numpy as np

SUPPORTED = {'png_signature_chain', 'after_crc_chunk', 'crc_consistent_chunk', 'zip_directory_reference'}


def describe(data, fmt, results):
    candidates = []
    for pos in sorted({r['offset'] for r in results if r.get('offset') is not None}):
        if not 0 <= pos < len(data):
            raise ValueError('candidate offset outside block')
        part = data[pos:]
        rs = [r for r in results if r.get('offset') == pos]
        fixed = 8 if fmt == 'png' else {b'PK\x03\x04':30, b'PK\x01\x02':46, b'PK\x05\x06':22}.get(part[:4], 4)
        extent = None
        name = part[4:8].decode('ascii', 'replace') if fmt == 'png' else part[:4].hex()
        stage = 'fixed_fields_incomplete'
        if len(part) >= fixed:
            if fmt == 'png':
                extent = int.from_bytes(part[:4], 'big') + 12
            elif part[:4] == b'PK\x03\x04':
                nl, xl = struct.unpack_from('<HH', part, 26)
                extent = 30 + nl + xl
            elif part[:4] == b'PK\x01\x02':
                nl, xl, cl = struct.unpack_from('<HHH', part, 28)
                extent = 46 + nl + xl + cl
            elif part[:4] == b'PK\x05\x06':
                extent = 22 + int.from_bytes(part[20:22], 'little')
            stage = 'declared_structure_complete' if extent is not None and extent <= len(part) else 'declared_extent_outside_block'
        supported = any(r.get('boundary_evidence') in SUPPORTED for r in rs)
        if supported:
            category = 'boundary_supported'
        elif stage == 'declared_structure_complete':
            category = 'complete_but_unanchored'
        else:
            category = stage
        candidates.append(dict(offset=pos, kind=name, category=category, extent_stage=stage,
                               declared_bytes=extent, available_bytes=len(part),
                               boundary_evidence=sorted({r.get('boundary_evidence','missing') for r in rs}),
                               rules=rs, context_hex=data[max(0,pos-16):min(len(data),pos+48)].hex(),
                               context_ascii=''.join(chr(v) if 32<=v<127 else '.' for v in data[max(0,pos-16):min(len(data),pos+48)])))
    priority = ['boundary_supported','complete_but_unanchored','declared_extent_outside_block','fixed_fields_incomplete']
    category = next((c for c in priority if any(x['category']==c for x in candidates)), 'no_recognized_candidate')
    return category, candidates


def audit(run, output, examples=3):
    if examples < 0:
        raise ValueError('examples must be nonnegative')
    run, output = Path(run), Path(output)
    summary = json.loads((run/'summary.json').read_text())
    if summary['validator_version'] not in ('v3','v4'):
        raise ValueError('v3/v4 evidence required')
    with np.load(summary['inputs']['npz'], allow_pickle=False) as z:
        x, y = z['x'], z['y']
    if x.dtype != np.uint8 or x.shape != (summary['total_rows'],summary['block_size']) or y.shape != (len(x),):
        raise ValueError('NPZ shape/dtype mismatch')
    output.mkdir(parents=True, exist_ok=False)
    counts, candidate_counts, strata = Counter(), Counter(), defaultdict(Counter)
    chosen, seen = Counter(), set()
    with (run/'evidence.jsonl').open() as source, (output/'examples.jsonl').open('w') as samples:
        for line in source:
            b = json.loads(line)
            i = b['row']
            if i in seen or not 0 <= i < len(x) or int(b['metadata']['label']) != int(y[i]):
                raise ValueError('duplicate row or metadata/NPZ mismatch')
            seen.add(i)
            fmt = b['validation_format']
            if fmt not in ('png','zip'):
                raise ValueError('unsupported validation format')
            category, candidates = describe(x[i].tobytes(), fmt, b['results'])
            counts[f'{fmt} | {category}'] += 1
            group = f'{fmt} | truth={b["ground_truth_type"]} | prediction={b["predicted_type"]}'
            strata[group][category] += 1
            for c in candidates:
                candidate_counts[f'{fmt} | {c["extent_stage"]}'] += 1
            key = (fmt,category,b['ground_truth_type'])
            if chosen[key] < examples:
                chosen[key] += 1
                samples.write(json.dumps(dict(row=i,block_id=b['block_id'],format=fmt,
                    truth=b['ground_truth_type'],prediction=b['predicted_type'], category=category,
                    candidates=candidates, block_preview_hex=x[i,:64].tobytes().hex()),ensure_ascii=False)+'\n')
    if len(seen) != summary['inspected_counts']['inspected']:
        raise ValueError('evidence row count mismatch')
    result = dict(source_run=str(run.resolve()),purpose='exploratory clue coverage; not held-out performance',
                  inspected=len(seen),block_categories=dict(counts),candidate_extents=dict(candidate_counts),
                  strata=dict(strata),examples_selection='first N per format/category/truth; not random',
                  note='Declared extent is a candidate interpretation, not proof of real structure or missing bytes. No recognized candidate does not mean no structure exists. Boundary support is conditional. Labels are used only for stratification.')
    (output/'clue_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    titles={'no_recognized_candidate':'현재 탐색기로 구조 후보를 찾지 못함','fixed_fields_incomplete':'후보 고정 필드 부족',
            'declared_extent_outside_block':'후보의 선언 길이가 블록 밖까지 요구함','complete_but_unanchored':'선언 범위는 확보됐지만 경계 근거 부족',
            'boundary_supported':'블록 내부 경계 근거 있음(검사 완료·위반 의미 아님)'}
    lines=['# 단일 블록 단서 조사','',f'검사 블록: {len(seen):,}', '', '| 포맷 | 범주 | 블록 수 | 포맷 내 비율 |','|---|---|---:|---:|']
    for fmt in ('png','zip'):
        total=sum(v for k,v in counts.items() if k.startswith(fmt+' |'))
        for category,title in titles.items():
            n=counts[f'{fmt} | {category}']
            lines.append(f'| {fmt} | {title} | {n} | {n/total:.2%} |' if total else f'| {fmt} | {title} | 0 | — |')
    lines += ['', '블록 범주는 중복 없이 가장 강한 단서 하나로 분류한다. 후보별 범위 집계는 별도이며 중복될 수 있다.',
              '선언 길이는 잘못 인식한 값일 수도 있으므로 실제 경계 걸침으로 확정하지 않는다.',
              '이 보고서는 기존 실행의 탐색적 분석이다. 검사 규칙·기각 여부를 바꾸지 않는다.',
              'examples.jsonl은 포맷·범주·정답별 처음 N개 예시이며 대표 표본이 아니다.']
    report='\n'.join(lines)+'\n'
    (output/'report.md').write_text(report)
    print(report)
    print(f'저장: {output}')
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',required=True)
    parser.add_argument('--output',default=str(Path(__file__).resolve().parent/'clue-output'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    parser.add_argument('--examples',type=int,default=3)
    args=parser.parse_args()
    audit(args.run,args.output,args.examples)
