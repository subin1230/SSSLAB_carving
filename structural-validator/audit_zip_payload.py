"""Post-hoc truth audit of ZIP payload evidence; never generates grouping edges."""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import struct
import zlib

from zip_pair_candidates import BLOCK_SIZE, fingerprint, load_blocks
from zip_pair_payload import file_hash, payload_check


def read_truth(path, count):
    with open(path, newline='') as f:
        truth = list(csv.DictReader(f))
    if len(truth) != count:
        raise ValueError('truth row count mismatch')
    lookup = {}
    for i, m in enumerate(truth):
        offset = int(m['source_byte_offset'])
        if int(m['sample_id']) != i or offset < 0 or offset % BLOCK_SIZE:
            raise ValueError('truth order/alignment mismatch')
        if not m['source_file_id'] or not m.get('format'):
            raise ValueError('truth requires source_file_id and format')
        key = (m['source_file_id'], offset)
        if key in lookup:
            raise ValueError('duplicate truth position')
        lookup[key] = i
    return truth, lookup


def reference_target(q, truth, lookup, x):
    m = truth[q['source_sample_id']]
    if q['rule'] == 'zip_next':
        address = int(m['source_byte_offset']) + q['target_block_delta'] * BLOCK_SIZE + q['target_offset']
        expected = (b'PK\x03\x04', b'PK\x01\x02')
    elif q['rule'] in ('cd_local', 'eocd'):
        address = q['archive_target_offset']
        expected = (b'PK\x03\x04',) if q['rule'] == 'cd_local' else (b'PK\x01\x02',)
    else:
        raise ValueError('unsupported relation rule')
    offset = address // BLOCK_SIZE * BLOCK_SIZE
    tid = lookup.get((m['source_file_id'], offset))
    p = q['target_offset']
    available = tid is not None and tid != q['source_sample_id'] and x[tid, p:p+4].tobytes() in expected
    return tid, offset, bool(available)


def describe_sample(sid, truth):
    m = truth[sid]
    return dict(sample_id=sid, format=m['format'], source_file_id=m['source_file_id'],
                source_byte_offset=int(m['source_byte_offset']))


def coverage(rows, truth):
    sources = {r['source_sample_id'] for r in rows}
    targets = {r['target_sample_id'] for r in rows}
    touched = sources | targets
    pairs = {tuple(sorted((r['source_sample_id'], r['target_sample_id']))) for r in rows}
    exact = sum(r['exact_target'] for r in rows)
    return dict(
        candidate_relations=len(rows), exact_target_relations=exact,
        wrong_file_relations=sum(not r['same_file'] for r in rows),
        same_file_wrong_target_relations=sum(r['same_file'] and not r['exact_target'] for r in rows),
        exact_target_precision=exact / len(rows) if rows else None,
        unique_queries=len({r['query_id'] for r in rows}), unique_unordered_pairs=len(pairs),
        source_blocks=len(sources), target_blocks=len(targets), touched_blocks=len(touched),
        source_files=len({truth[i]['source_file_id'] for i in sources}),
        target_files=len({truth[i]['source_file_id'] for i in targets}),
        touched_files=len({truth[i]['source_file_id'] for i in touched}),
        exact_relation_source_files=len({truth[r['source_sample_id']]['source_file_id'] for r in rows if r['exact_target']}),
    )


def entry_parts(source, target, q):
    """Only called for an adjacent zip_next interpretation replayed as PASS/FAIL."""
    pair = source + target
    p = q['source_offset']
    csize = struct.unpack_from('<I', source, p+18)[0]
    nlen, xlen = struct.unpack_from('<HH', source, p+26)
    start = p+30+nlen+xlen
    end = start+csize
    return pair[start:end], pair[p+30:p+30+nlen], max(0, start-BLOCK_SIZE), end-BLOCK_SIZE


def decode_passed(payload, check_result, limit):
    # Replay has already verified termination, size, CRC and the allocation cap.
    if check_result['status'] != 'PASS':
        raise ValueError('only decode a replayed PASS')
    if check_result['method'] == 0:
        return payload
    inflater = zlib.decompressobj(-15)
    decoded = inflater.decompress(payload, min(check_result['declared_size'], limit)+1)
    if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise ValueError('inconsistent Deflate replay')
    return decoded


def inspect_case(row, q, truth, x, limit):
    sid, tid = row['source_sample_id'], row['target_sample_id']
    source, target = x[sid].tobytes(), x[tid].tobytes()
    rid = row['reference_sample_id']
    payload, name, used_start, used_end = entry_parts(source, target, q)
    case = dict(
        query_id=row['query_id'], rule=q['rule'], check=row['check'],
        source=describe_sample(sid, truth), candidate=describe_sample(tid, truth),
        reference=describe_sample(rid, truth) if rid is not None else None,
        expected_target_byte_offset=row['expected_target_byte_offset'],
        source_header_offset=q['source_offset'], target_header_offset=q['target_offset'],
        same_file=row['same_file'], exact_target=row['exact_target'],
        filename=name.decode('utf-8', errors='replace'), filename_hex=name.hex(),
        target_payload_span=[used_start, used_end],
    )
    if row['same_file']:
        case['candidate_minus_expected_blocks'] = (
            int(truth[tid]['source_byte_offset'])-row['expected_target_byte_offset']) // BLOCK_SIZE
    if rid is None:
        case['comparison'] = 'REFERENCE_TARGET_ABSENT'
        return case
    if not row['reference_signature_available']:
        case['comparison'] = 'REFERENCE_SIGNATURE_MISMATCH'
        return case

    reference = x[rid].tobytes()
    ref_check = payload_check(source, reference, q,
                              dict(target_offset=q['target_offset']), limit)
    ref_payload, _, _, _ = entry_parts(source, reference, q)
    case.update(
        reference_check=ref_check,
        whole_target_block_equal=target == reference,
        used_target_payload_equal=target[used_start:used_end] == reference[used_start:used_end],
        reconstructed_compressed_payload_equal=payload == ref_payload,
        candidate_compressed_sha256=hashlib.sha256(payload).hexdigest(),
        reference_compressed_sha256=hashlib.sha256(ref_payload).hexdigest(),
    )
    if row['check']['status'] != 'PASS':
        case['comparison'] = 'REFERENCE_PAIR_PASSES' if ref_check['status'] == 'PASS' else 'REFERENCE_PAIR_NOT_PASS'
        return case
    if ref_check['status'] != 'PASS':
        case['comparison'] = 'REFERENCE_PAIR_NOT_PASS'
        return case
    decoded = decode_passed(payload, row['check'], limit)
    ref_decoded = decode_passed(ref_payload, ref_check, limit)
    case.update(decoded_payload_equal=decoded == ref_decoded,
                candidate_decoded_sha256=hashlib.sha256(decoded).hexdigest(),
                reference_decoded_sha256=hashlib.sha256(ref_decoded).hexdigest())
    if target == reference:
        case['comparison'] = 'IDENTICAL_TARGET_BLOCK'
    elif payload == ref_payload:
        case['comparison'] = 'IDENTICAL_RECONSTRUCTED_COMPRESSED_PAYLOAD'
    elif decoded == ref_decoded:
        case['comparison'] = 'IDENTICAL_DECODED_PAYLOAD_ONLY'
    else:
        case['comparison'] = 'DIFFERENT_DECODED_BYTES_WITH_SAME_SIZE_AND_CRC'
    return case


def audit(blocks, truth_path, run, output):
    x = load_blocks(blocks)
    run, output = Path(run), Path(output)
    if output.exists():
        raise FileExistsError('audit output already exists; choose a new directory')
    saved = json.loads((run/'payload_summary.json').read_text())
    if saved['checker_version'] != 'zip-pair-payload-v1':
        raise ValueError('unsupported checker version')
    if saved['blocks_sha256'] != fingerprint(x):
        raise ValueError('blocks differ from payload check input')
    query_path = run/'baseline/queries.jsonl'
    if file_hash(query_path) != saved['variant_queries_sha256']['baseline']:
        raise ValueError('saved baseline queries changed since check')
    queries = [json.loads(line) for line in query_path.read_text().splitlines()]
    evidence_path = run/'payload_evidence.jsonl'
    evidence = {}
    for line in evidence_path.read_text().splitlines():
        r = json.loads(line)
        key = (r['query_id'], r['target_sample_id'], r['target_offset'])
        if key in evidence:
            raise ValueError('duplicate payload evidence')
        evidence[key] = r
    truth, lookup = read_truth(truth_path, len(x))
    limit = saved['max_output_bytes']
    rows, cases = [], defaultdict(list)
    for qid, q in enumerate(queries):
        sid = q['source_sample_id']
        if type(sid) is not int or not 0 <= sid < len(x):
            raise ValueError('invalid source sample ID')
        rid, offset, available = reference_target(q, truth, lookup, x)
        for t in q['candidates']:
            tid = t['target_sample_id']
            if type(tid) is not int or not 0 <= tid < len(x) or tid == sid:
                raise ValueError('invalid target sample ID')
            key = (qid, tid, t['target_offset'])
            if key not in evidence:
                raise ValueError('missing payload evidence')
            stored = evidence.pop(key)
            replay = payload_check(x[sid].tobytes(), x[tid].tobytes(), q, t, limit)
            expected = dict(query_id=qid, source_sample_id=sid, source_offset=q['source_offset'],
                            target_sample_id=tid, target_offset=t['target_offset'], rule=q['rule'], check=replay)
            if stored != expected:
                raise ValueError('payload evidence differs from replay; check files/checker/zlib version')
            row = dict(expected, same_file=truth[sid]['source_file_id'] == truth[tid]['source_file_id'],
                       exact_target=available and tid == rid, reference_sample_id=rid,
                       expected_target_byte_offset=offset, reference_signature_available=available)
            rows.append(row)
            category = None
            if replay['status'] == 'PASS' and not row['same_file']:
                category = 'cross_file_pass'
            elif replay['status'] == 'FAIL' and row['exact_target']:
                category = 'exact_target_fail'
            elif replay['status'] == 'FAIL' and row['same_file']:
                category = 'same_file_wrong_target_fail'
            if category:
                cases[category].append(inspect_case(row, q, truth, x, limit))
    if evidence:
        raise ValueError('unmatched payload evidence')
    by_status = {s: coverage([r for r in rows if r['check']['status'] == s], truth)
                 for s in ('PASS', 'FAIL', 'UNKNOWN')}
    by_format = {}
    for fmt in sorted({m['format'] for m in truth}):
        selected = [r for r in rows if truth[r['source_sample_id']]['format'] == fmt]
        by_format[fmt] = {s: coverage([r for r in selected if r['check']['status'] == s], truth)
                          for s in ('PASS', 'FAIL', 'UNKNOWN')}
    category_names = ('cross_file_pass', 'same_file_wrong_target_fail', 'exact_target_fail')
    report = dict(
        blocks=len(x), input_files=len({m['source_file_id'] for m in truth}),
        blocks_sha256=saved['blocks_sha256'], truth_sha256=file_hash(truth_path),
        baseline_queries_sha256=file_hash(query_path), payload_evidence_sha256=file_hash(evidence_path),
        payload_summary_sha256=file_hash(run/'payload_summary.json'),
        evidence_replayed=True, truth_used=True, stage='post-hoc audit only',
        checker_zlib_version=saved['zlib_version'], audit_zlib_version=zlib.ZLIB_RUNTIME_VERSION,
        by_status=by_status, by_source_format=by_format,
        case_counts={k: len(cases[k]) for k in category_names},
        cross_file_pass_comparisons=dict(Counter(c['comparison'] for c in cases['cross_file_pass'])),
        note='Relation counts are not unique pairs or independent samples. Truth is used only for this audit, including reference payload reconstruction. Byte equality supports content duplication within inspected spans, not archive identity or copying history. FAIL concerns the declared target position, not different-file identity. No candidate, score or grouping is changed.',
    )
    output.mkdir(parents=True, exist_ok=False)
    (output/'audit_summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    for key in category_names:
        with (output/(key+'.jsonl')).open('x') as f:
            for c in cases[key]:
                f.write(json.dumps(c, ensure_ascii=False)+'\n')
    print('[PASS 적용 범위 — 관계 건수와 고유 쌍은 다름]')
    print(json.dumps(by_status['PASS'], ensure_ascii=False, indent=2))
    print('\n출발 포맷 | PASS 관계 | 정확한 대상 | 다른 파일 | 고유 쌍 | 출발 파일')
    for fmt, groups in by_format.items():
        s = groups['PASS']
        print(f"{fmt} | {s['candidate_relations']} | {s['exact_target_relations']} | "
              f"{s['wrong_file_relations']} | {s['unique_unordered_pairs']} | {s['source_files']}")
    print('\n[검토 사례 수]')
    print(json.dumps(report['case_counts'], ensure_ascii=False, indent=2))
    print('\n[다른 파일인데 PASS한 원인 비교]')
    print(json.dumps(report['cross_file_pass_comparisons'], ensure_ascii=False, indent=2))
    for c in cases['cross_file_pass'][:10]:
        print(f"source={c['source']['sample_id']} candidate={c['candidate']['sample_id']} "
              f"reference={c['reference']['sample_id'] if c['reference'] else None} | {c['comparison']}")
    print('\n[같은 파일인데 FAIL한 위치 — 실제 대상과의 블록 차이]')
    for c in cases['same_file_wrong_target_fail'][:10]:
        print(f"source={c['source']['sample_id']} candidate={c['candidate']['sample_id']} | "
              f"위치차이={c['candidate_minus_expected_blocks']:+d} | "
              f"{c['check']['reason']} | {c['comparison']}")
    if cases['exact_target_fail']:
        print('주의: 정확한 대상이 FAIL한 사례 있음. exact_target_fail.jsonl 확인 필요.')
    print('\n정답을 사용하는 사후 분석임. 기존 판정·후보·grouping 입력은 변경하지 않음.')
    print('저장:', output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blocks', required=True)
    parser.add_argument('--truth', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--output', required=True)
    a = parser.parse_args()
    audit(a.blocks, a.truth, a.run, a.output)


if __name__ == '__main__':
    main()
