"""FFC top-1 PNG/ZIP 후보의 구조 증거 수집. 후보를 자동 기각하지 않는다."""
import argparse
from collections import Counter
import csv
from datetime import datetime
from itertools import zip_longest
import json
from pathlib import Path

import numpy as np
from validator import validate_block
from validator_v4 import validate_block as validate_v4
from validator_v2 import validate_block as validate_v2
from validator_v1 import validate_block as validate_v1


def collect(npz_path, meta_path, predictions_path, output, limit=100, version="v3", mode="prediction"):
    if mode not in ("prediction", "ground-truth"):
        raise ValueError("알 수 없는 선택 모드")
    check = {"v1": validate_v1, "v2": validate_v2, "v3": validate_block, "v4": validate_v4}[version]
    if limit < 0:
        raise ValueError('limit은 0(전체) 또는 양수여야 합니다')
    with np.load(npz_path, allow_pickle=False) as archive:
        x, y = archive['x'], archive['y']
    if x.dtype != np.uint8 or x.ndim != 2 or x.shape[1] not in (512, 4096):
        raise ValueError('x는 (N, 512) 또는 (N, 4096)의 uint8 배열이어야 합니다. 자동 변환하지 않습니다.')
    if y.ndim != 1 or len(y) != len(x) or y.dtype.kind not in 'iu':
        raise ValueError('y의 형태·길이·정수 자료형 확인 실패')

    required_meta = {'row', 'block_id', 'source_file_id', 'ground_truth_type', 'label'}
    required_pred = {'block_id', 'source_file_id', 'ground_truth_type', 'predicted_type'}
    ids, selected = set(), []
    candidates, confusion = Counter(), Counter()
    label_types = {}
    count = 0
    with open(meta_path, newline='', encoding='utf-8-sig') as mf, open(predictions_path, newline='', encoding='utf-8-sig') as pf:
        metas, preds = csv.DictReader(mf), csv.DictReader(pf)
        if not required_meta.issubset(metas.fieldnames or []):
            raise ValueError('메타데이터 필수 컬럼 부족')
        if not required_pred.issubset(preds.fieldnames or []):
            raise ValueError('예측 파일 필수 컬럼 부족')
        for i, (meta, pred) in enumerate(zip_longest(metas, preds)):
            if meta is None or pred is None or i >= len(x):
                raise ValueError('NPZ/메타데이터/예측의 행 수 불일치')
            # 이 버전은 순서가 같은 export만 허용한다. 불일치 시 추측해서 연결하지 않는다.
            if int(meta['row']) != i or int(meta['label']) != int(y[i]):
                raise ValueError(f'{i}행: NPZ 라벨 또는 메타데이터 row 불일치')
            for key in ('block_id', 'source_file_id', 'ground_truth_type'):
                if meta[key] != pred[key]:
                    raise ValueError(f'{i}행: {key} 불일치. 데이터셋 또는 순서 확인 필요')
            for key in ('file_code', 'block_index_in_file', 'byte_offset', 'data_representation'):
                if key in meta and key in pred and meta[key] != pred[key]:
                    raise ValueError(f'{i}행: {key} 불일치')
            block_id = meta['block_id']
            if not block_id or block_id in ids:
                raise ValueError(f'{i}행: 빈 ID 또는 중복 ID')
            ids.add(block_id)
            truth = meta['ground_truth_type'].lower()
            label = int(y[i])
            if label in label_types and label_types[label] != truth:
                raise ValueError(f'{i}행: 같은 라벨에 다른 포맷 이름')
            label_types[label] = truth
            predicted = pred['predicted_type'].lower()
            fmt = truth if mode == 'ground-truth' else predicted
            if fmt in ('png', 'zip'):
                candidates[fmt] += 1
                confusion[f'{truth} -> {predicted}'] += 1
                if limit == 0 or len(selected) < limit:
                    selected.append((i, meta, pred))
            count += 1
    if count != len(x):
        raise ValueError('NPZ와 CSV 행 수 불일치')
    print(f'전체 {count:,}행 대응 검사 통과. 모드={mode}, PNG/ZIP 선택 블록: {dict(candidates)}', flush=True)

    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    counts = Counter()
    rule_counts = Counter()
    boundary_counts = Counter()
    evidence_counts = Counter()
    per_format = {}
    with (output / 'evidence.jsonl').open('w', encoding='utf-8') as detail, (output / 'blocks.csv').open('w', newline='', encoding='utf-8-sig') as bf:
        columns = ['row', 'block_id', 'selection_mode', 'validation_format', 'predicted_type', 'ground_truth_type', 'ffc_correct',
                   'has_invalid_rule', 'has_valid_rule', 'has_unknown_rule', 'rule_count']
        writer = csv.DictWriter(bf, fieldnames=columns)
        writer.writeheader()
        for i, meta, pred in selected:
            predicted, truth = pred['predicted_type'].lower(), meta['ground_truth_type'].lower()
            fmt = truth if mode == 'ground-truth' else predicted
            results = check(x[i].tobytes(), fmt)  # 정답 포맷 사용은 진단 모드에 한정
            statuses = {r['status'] for r in results}
            for result in results:
                rule_counts[f"{fmt} | {result['rule']} | {result['status']}"] += 1
            if version in ('v3', 'v4'):
                evidence_states = {r['evidence_status'] for r in results}
                for r in results:
                    boundary_counts[f"{fmt} | {r['boundary_evidence']}"] += 1
                    evidence_counts[f"{fmt} | {r['rule']} | {r['evidence_status']}"] += 1
                counts['boundary_supported_invalid_blocks'] += int('INVALID' in evidence_states)
                counts['unanchored_invalid_blocks'] += int(any(r['status']=='INVALID' and r['evidence_status']=='UNKNOWN' for r in results))
                counts['evidence_unknown_only_blocks'] += int(evidence_states == {'UNKNOWN'})
            group = per_format.setdefault(fmt, Counter())
            group['inspected'] += 1
            group['ffc_correct' if predicted == truth else 'ffc_wrong'] += 1
            group['has_invalid_rule'] += int('INVALID' in statuses)
            group['has_valid_rule'] += int('VALID' in statuses)
            group['no_structure_candidate'] += int(all(r['rule'] == 'STRUCTURE_EVIDENCE' for r in results))
            group['unknown_only'] += int(statuses == {'UNKNOWN'})
            record = dict(row=i, block_id=meta['block_id'], selection_mode=mode, validation_format=fmt, predicted_type=predicted,
                          ground_truth_type=truth, ffc_correct=predicted == truth,
                          has_invalid_rule='INVALID' in statuses, has_valid_rule='VALID' in statuses,
                          has_unknown_rule='UNKNOWN' in statuses, rule_count=len(results))
            writer.writerow(record)
            detail.write(json.dumps(dict(record, metadata=meta, prediction=pred,
                                         results=results, rejection_applied=False), ensure_ascii=False) + '\n')
            counts['inspected'] += 1
            counts['ffc_correct' if predicted == truth else 'ffc_wrong'] += 1
            if 'INVALID' in statuses:
                counts['invalid_evidence_on_correct' if predicted == truth else 'invalid_evidence_on_wrong'] += 1
            if statuses == {'UNKNOWN'}:
                counts['unknown_only'] += 1
    summary = dict(inputs={k: str(Path(v).resolve()) for k, v in
                           [('npz', npz_path), ('metadata', meta_path), ('predictions', predictions_path)]},
                   selection_mode=mode, uses_ground_truth_for_validation=(mode == "ground-truth"), validator_version=version, total_rows=count, block_size=x.shape[1], prediction_column='predicted_type',
                   candidates=dict(candidates), candidate_confusion=dict(confusion),
                   inspected_counts=dict(counts), rule_counts=dict(rule_counts), boundary_counts=dict(boundary_counts), evidence_counts=dict(evidence_counts), per_format=per_format, limit=limit,
                   selection='first matching rows; smoke test, not representative' if limit else 'all matching rows',
                   rejection_applied=False,
                   note='INVALID는 구조 후보의 규칙 위반 증거이며 블록 또는 FFC 후보 기각이 아님.')
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'검사 {len(selected):,}개 완료: {output}', flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--npz', required=True)
    parser.add_argument('--meta', required=True)
    parser.add_argument('--predictions', required=True)
    parser.add_argument('--mode', choices=['prediction', 'ground-truth'], default='prediction', help='ground-truth는 정답 기반 진단 전용')
    parser.add_argument('--validator-version', choices=['v1', 'v2', 'v3', 'v4'], default='v3')
    parser.add_argument('--limit', type=int, default=100, help='0이면 모든 PNG/ZIP 예측 후보 검사')
    parser.add_argument('--output', default=str(Path(__file__).resolve().parent / 'ffc-output' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    args = parser.parse_args()
    collect(args.npz, args.meta, args.predictions, args.output, args.limit, args.validator_version, args.mode)


if __name__ == '__main__':
    main()
