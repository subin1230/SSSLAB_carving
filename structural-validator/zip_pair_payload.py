"""Test complete ZIP entry payloads in adjacent candidate block pairs.

PASS/FAIL describe a pair under an assumed local-header interpretation, not
source-file identity. No labels, original offsets or model scores are used by check.
"""
import argparse
from collections import Counter, defaultdict
import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import zlib

from zip_pair_candidates import BLOCK_SIZE, evaluate, fingerprint, load_blocks

VARIANTS = ('baseline', 'keep_not_failed', 'payload_passed')
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
ALLOWED_FLAGS = 0x0800 | 0x0006  # UTF-8 and Deflate compression options only


def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def payload_check(source, target, query, candidate, max_output_bytes=MAX_OUTPUT_BYTES):
    """Validate only complete data, and only if target bytes participate in it."""
    details = dict(scope='pair_under_local_header_hypothesis')

    def result(status, reason):
        return dict(details, status=status, reason=reason)

    if query['rule'] != 'zip_next':
        return result('UNKNOWN', 'NOT_A_NEXT_ENTRY_RELATION')
    if query.get('target_block_delta') != 1:
        return result('UNKNOWN', 'MISSING_MIDDLE_BLOCKS_OR_NONADJACENT')
    if len(source) != BLOCK_SIZE or len(target) != BLOCK_SIZE:
        raise ValueError('requires two complete 512-byte input blocks')
    if max_output_bytes < 1:
        raise ValueError('max_output_bytes must be positive')
    p = query['source_offset']
    if not 0 <= p <= BLOCK_SIZE - 30 or source[p:p+4] != b'PK\x03\x04':
        raise ValueError('query does not point to a complete local fixed header')
    flags, method = struct.unpack_from('<HH', source, p+6)
    crc, csize, usize = struct.unpack_from('<III', source, p+14)
    nlen, xlen = struct.unpack_from('<HH', source, p+26)
    details.update(method=method, compressed_size=csize, declared_size=usize,
                   declared_crc=f'{crc:08x}')
    if flags & ~ALLOWED_FLAGS or method not in (0, 8):
        return result('UNKNOWN', 'UNSUPPORTED_FLAGS_OR_METHOD')
    if 0xffffffff in (csize, usize):
        return result('UNKNOWN', 'ZIP64_SENTINEL')
    data_start = p + 30 + nlen + xlen
    data_end = data_start + csize
    expected_end = BLOCK_SIZE + query['target_offset']
    if data_end != expected_end or candidate['target_offset'] != query['target_offset']:
        raise ValueError('candidate position differs from declared entry end')
    if not data_start <= data_end <= 2 * BLOCK_SIZE:
        return result('UNKNOWN', 'ENTRY_NOT_COMPLETE_IN_PAIR')
    b = source + target  # Hypothesized adjacency, never a truth-based join.
    if b[data_end:data_end+4] not in (b'PK\x03\x04', b'PK\x01\x02'):
        raise ValueError('candidate next signature no longer matches')
    used_from_target = max(0, data_end - max(BLOCK_SIZE, data_start))
    details['payload_bytes_from_target'] = used_from_target
    if used_from_target == 0:
        return result('UNKNOWN', 'NO_TARGET_PAYLOAD_BYTES')
    if usize == 0:
        return result('UNKNOWN', 'EMPTY_OUTPUT_NOT_DISCRIMINATIVE')
    if usize > max_output_bytes:
        return result('UNKNOWN', 'DECLARED_OUTPUT_EXCEEDS_RESOURCE_LIMIT')

    # Variable header bytes must be available. Unhandled extras remain unknown;
    # do not mistake an unsupported interpretation for a payload contradiction.
    extra = b[p+30+nlen:data_start]
    pos = 0
    while pos < len(extra):
        if pos + 4 > len(extra):
            return result('UNKNOWN', 'EXTRA_LAYOUT_UNSUPPORTED')
        tag, size = struct.unpack_from('<HH', extra, pos)
        if pos + 4 + size > len(extra):
            return result('UNKNOWN', 'EXTRA_LAYOUT_UNSUPPORTED')
        if tag == 0x0001:
            return result('UNKNOWN', 'ZIP64_EXTRA_UNSUPPORTED')
        pos += 4 + size

    payload = b[data_start:data_end]
    if method == 0:
        decoded = payload
    else:
        inflater = zlib.decompressobj(-15)
        try:
            # Bound allocation; one byte beyond the declared size detects a
            # contradiction without accepting truncated decompressor output.
            decoded = inflater.decompress(payload, min(usize, max_output_bytes) + 1)
        except zlib.error:
            return result('FAIL', 'DEFLATE_STREAM_ERROR')
        if len(decoded) > usize:
            details['observed_size_at_least'] = len(decoded)
            return result('FAIL', 'OUTPUT_EXCEEDS_DECLARED_SIZE')
        if not inflater.eof:
            return result('FAIL', 'DEFLATE_STREAM_NOT_FINISHED')
        if inflater.unused_data or inflater.unconsumed_tail:
            return result('FAIL', 'DECLARED_COMPRESSED_EXTENT_MISMATCH')
    details['observed_size'] = len(decoded)
    if len(decoded) != usize:
        return result('FAIL', 'UNCOMPRESSED_SIZE_MISMATCH')
    actual_crc = zlib.crc32(decoded) & 0xffffffff
    details['observed_crc'] = f'{actual_crc:08x}'
    if actual_crc != crc:
        return result('FAIL', 'PAYLOAD_CRC_MISMATCH')
    return result('PASS', 'COMPLETE_PAYLOAD_SIZE_AND_CRC_MATCH')


def check(blocks, candidates, output, max_output_bytes=MAX_OUTPUT_BYTES):
    if max_output_bytes < 1:
        raise ValueError('max_output_bytes must be positive')
    x = load_blocks(blocks)
    candidates, output = Path(candidates), Path(output)
    baseline = json.loads((candidates/'candidate_summary.json').read_text())
    digest = fingerprint(x)
    if baseline['blocks_sha256'] != digest:
        raise ValueError('blocks differ from original candidate input')
    query_path = candidates/'queries.jsonl'
    output.mkdir(parents=True, exist_ok=False)
    counts, by_rule, query_counts = Counter(), defaultdict(Counter), Counter()
    variant_counts = {name: defaultdict(Counter) for name in VARIANTS}
    with contextlib.ExitStack() as stack:
        writers = {}
        for name in VARIANTS:
            (output/name).mkdir()
            writers[name] = stack.enter_context((output/name/'queries.jsonl').open('w'))
        evidence = stack.enter_context((output/'payload_evidence.jsonl').open('w'))
        reader = stack.enter_context(query_path.open())
        for qid, line in enumerate(reader):
            q = json.loads(line)
            sid = q['source_sample_id']
            if not isinstance(sid, int) or not 0 <= sid < len(x):
                raise ValueError('invalid source sample ID')
            query_counts[q['rule']+' | queries'] += 1
            if q['rule'] == 'zip_next' and q.get('target_block_delta') == 1:
                query_counts['zip_next | adjacent_queries'] += 1
            selections = {name: [] for name in VARIANTS}
            source = x[sid].tobytes()
            for t in q['candidates']:
                tid = t['target_sample_id']
                if not isinstance(tid, int) or not 0 <= tid < len(x) or sid == tid:
                    raise ValueError('invalid target sample ID')
                r = payload_check(source, x[tid].tobytes(), q, t, max_output_bytes)
                key = r['status']+' | '+r['reason']
                counts[key] += 1
                by_rule[q['rule']][r['status']] += 1
                evidence.write(json.dumps(dict(query_id=qid, source_sample_id=sid,
                    source_offset=q['source_offset'], target_sample_id=tid,
                    target_offset=t['target_offset'], rule=q['rule'], check=r))+'\n')
                selections['baseline'].append(t)
                if r['status'] != 'FAIL':
                    selections['keep_not_failed'].append(t)
                if r['status'] == 'PASS':
                    selections['payload_passed'].append(t)
            for name, selected in selections.items():
                writers[name].write(json.dumps(dict(q, candidates=selected))+'\n')
                stats = variant_counts[name][q['rule']]
                stats['queries'] += 1
                stats['candidate_relations'] += len(selected)
                stats['singleton_queries'] += len(selected) == 1
    variant_hashes = {}
    for name in VARIANTS:
        summary = dict(blocks=len(x), blocks_sha256=digest, truth_used=False,
                       variant=name, by_rule=dict(variant_counts[name]),
                       note='Pair hypotheses only. FAIL is conditional, not proof of different source files.')
        (output/name/'candidate_summary.json').write_text(json.dumps(summary, indent=2))
        variant_hashes[name] = file_hash(output/name/'queries.jsonl')
    report = dict(blocks=len(x), blocks_sha256=digest, source_queries_sha256=file_hash(query_path),
                  truth_used=False, max_output_bytes=max_output_bytes, counts=dict(counts),
                  by_rule=dict(by_rule), query_counts=dict(query_counts),
                  variant_queries_sha256=variant_hashes,
                  checker_version='zip-pair-payload-v1', zlib_version=zlib.ZLIB_RUNTIME_VERSION,
                  note='No truth/model score used. Only adjacent zip_next hypotheses with complete nonempty payload using target bytes are checked. Gaps, unsupported cases and resource limits stay UNKNOWN. PASS does not prove file identity; FAIL rejects only the assumed entry reconstruction.')
    (output/'payload_summary.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ('blocks', 'by_rule', 'counts', 'query_counts')}, indent=2))
    print('검사 저장:', output)
    return report


def compare(blocks, truth, run):
    run = Path(run)
    checked = json.loads((run/'payload_summary.json').read_text())
    if checked['blocks_sha256'] != fingerprint(load_blocks(blocks)):
        raise ValueError('blocks differ from payload check input')
    if (run/'comparison.json').exists() or any((run/v/'evaluation.json').exists() for v in VARIANTS):
        raise FileExistsError('evaluation already exists; use a new check output directory')
    for v in VARIANTS:
        if file_hash(run/v/'queries.jsonl') != checked['variant_queries_sha256'][v]:
            raise ValueError('saved candidate variants changed since check')
    reports = {}
    for v in VARIANTS:
        with contextlib.redirect_stdout(io.StringIO()):
            reports[v] = evaluate(blocks, truth, run/v)
    before = reports['baseline']['by_rule']
    after = reports['keep_not_failed']['by_rule']
    removed = {}
    for rule in before:
        keys = ('candidate_relations', 'exact_target_relations', 'same_file_relations', 'wrong_file_relations')
        removed[rule] = {k: before[rule].get(k, 0)-after[rule].get(k, 0) for k in keys}
    result = dict(variants=reports, removed_by_conditional_failure=removed,
                  note='Exploratory paired comparison on the same pilot. Check exact_target_relations removed (false rejection) as well as wrong-file removals. UNKNOWN is retained in keep_not_failed. Payload passed subset has narrower coverage and is not proof of source identity. No grouping or Same-File model evaluation performed.')
    (run/'comparison.json').write_text(json.dumps(result, indent=2))
    print('방식 | 규칙 | 후보 | 정확한 대상 | 다른 파일 | 정확한 대상 비율')
    for v in VARIANTS:
        for rule, s in reports[v]['by_rule'].items():
            precision = s['exact_target_precision']
            ratio = '—' if precision is None else f'{precision:.2%}'
            print(f"{v} | {rule} | {s.get('candidate_relations', 0)} | "
                  f"{s.get('exact_target_relations', 0)} | {s.get('wrong_file_relations', 0)} | {ratio}")
    print('\nFAIL로 제외된 관계 (정확한 대상 제거는 손실):')
    print(json.dumps(removed, indent=2))
    print('비교 저장:', run/'comparison.json')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('check')
    c.add_argument('--blocks', required=True)
    c.add_argument('--candidates', required=True)
    c.add_argument('--output', required=True)
    c.add_argument('--max-output-bytes', type=int, default=MAX_OUTPUT_BYTES)
    e = sub.add_parser('evaluate')
    e.add_argument('--blocks', required=True)
    e.add_argument('--truth', required=True)
    e.add_argument('--run', required=True)
    a = parser.parse_args()
    if a.command == 'check':
        check(a.blocks, a.candidates, a.output, a.max_output_bytes)
    else:
        compare(a.blocks, a.truth, a.run)


if __name__ == '__main__':
    main()
