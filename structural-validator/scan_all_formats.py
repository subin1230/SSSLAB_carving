"""Label-independent PNG/ZIP scanning; labels are used only after detection."""
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path

FORMATS = ('png', 'zip')


def detect(data, check):
    results = {fmt: check(data, fmt) for fmt in FORMATS}
    candidates = [fmt for fmt in FORMATS if any(r.get('offset') is not None for r in results[fmt])]
    # Only positive evidence survives boundary qualification. Raw VALID is separate.
    supported = [fmt for fmt in FORMATS if any(r.get('evidence_status') == 'VALID' for r in results[fmt])]
    return results, candidates, supported


def category(formats):
    return '+'.join(formats) if formats else 'none'


def scan(x, selected, check, output, version, total, limit, npz, meta, predictions):
    counts, candidate_groups, evidence_groups = Counter(), Counter(), Counter()
    rules, qualified = Counter(), Counter()
    labels = defaultdict(Counter)
    fields = ['row', 'block_id', 'candidate_formats', 'supported_formats',
              'ground_truth_type', 'predicted_type', 'ffc_correct', 'alternative_supported_formats']
    with (output/'blocks.csv').open('w', newline='', encoding='utf-8-sig') as bf, (output/'evidence.jsonl').open('w', encoding='utf-8') as ef:
        writer = csv.DictWriter(bf, fieldnames=fields)
        writer.writeheader()
        for n, (i, metadata, prediction) in enumerate(selected, 1):
            results, candidates, supported = detect(x[i].tobytes(), check)
            # Neither metadata nor prediction is passed to detect().
            truth = metadata['ground_truth_type'].lower()
            predicted = prediction['predicted_type'].lower()
            alternatives = [fmt for fmt in supported if fmt != predicted]
            candidate_groups[category(candidates)] += 1
            evidence_groups[category(supported)] += 1
            counts['inspected'] += 1
            counts['candidate_blocks'] += bool(candidates)
            counts['supported_blocks'] += bool(supported)
            counts['ffc_wrong'] += predicted != truth
            counts['ffc_wrong_with_alternative_support'] += predicted != truth and bool(alternatives)
            counts['ffc_wrong_with_truth_support'] += predicted != truth and truth in supported
            counts['ffc_correct_with_alternative_support'] += predicted == truth and bool(alternatives)
            group = labels[truth]
            group['inspected'] += 1
            for fmt in FORMATS:
                group[fmt+'_candidate_blocks'] += fmt in candidates
                group[fmt+'_supported_blocks'] += fmt in supported
                for r in results[fmt]:
                    rules[f"{fmt} | {r['rule']} | {r['status']}"] += 1
                    qualified[f"{fmt} | {r['rule']} | {r.get('evidence_status', 'unavailable')}"] += 1
            record = dict(row=i, block_id=metadata['block_id'], candidate_formats=category(candidates),
                          supported_formats=category(supported), ground_truth_type=truth,
                          predicted_type=predicted, ffc_correct=truth == predicted,
                          alternative_supported_formats=category(alternatives))
            writer.writerow(record)
            # Keep details only for candidate-bearing blocks; all blocks remain in CSV.
            if candidates:
                ef.write(json.dumps(dict(record, results=results, metadata=metadata,
                                         rejection_applied=False), ensure_ascii=False)+'\n')
            if n % 10000 == 0:
                print(f'PNG/ZIP 독립 탐색 {n:,}/{len(selected):,}블록', flush=True)
    summary = dict(selection_mode='all-formats', validator_version=version,
                   uses_ground_truth_for_validation=False, uses_prediction_for_validation=False,
                   formats=list(FORMATS), total_rows=total, block_size=int(x.shape[1]), limit=limit,
                   inputs={'npz': str(Path(npz).resolve()), 'metadata': str(Path(meta).resolve()),
                           'predictions': str(Path(predictions).resolve())},
                   inspected_counts=dict(counts), candidate_categories=dict(candidate_groups),
                   supported_categories=dict(evidence_groups), per_truth_label=dict(labels),
                   rule_counts=dict(rules), evidence_counts=dict(qualified),
                   detail_selection='candidate-bearing blocks only; blocks.csv includes every inspected block',
                   selection='first rows; smoke test, not representative' if limit else 'all rows',
                   rejection_applied=False, reclassification_applied=False,
                   note='Supported means at least one boundary-qualified VALID rule, not source format classification. Alternative support does not disprove the FFC label. Rule counts are not block counts.')
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'전체 포맷 탐색 {len(selected):,}블록 완료: {output}', flush=True)
    return summary
