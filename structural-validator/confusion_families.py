"""FFC 오답을 압축 방식 계열로 묶어 집계. 블록 바이트는 읽지 않는다.

질문: PNG/ZIP으로 잘못 예측된 블록의 실제 포맷이 같은 deflate 계열인가?
같은 계열이면 구조 단서가 없는 블록에서는 바이트 수준 구별 정보가 거의 없다.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path

# 계열 정의는 연구 가정이다. 데이터셋 이름과 다르면 --family-map JSON으로 덮어쓴다.
FAMILIES = {
    # 본문 대부분이 deflate(zlib) 스트림
    'deflate': ['png', 'zip', 'apk', 'jar', 'docx', 'pptx', 'xlsx', 'epub', 'gz', 'gzip',
                'key', 'odt', 'ods', 'odp', 'swf', 'xpi', 'kmz'],
    # deflate 비중이 크지만 다른 압축·평문 영역도 섞임
    'deflate_mixed': ['pdf', 'ai', 'dmg', 'pkg', 'rpm', 'deb', 'msi'],
    # deflate가 아닌 고엔트로피 압축 데이터
    'other_compressed': ['7z', 'xz', 'bz2', 'rar', 'jpg', 'jpeg', 'heic', 'gif', 'webp', 'djvu',
                         'mobi', 'mp4', 'mov', '3gp', 'avi', 'mkv', 'webm', 'ogv', 'mp3',
                         'm4a', 'ogg', 'flac', 'wma'],
    'image_raw': ['arw', 'cr2', 'dng', 'gpr', 'nef', 'nrw', 'orf', 'pef', 'raf', 'rw2', '3fr',
                  'tiff', 'tif', 'bmp', 'psd'],
    'text': ['md', 'rtf', 'txt', 'tex', 'json', 'html', 'xml', 'log', 'csv', 'eps'],
    'executable': ['exe', 'mach-o', 'macho', 'elf', 'dll'],
    'other': ['doc', 'ppt', 'xls', 'aiff', 'wav', 'pcap', 'ttf', 'dwg', 'sqlite'],
}
DEFLATE = ('deflate',)
DEFLATE_ANY = ('deflate', 'deflate_mixed')


def build_lookup(families):
    lookup = {}
    for family, names in families.items():
        for name in names:
            if name.lower() in lookup:
                raise ValueError(f'{name}: 두 계열에 중복 정의')
            lookup[name.lower()] = family
    return lookup


def share(part, whole):
    return round(part / whole, 4) if whole else None


def summarize(rows, families=FAMILIES, focus=('png', 'zip')):
    lookup = build_lookup(families)
    family = lambda name: lookup.get(name, 'unmapped')
    total, wrong = 0, 0
    pairs, unmapped = Counter(), Counter()
    as_pred = {f: defaultdict(Counter) for f in focus}   # f로 잘못 예측된 블록의 정답
    as_truth = {f: defaultdict(Counter) for f in focus}  # 정답 f인데 다르게 예측된 블록
    for truth, predicted in rows:
        truth, predicted = truth.lower(), predicted.lower()
        total += 1
        for name in (truth, predicted):
            if name not in lookup:
                unmapped[name] += 1
        if truth == predicted:
            continue
        wrong += 1
        pairs[(family(truth), family(predicted))] += 1
        if predicted in as_pred:
            as_pred[predicted]['type'][truth] += 1
            as_pred[predicted]['family'][family(truth)] += 1
        if truth in as_truth:
            as_truth[truth]['type'][predicted] += 1
            as_truth[truth]['family'][family(predicted)] += 1

    def view(groups):
        n = sum(groups['family'].values())
        return dict(wrong=n,
                    deflate_share=share(sum(groups['family'][k] for k in DEFLATE), n),
                    deflate_or_mixed_share=share(sum(groups['family'][k] for k in DEFLATE_ANY), n),
                    by_family=dict(groups['family'].most_common()),
                    top_types=dict(groups['type'].most_common(15)))

    intra = sum(v for (t, p), v in pairs.items() if t in DEFLATE_ANY and p in DEFLATE_ANY)
    return dict(total_rows=total, ffc_wrong=wrong, ffc_accuracy=share(total - wrong, total),
                all_errors_within_deflate_or_mixed=intra,
                all_errors_within_deflate_or_mixed_share=share(intra, wrong),
                wrong_predicted_as={f: view(g) for f, g in as_pred.items()},
                missed_truth={f: view(g) for f, g in as_truth.items()},
                family_pairs={f'{t} -> {p}': v for (t, p), v in pairs.most_common()},
                unmapped_types=dict(unmapped.most_common()),
                families=families,
                note='계열은 압축 방식 가정이다. 같은 계열 오답 비율은 구조 검증으로 '
                     '구별할 수 없다는 증명이 아니라, 단일 블록 바이트 정보가 적은 영역의 크기 추정이다.')


def read_rows(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not {'ground_truth_type', 'predicted_type'}.issubset(reader.fieldnames or []):
            raise ValueError('ground_truth_type, predicted_type 컬럼 필요')
        for row in reader:
            yield row['ground_truth_type'], row['predicted_type']


def report(summary):
    lines = [f"전체 {summary['total_rows']:,}행, FFC 오답 {summary['ffc_wrong']:,}개 "
             f"(정확도 {summary['ffc_accuracy']})",
             f"전체 오답 중 deflate(+mixed) 계열 내부 혼동: "
             f"{summary['all_errors_within_deflate_or_mixed']:,} "
             f"({summary['all_errors_within_deflate_or_mixed_share']})"]
    for title, key in (('잘못 예측된', 'wrong_predicted_as'), ('놓친 정답', 'missed_truth')):
        for fmt, v in summary[key].items():
            lines.append(f"[{title} {fmt}] 오답 {v['wrong']:,}개, deflate {v['deflate_share']}, "
                         f"deflate+mixed {v['deflate_or_mixed_share']}")
            lines.append('    계열: ' + ', '.join(f'{k}={n:,}' for k, n in v['by_family'].items()))
            lines.append('    포맷: ' + ', '.join(f'{k}={n:,}' for k, n in v['top_types'].items()))
    if summary['unmapped_types']:
        lines.append('경고: 계열 미지정 포맷 ' + ', '.join(summary['unmapped_types'])
                     + ' → --family-map으로 지정 권장')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--predictions', required=True, help='ground_truth_type, predicted_type 컬럼이 있는 CSV')
    parser.add_argument('--family-map', help='{"계열": ["포맷", ...]} JSON. 지정하면 기본 정의를 대체')
    parser.add_argument('--focus', nargs='+', default=['png', 'zip'])
    parser.add_argument('--output', default=str(Path(__file__).resolve().parent / 'family-output'
                                                / datetime.now().strftime('%Y%m%d-%H%M%S')))
    args = parser.parse_args()
    families = json.loads(Path(args.family_map).read_text(encoding='utf-8')) if args.family_map else FAMILIES
    summary = summarize(read_rows(args.predictions), families, tuple(f.lower() for f in args.focus))
    summary['input'] = str(Path(args.predictions).resolve())
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'family_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    text = report(summary)
    (output / 'report.txt').write_text(text + '\n', encoding='utf-8')
    print(text)
    print(f'저장: {output}')


if __name__ == '__main__':
    main()
