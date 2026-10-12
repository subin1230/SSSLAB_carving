"""ZIP relation candidate enumeration from bytes only, followed by separate truth evaluation.

Candidates are hypotheses, NOT same-file facts or automatic grouping edges.
"""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import struct
import numpy as np

BLOCK_SIZE = 512
EXCLUDED_FLAGS = 1 | 8 | 64 | 8192


def load_blocks(path):
    x = np.load(path, mmap_mode='r', allow_pickle=False)
    if x.dtype != np.uint8 or x.ndim != 2 or x.shape[1] != BLOCK_SIZE:
        raise ValueError('requires uint8 (N,512) blocks')
    return x


def fingerprint(x):
    h = hashlib.sha256()
    h.update(str(x.shape).encode('ascii'))
    for start in range(0, len(x), 8192):
        h.update(x[start:start+8192].tobytes())
    return h.hexdigest()


def scan_headers(x):
    """Fixed headers only. A complete name is required for cd_local matching."""
    records = []
    counts = Counter()
    for sid, row in enumerate(x):
        b = row.tobytes()
        for p in range(len(b)-3):
            sig = b[p:p+4]
            if sig not in (b'PK\x03\x04', b'PK\x01\x02', b'PK\x05\x06'):
                continue
            kind, size = {b'PK\x03\x04':('local',30), b'PK\x01\x02':('central',46), b'PK\x05\x06':('eocd',22)}[sig]
            if p+size > len(b):
                counts[kind+' | partial_fixed_header'] += 1
                continue
            r = dict(sample_id=sid, offset=p, kind=kind)
            if kind == 'eocd':
                disk, sd, here, total, extent, start, comment = struct.unpack_from('<4H2IH', b, p+4)
                if disk or sd or here != total or total in (0,65535) or 0xffffffff in (extent,start) or extent < total*46:
                    counts['eocd | unsupported_or_inconsistent'] += 1
                    continue
                r.update(cd_size=extent, cd_offset=start, entries=total)
            else:
                shift = 0 if kind == 'local' else 2
                flags, method = struct.unpack_from('<HH', b, p+6+shift)
                crc, cs, us = struct.unpack_from('<III', b, p+14+shift)
                nl, xl = struct.unpack_from('<HH', b, p+26+shift)
                if flags & EXCLUDED_FLAGS or method not in (0,8) or 0xffffffff in (cs,us) or (method == 0 and cs != us):
                    counts[kind+' | unsupported_or_inconsistent'] += 1
                    continue
                extra_comment = int.from_bytes(b[p+32:p+34], 'little') if kind == 'central' else 0
                r.update(flags=flags, method=method, crc=crc, csize=cs, usize=us,
                         header_size=size+nl+xl+extra_comment,
                         name_hex=b[p+size:p+size+nl].hex() if nl and p+size+nl <= len(b) else None)
                if kind == 'central':
                    disk = int.from_bytes(b[p+34:p+36], 'little')
                    local_offset = int.from_bytes(b[p+42:p+46], 'little')
                    if disk or local_offset == 0xffffffff:
                        counts['central | unsupported_offset'] += 1
                        continue
                    r['local_offset'] = local_offset
            records.append(r)
            counts[kind+' | eligible_fixed_header'] += 1
    return records, dict(counts)


def entry_key(r):
    return (r['name_hex'], r['flags'], r['method'], r['crc'], r['csize'], r['usize'])


def enumerate_queries(records):
    at = defaultdict(list)
    names = defaultdict(list)
    for r in records:
        at[r['kind'], r['offset']].append(r)
        if r['kind'] == 'local' and r['name_hex'] is not None:
            names[entry_key(r), r['offset']].append(r)
    for r in records:
        sid, p, kind = r['sample_id'], r['offset'], r['kind']
        q = dict(source_sample_id=sid, source_offset=p)
        if kind == 'local':
            distance, pos = divmod(p+r['header_size']+r['csize'], BLOCK_SIZE)
            if distance == 0:
                continue  # Not a cross-block hypothesis.
            candidates = at['local',pos] + at['central',pos]
            q.update(rule='zip_next', target_offset=pos, target_block_delta=distance,
                     evidence='length_to_header_position_only', assumption='entry data immediately follows local header; no descriptor/encryption/ZIP64')
        elif kind == 'central' and r['name_hex'] is not None:
            pos = r['local_offset'] % BLOCK_SIZE
            candidates = names[entry_key(r),pos]
            q.update(rule='cd_local', target_offset=pos, archive_target_offset=r['local_offset'],
                     evidence='filename_flags_method_crc_sizes_and_offset_modulo',
                     assumption='archive base at original file offset zero; complete matching filename, supported fields only')
        elif kind == 'eocd':
            # Under classic contiguous-directory layout, EOCD absolute offset is
            # cd_offset + cd_size. Check alignment before forming hypotheses.
            if (r['cd_offset']+r['cd_size']) % BLOCK_SIZE != p:
                continue
            pos = r['cd_offset'] % BLOCK_SIZE
            candidates = [t for t in at['central',pos] if t['header_size'] <= r['cd_size']]
            distance = r['cd_offset']//BLOCK_SIZE - (r['cd_offset']+r['cd_size'])//BLOCK_SIZE
            if distance == 0:
                continue
            q.update(rule='eocd', target_offset=pos, target_block_delta=distance,
                     archive_target_offset=r['cd_offset'], evidence='directory_offset_size_alignment_only',
                     assumption='archive base zero; classic unencrypted central directory immediately precedes EOCD; no intervening ZIP64 records')
        else:
            continue
        q['candidates'] = [dict(target_sample_id=t['sample_id'], target_offset=t['offset'], target_kind=t['kind'])
                           for t in candidates if t['sample_id'] != sid]
        yield q


def build(block_path, output):
    x = load_blocks(block_path)
    records, scanned = scan_headers(x)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    stats = defaultdict(Counter)
    unique_pairs = set()
    with (output/'queries.jsonl').open('w') as f:
        for q in enumerate_queries(records):
            n = len(q['candidates']); s = stats[q['rule']]
            s['queries'] += 1; s['candidate_relations'] += n
            s['no_candidate_queries'] += n == 0
            s['singleton_queries'] += n == 1
            s['ambiguous_queries'] += n > 1
            for t in q['candidates']:
                unique_pairs.add(tuple(sorted((q['source_sample_id'],t['target_sample_id']))))
            f.write(json.dumps(q)+'\n')
    result = dict(blocks=len(x), blocks_sha256=fingerprint(x), scanned=scanned,
                  by_rule=dict(stats), unique_unordered_block_pairs=len(unique_pairs),
                  truth_used=False, stage='bytes-only candidate generation',
                  input_assumption='512-byte blocks aligned to original file offset multiples of 512; row order is arbitrary',
                  note='All candidates retained, no top-1 choice or grouping weights. Modulo matches are weak. Even matching names/CRC can occur across files with shared resources. Missing relations never imply different files.')
    (output/'candidate_summary.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2)); print('후보 저장:', output)
    return result


def evaluate(block_path, truth_path, candidate_dir):
    """Read truth only after candidates have been saved. Never modify hypotheses."""
    x = load_blocks(block_path)
    candidate_dir = Path(candidate_dir)
    summary = json.loads((candidate_dir/'candidate_summary.json').read_text())
    if summary['blocks_sha256'] != fingerprint(x):
        raise ValueError('blocks differ from generation input')
    with open(truth_path, newline='') as f:
        truth = list(csv.DictReader(f))
    if len(truth) != len(x):
        raise ValueError('truth row count mismatch')
    lookup = {}
    for i,m in enumerate(truth):
        if int(m['sample_id']) != i or int(m['source_byte_offset']) < 0 or int(m['source_byte_offset'])%BLOCK_SIZE:
            raise ValueError('truth order/alignment mismatch')
        key = (m['source_file_id'], int(m['source_byte_offset']))
        if key in lookup:
            raise ValueError('duplicate truth position')
        lookup[key] = i
    stats = defaultdict(Counter)
    pairs = defaultdict(set)
    source_files = defaultdict(set)
    samples = defaultdict(list)
    with (candidate_dir/'queries.jsonl').open() as f:
        for line in f:
            q = json.loads(line); rule=q['rule']; s=stats[rule]
            sid=q['source_sample_id']; m=truth[sid]; pos=q['target_offset']
            address = int(m['source_byte_offset'])+q['target_block_delta']*BLOCK_SIZE+pos if rule=='zip_next' else q['archive_target_offset']
            tid = lookup.get((m['source_file_id'],address//BLOCK_SIZE*BLOCK_SIZE))
            expected = (b'PK\x03\x04',b'PK\x01\x02') if rule=='zip_next' else ((b'PK\x03\x04',) if rule=='cd_local' else (b'PK\x01\x02',))
            oracle_available = tid is not None and tid!=sid and x[tid,pos:pos+4].tobytes() in expected
            s['queries'] += 1
            s['oracle_target_signature_available_queries'] += oracle_available
            s['queries_with_exact_target'] += any(t['target_sample_id']==tid for t in q['candidates']) and oracle_available
            singleton = len(q['candidates'])==1
            s['singleton_queries'] += singleton
            for t in q['candidates']:
                target=t['target_sample_id']; tm=truth[target]
                same=m['source_file_id']==tm['source_file_id']
                exact=bool(oracle_available and target==tid)
                s['candidate_relations'] += 1; s['same_file_relations'] += same
                s['exact_target_relations'] += exact
                s['wrong_file_relations'] += not same
                s['same_file_wrong_target_relations'] += same and not exact
                s['singleton_exact_relations'] += singleton and exact
                s['singleton_wrong_file_relations'] += singleton and not same
                pairs[rule].add(tuple(sorted((sid,target))))
                if exact:source_files[rule].add(m['source_file_id'])
                if not exact and len(samples[rule])<5:
                    samples[rule].append(dict(source=sid,target=target,rule=rule,same_file=same,
                                             correct_target_sample_id=tid if oracle_available else None))
    result = {}
    for rule,s in stats.items():
        n=s['candidate_relations']; k=s['singleton_queries']; available=s['oracle_target_signature_available_queries']
        pp=pairs[rule]; true_pairs=sum(truth[a]['source_file_id']==truth[b]['source_file_id'] for a,b in pp)
        result[rule]=dict(s, exact_target_precision=s['exact_target_relations']/n if n else None,
                         same_file_precision=s['same_file_relations']/n if n else None,
                         singleton_exact_precision=s['singleton_exact_relations']/k if k else None,
                         available_target_query_recall=s['queries_with_exact_target']/available if available else None,
                         unique_unordered_pairs=len(pp),same_file_unique_pairs=true_pairs,
                         exact_relation_source_files=len(source_files[rule]))
    report=dict(by_rule=result, incorrect_examples=dict(samples),
                note='Pilot evaluation only; truth used after generation. Availability recall conditional on supported source queries and target signature presence, not whole-archive recall. Grouping not run; duplicate/shared resources may cause cross-file ambiguity.')
    out=candidate_dir/'evaluation.json'
    with out.open('x') as f:json.dump(report,f,indent=2)
    print(json.dumps(report,indent=2));print('평가 저장:',out)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    g=sub.add_parser('generate');g.add_argument('--blocks',required=True);g.add_argument('--output',required=True)
    e=sub.add_parser('evaluate');e.add_argument('--blocks',required=True);e.add_argument('--truth',required=True);e.add_argument('--candidates',required=True)
    a=p.parse_args()
    if a.command=='generate':build(a.blocks,a.output)
    else:evaluate(a.blocks,a.truth,a.candidates)


if __name__=='__main__':main()
