"""Compare pilot identities with Same-File model data using Tesserae's UUID cache.

This is an evaluation audit. It never selects pairs or changes model input.
"""
import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path

import numpy as np

from audit_zip_payload import read_truth
from zip_pair_candidates import fingerprint, load_blocks
from zip_pair_payload import file_hash

SPLITS = ('train', 'val', 'test')


def audit(pilot, model_blocks, cache, data_root, output):
    pilot, model_blocks, cache, data_root, output = map(
        Path, (pilot, model_blocks, cache, data_root, output))
    if output.exists():
        raise FileExistsError('output exists; choose a new audit directory')
    x = load_blocks(pilot/'blocks.npy')
    truth, _ = read_truth(pilot/'truth.csv', len(x))
    paths = dict(codes=cache/'file_code.npy', table=cache/'uuid_table.npy',
                 indices=data_root/'index.npy', types=data_root/'filetype_id.npy')
    arrays = {k: np.load(v, mmap_mode='r', allow_pickle=False) for k, v in paths.items()}
    codes, table, indices, types = (arrays[k] for k in ('codes', 'table', 'indices', 'types'))
    if any(a.ndim != 1 for a in arrays.values()) or not len(codes) == len(indices) == len(types):
        raise ValueError('invalid cache/metadata shapes')
    if any(a.dtype.kind not in 'iu' for a in (codes, indices, types)) or table.dtype.kind != 'S':
        raise ValueError('requires integer metadata and fixed-width byte UUIDs')
    uuid_cache = {}

    def identity(gid):
        if not 0 <= gid < len(codes):
            raise ValueError('global block ID outside metadata')
        code = int(codes[gid])
        if not 0 <= code < len(table):
            raise ValueError('file code outside UUID table')
        if code not in uuid_cache:
            uuid_cache[code] = bytes(table[code]).decode('ascii')
        return uuid_cache[code]

    pilot_files, pilot_ids = {}, set()
    for m in truth:
        gid = int(m['global_block_id'])
        uuid = identity(gid)
        if uuid != m['source_file_id'] or int(indices[gid])*512 != int(m['source_byte_offset']):
            raise ValueError('pilot truth does not match Tesserae UUID/index cache')
        if gid in pilot_ids:
            raise ValueError('duplicate pilot global block ID')
        pilot_ids.add(gid)
        if uuid in pilot_files and pilot_files[uuid] != m['format']:
            raise ValueError('one pilot UUID has multiple format labels')
        pilot_files[uuid] = m['format']

    counts = defaultdict(Counter)
    by_uuid, by_source, source_splits = defaultdict(set), defaultdict(set), defaultdict(set)
    matches = defaultdict(lambda: defaultdict(set))
    exact = defaultdict(set)
    seen = set()
    with model_blocks.open(newline='') as f:
        for row in csv.DictReader(f):
            split, source = row['split'], row['source_id']
            gid = int(row['global_block_index'])
            if split not in SPLITS or not source:
                raise ValueError('invalid model metadata split/source')
            if gid in seen:
                raise ValueError('duplicate global block ID in model metadata')
            seen.add(gid)
            uuid = identity(gid)
            start, end = int(row['source_start_global']), int(row['source_end_global'])
            local = int(row['source_local_index'])
            if not start <= gid < end or start+local != gid or int(indices[gid]) != local:
                raise ValueError(f'model metadata index/interval mismatch at {gid}')
            if int(row['old_filetype_id']) != int(types[gid]):
                raise ValueError(f'model metadata type mismatch at {gid}')
            counts[row['file_type']][split] += 1
            by_uuid[uuid].add(split)
            by_source[source].add(uuid)
            source_splits[source].add(split)
            if uuid in pilot_files:
                matches[uuid][split].add(source)
            if gid in pilot_ids:
                exact[split].add(gid)

    output_rows = []
    for uuid, fmt in sorted(pilot_files.items()):
        output_rows.append(dict(source_file_id=uuid, format=fmt,
                                **{s+'_model_sources': ';'.join(sorted(matches[uuid][s])) for s in SPLITS}))
    overlap_counts = {s: sum(bool(matches[u][s]) for u in pilot_files) for s in SPLITS}
    by_format = {}
    for fmt in sorted(set(pilot_files.values())):
        files = [u for u in pilot_files if pilot_files[u] == fmt]
        by_format[fmt] = dict(pilot_files=len(files), model_block_counts=dict(counts[fmt]),
                             overlap_files={s: sum(bool(matches[u][s]) for u in files) for s in SPLITS},
                             not_observed_in_model_metadata=sum(not any(matches[u].values()) for u in files))
    anomalies = dict(model_source_ids_with_multiple_observed_uuids=sum(len(v)>1 for v in by_source.values()),
                     model_source_ids_in_multiple_splits=sum(len(v)>1 for v in source_splits.values()),
                     observed_uuids_in_multiple_model_splits=sum(len(v)>1 for v in by_uuid.values()))
    report = dict(
        pilot_blocks=len(x), pilot_files=len(pilot_files), model_metadata_blocks=len(seen),
        pilot_blocks_sha256=fingerprint(x), truth_sha256=file_hash(pilot/'truth.csv'),
        model_metadata_path=str(model_blocks), model_metadata_sha256=file_hash(model_blocks),
        cache_arrays={k: dict(path=str(p), shape=list(arrays[k].shape), dtype=str(arrays[k].dtype),
                             file_size=p.stat().st_size) for k, p in paths.items()},
        overlap_files=overlap_counts, exact_block_overlap={s: len(exact[s]) for s in SPLITS},
        not_observed_in_model_metadata=sum(not any(matches[u].values()) for u in pilot_files),
        by_format=by_format, identity_diagnostics=anomalies,
        truth_used=True, affects_scoring=False,
        note='UUID overlap mapped from every final model block using the shared Tesserae cache. This does not establish content-level deduplication, full interval identity, or independence from FiFTy pretraining. Counts across splits can overlap. Train overlap is seen-source evaluation; validation overlap may reflect model selection. No pilot block is removed and no score is changed.',
    )
    output.mkdir(parents=True, exist_ok=False)
    (output/'overlap_summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    with (output/'pilot_source_overlap.csv').open('x', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['source_file_id', 'format']+[s+'_model_sources' for s in SPLITS])
        w.writeheader()
        w.writerows(output_rows)
    print('포맷 | 파일 수 | train 중복 | val 중복 | test 중복 | 모델 메타에서 미관측')
    for fmt, s in by_format.items():
        o = s['overlap_files']
        print(f"{fmt} | {s['pilot_files']} | {o['train']} | {o['val']} | {o['test']} | {s['not_observed_in_model_metadata']}")
    print('\n[전체 파일 중복]', json.dumps(overlap_counts))
    print('[동일 블록 중복]', json.dumps(report['exact_block_overlap']))
    print('[파일 식별 진단]', json.dumps(anomalies))
    print('중복 집계는 평가 해석용이며 점수 입력이나 후보 선택에 사용하지 않음.')
    print('저장:', output)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pilot', required=True)
    p.add_argument('--model-blocks', required=True)
    p.add_argument('--cache', required=True)
    p.add_argument('--data-root', required=True, help='directory containing index.npy and filetype_id.npy')
    p.add_argument('--output', required=True)
    a = p.parse_args()
    audit(a.pilot, a.model_blocks, a.cache, a.data_root, a.output)


if __name__ == '__main__':
    main()
