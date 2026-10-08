"""같은 원본 파일의 연속 블록이 데이터셋에 얼마나 있는지 집계. 블록 바이트는 읽지 않는다.

질문: 블록 쌍(A, A+1) 구조 검사를 기존 데이터만으로 할 수 있는가,
아니면 원본 파일에서 쌍 데이터셋을 새로 만들어야 하는가?
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path
from statistics import median


def block_index(row, block_size):
    if row.get('block_index_in_file', '') != '':
        return int(row['block_index_in_file'])
    if row.get('byte_offset', '') != '':
        offset = int(row['byte_offset'])
        if offset % block_size:
            raise ValueError(f"{row.get('block_id')}: byte_offset이 블록 크기의 배수가 아님")
        return offset // block_size
    raise ValueError('block_index_in_file 또는 byte_offset 컬럼 필요')


def read_meta(paths, block_size):
    """(split, file_id, format, index) 목록. split 이름은 파일명에서 _meta 앞부분."""
    rows = []
    for path in paths:
        split = Path(path).stem.removesuffix('_meta')
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            if not {'source_file_id', 'ground_truth_type'}.issubset(reader.fieldnames or []):
                raise ValueError(f'{path}: source_file_id, ground_truth_type 컬럼 필요')
            for row in reader:
                rows.append((split, row['source_file_id'], row['ground_truth_type'].lower(),
                             block_index(row, block_size)))
    return rows


def runs(indices):
    """정렬된 고유 인덱스의 연속 구간 길이 목록."""
    lengths, current = [], 1
    for a, b in zip(indices, indices[1:]):
        if b == a + 1:
            current += 1
        else:
            lengths.append(current)
            current = 1
    return lengths + [current] if indices else []


def stats(files):
    """files: {file_id: (format, [index...])} → 포맷별·전체 집계."""
    groups = defaultdict(Counter)
    per_file, longest = defaultdict(list), Counter()
    for fmt, indices in files.values():
        unique = sorted(set(indices))
        lengths = runs(unique)
        for key in (fmt, 'ALL'):
            g = groups[key]
            g['blocks'] += len(indices)
            g['duplicate_block_entries'] += len(indices) - len(unique)
            g['files'] += 1
            g['files_with_2plus_blocks'] += len(unique) >= 2
            g['files_with_adjacent_pair'] += any(n >= 2 for n in lengths)
            g['files_with_first_block'] += 0 in unique
            g['adjacent_pairs'] += sum(n - 1 for n in lengths)
            g['blocks_with_neighbor'] += sum(n for n in lengths if n >= 2)
            per_file[key].append(len(unique))
            longest[key] = max(longest[key], max(lengths))
    result = {}
    for key, g in sorted(groups.items(), key=lambda kv: (kv[0] != 'ALL', kv[0])):
        counts = per_file[key]
        result[key] = dict(g, blocks_per_file_mean=round(sum(counts) / len(counts), 2),
                           blocks_per_file_median=median(counts), blocks_per_file_max=max(counts),
                           longest_contiguous_run=longest[key],
                           blocks_with_neighbor_share=round(g['blocks_with_neighbor'] / g['blocks'], 4))
    return result


def analyze(rows):
    by_split = defaultdict(dict)
    combined, splits_of, formats_of = {}, defaultdict(set), defaultdict(set)
    for split, file_id, fmt, index in rows:
        by_split[split].setdefault(file_id, (fmt, []))[1].append(index)
        combined.setdefault(file_id, (fmt, []))[1].append(index)
        splits_of[file_id].add(split)
        formats_of[file_id].add(fmt)
    conflicts = sorted(f for f, s in formats_of.items() if len(s) > 1)
    if conflicts:
        raise ValueError(f'같은 source_file_id에 다른 정답 포맷: {conflicts[:5]}')
    shared = Counter(fmt for f, (fmt, _) in combined.items() if len(splits_of[f]) > 1)
    return dict(within_split={s: stats(files) for s, files in sorted(by_split.items())},
                all_splits_combined=stats(combined) if len(by_split) > 1 else None,
                files_in_multiple_splits=dict(shared, ALL=sum(shared.values())),
                note='같은 split 안의 연속 쌍만 실제 블록 쌍 실험에 바로 쓸 수 있다. '
                     'split 간 파일 공유가 0이 아니면 split이 파일 단위로 분리되지 않았다는 뜻이므로 별도 확인 필요.')


def report(summary, formats):
    lines = []
    for split, table in summary['within_split'].items():
        lines.append(f'[{split}] 포맷 | 블록 | 파일 | 파일당 블록(중앙값/최대) | 연속 쌍 | 이웃 있는 블록 비율 | 첫 블록 보유 파일')
        for fmt, s in table.items():
            if fmt == 'ALL' or not formats or fmt in formats:
                lines.append(f"  {fmt} | {s['blocks']:,} | {s['files']:,} | {s['blocks_per_file_median']}/"
                             f"{s['blocks_per_file_max']} | {s['adjacent_pairs']:,} | "
                             f"{s['blocks_with_neighbor_share']} | {s['files_with_first_block']:,}")
    lines.append(f"split 간 공유 파일: {summary['files_in_multiple_splits']}")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--meta', nargs='+', required=True, help='train_meta.csv val_meta.csv test_meta.csv 등')
    parser.add_argument('--block-size', type=int, default=512, help='byte_offset만 있을 때 인덱스 계산용')
    parser.add_argument('--show', nargs='*', default=['png', 'zip'], help='화면에 표시할 포맷. 빈 값이면 전체')
    parser.add_argument('--output', default=str(Path(__file__).resolve().parent / 'adjacency-output'
                                                / datetime.now().strftime('%Y%m%d-%H%M%S')))
    args = parser.parse_args()
    summary = analyze(read_meta(args.meta, args.block_size))
    summary['inputs'] = [str(Path(p).resolve()) for p in args.meta]
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'adjacency_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    text = report(summary, {f.lower() for f in args.show})
    (output / 'report.txt').write_text(text + '\n', encoding='utf-8')
    print(text)
    print(f'저장: {output}')


if __name__ == '__main__':
    main()
