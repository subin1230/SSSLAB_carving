"""Inspect existing all-formats evidence; no new classification or rejection."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import struct
import zlib
import numpy as np


def inspect(data, fmt, rows):
    items = []
    for pos in sorted({r['offset'] for r in rows if r.get('offset') is not None}):
        if not 0 <= pos < len(data):
            raise ValueError('candidate offset outside block')
        rules = [r for r in rows if r.get('offset') == pos]
        item = dict(offset=pos, supported=any(r.get('evidence_status') == 'VALID' for r in rules),
                    boundaries=sorted({r.get('boundary_evidence', 'missing') for r in rules}),
                    valid_rules=sorted({r['rule'] for r in rules if r.get('evidence_status') == 'VALID'}),
                    invalid_rules=sorted({r['rule'] for r in rules if r['status'] == 'INVALID'}))
        if fmt == 'png' and pos+8 <= len(data):
            size = int.from_bytes(data[pos:pos+4], 'big')
            kind = data[pos+4:pos+8]
            end = pos+12+size
            item.update(kind=kind.decode('ascii', 'replace'), data_length=size, declared_end=end,
                        complete=end <= len(data))
            if end <= len(data):
                payload = data[pos+8:pos+8+size]
                item['crc_matches'] = (zlib.crc32(kind+payload) & 0xffffffff) == int.from_bytes(data[end-4:end], 'big')
                if kind == b'IHDR' and size == 13:
                    width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
                    item['ihdr'] = dict(width=width, height=height, depth=depth, color=color,
                                        compression=compression, filter=filtering, interlace=interlace)
                if kind in (b'tEXt', b'zTXt', b'iTXt'):
                    item['keyword'] = payload.split(b'\0', 1)[0].decode('latin1')
                    # Do not decompress arbitrary metadata. Preview is bounded.
                    if kind == b'tEXt':
                        item['text_preview'] = payload.split(b'\0', 1)[-1][:160].decode('latin1')
        elif fmt == 'zip':
            sig = data[pos:pos+4]
            item['kind'] = sig.hex()
            if sig == b'PK\x03\x04' and pos+30 <= len(data):
                flags, method = struct.unpack_from('<HH', data, pos+6)
                crc, compressed, uncompressed = struct.unpack_from('<III', data, pos+14)
                nl, xl = struct.unpack_from('<HH', data, pos+26)
                name_start, header_end = pos+30, pos+30+nl+xl
                item.update(flags=flags, method=method, crc=f'{crc:08x}', compressed_size=compressed,
                            uncompressed_size=uncompressed, header_complete=header_end <= len(data),
                            declared_payload_end=header_end+compressed)
            elif sig == b'PK\x01\x02' and pos+46 <= len(data):
                flags, method = struct.unpack_from('<HH', data, pos+8)
                nl, xl, cl = struct.unpack_from('<HHH', data, pos+28)
                name_start, header_end = pos+46, pos+46+nl+xl+cl
                item.update(flags=flags, method=method, header_complete=header_end <= len(data))
            else:
                items.append(item)
                continue
            if name_start+nl <= len(data):
                item['filename'] = data[name_start:name_start+nl].decode('utf-8' if flags & 2048 else 'cp437', 'replace')
        items.append(item)
    return items


def analyze(run, output):
    run, output = Path(run), Path(output)
    summary = json.loads((run/'summary.json').read_text(encoding='utf-8'))
    if summary['selection_mode'] != 'all-formats':
        raise ValueError('all-formats run required')
    with np.load(summary['inputs']['npz'], allow_pickle=False) as archive:
        x, y = archive['x'], archive['y']
    if x.dtype != np.uint8 or x.shape != (summary['total_rows'], summary['block_size']) or y.shape != (len(x),):
        raise ValueError('NPZ shape/dtype mismatch')
    output.mkdir(parents=True, exist_ok=False)
    groups = defaultdict(Counter)
    kinds, boundaries, keywords, names = Counter(), Counter(), Counter(), Counter()
    seen, supported_blocks = set(), 0
    with (run/'evidence.jsonl').open(encoding='utf-8') as source, (output/'supported_details.jsonl').open('w', encoding='utf-8') as details:
        for line in source:
            record = json.loads(line)
            i = record['row']
            if i in seen or not 0 <= i < len(x) or int(record['metadata']['label']) != int(y[i]):
                raise ValueError('duplicate row or metadata/NPZ mismatch')
            seen.add(i)
            supported = [fmt for fmt, rows in record['results'].items()
                         if any(r.get('evidence_status') == 'VALID' for r in rows)]
            if not supported:
                continue
            supported_blocks += 1
            data = x[i].tobytes()
            structures = {fmt: inspect(data, fmt, record['results'][fmt]) for fmt in supported}
            truth, predicted = record['ground_truth_type'], record['predicted_type']
            for fmt, items in structures.items():
                key = f'{fmt} | truth={truth}'
                groups[key]['blocks'] += 1
                groups[key]['ffc_correct'] += truth == predicted
                groups[key]['ffc_wrong'] += truth != predicted
                groups[key]['source_label_matches_structure'] += truth == fmt
                for item in items:
                    if not item['supported']:
                        continue
                    kinds[f"{key} | {item.get('kind', 'partial')}"] += 1
                    for boundary in item['boundaries']:
                        boundaries[f'{key} | {boundary}'] += 1
                    if 'keyword' in item:
                        keywords[f"{key} | {item['keyword']}"] += 1
                    if 'filename' in item:
                        names[f"{key} | {item['filename']}"] += 1
            details.write(json.dumps(dict(row=i, block_id=record['block_id'], truth=truth,
                prediction=predicted, metadata=record['metadata'], structures=structures,
                block_hex=data.hex()), ensure_ascii=False)+'\n')
    if len(seen) != summary['inspected_counts']['candidate_blocks'] or supported_blocks != summary['inspected_counts']['supported_blocks']:
        raise ValueError('evidence/summary count mismatch')
    result = dict(source_run=str(run.resolve()), supported_blocks=supported_blocks,
                  per_structure_and_truth=dict(groups), supported_candidate_kind_counts=dict(kinds),
                  boundary_counts=dict(boundaries), metadata_keywords=dict(keywords), zip_filenames=dict(names),
                  note='Labels only stratify findings. Counts of kinds/boundaries are candidate counts, not blocks. Metadata and filenames are observations, not source-format proof. No reclassification.')
    (output/'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 구조 근거와 원본 라벨 비교', '', f'근거 반영 VALID 규칙이 있는 블록: {supported_blocks:,}', '',
             '| 구조 / 정답 라벨 | 블록 수 | FFC 정답 | FFC 오답 |', '|---|---:|---:|---:|']
    for key, counts in sorted(groups.items()):
        lines.append(f"| {key.replace(' | ', ' / ')} | {counts['blocks']} | {counts['ffc_correct']} | {counts['ffc_wrong']} |")
    for title, counter in [('근거 있는 후보 종류', kinds), ('PNG 메타데이터 키워드', keywords), ('근거 있는 ZIP 후보의 파일명', names)]:
        lines.extend(['', '## '+title, ''])
        lines.extend(f'- {json.dumps(key, ensure_ascii=False)}: {n}건' for key, n in counter.most_common())
        if not counter:
            lines.append('추출 가능한 항목 없음')
    lines.extend(['', '상세 파일에는 각 블록의 모든 해당 포맷 후보·IHDR·파일명·전체 512바이트 hex를 저장함.',
                  '후보 종류 건수와 블록 수는 다름. 내부 구조를 원본 파일 형식으로 치환하지 않음.'])
    (output/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(f'근거 블록 {supported_blocks:,}개 분석 완료. 저장: {output}', flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--output', default=str(Path(__file__).resolve().parent/'structure-analysis'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    args = parser.parse_args()
    analyze(args.run, args.output)
