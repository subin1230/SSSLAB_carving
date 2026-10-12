"""Diagnose reference embedding differences without relaxing the scoring gate."""
import argparse
import json
from pathlib import Path

import numpy as np

from score_samefile_pilot import (
    HIDDEN_LAYER, REFERENCE_ATOL, REFERENCE_RTOL, extract_embeddings,
    load_models_cpu, pair_batches, pair_features, reference_sample, sigmoid,
)
from zip_pair_payload import file_hash


def difference_summary(actual, expected):
    a, b = np.asarray(actual, dtype=np.float64), np.asarray(expected, dtype=np.float64)
    if a.shape != b.shape or not a.size or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError('comparison requires matching nonempty finite arrays')
    delta = np.abs(a-b)
    return dict(max_absolute_difference=float(delta.max()),
                mean_absolute_difference=float(delta.mean()),
                root_mean_square_difference=float(np.sqrt(np.mean(delta**2))),
                absolute_difference_percentiles=dict(zip(('50', '95', '99'),
                    np.percentile(delta, (50, 95, 99)).tolist())))


def embedding_report(actual, reference, rows):
    a, b = np.asarray(actual, dtype=np.float64), np.asarray(reference, dtype=np.float64)
    report = difference_summary(a, b)
    if a.ndim != 2 or len(rows) != len(a):
        raise ValueError('embedding rows do not match')
    delta = np.abs(a-b)
    close = np.isclose(actual, reference, atol=REFERENCE_ATOL, rtol=REFERENCE_RTOL)
    # Show failures by violation of the tolerance, not merely by largest value.
    tolerance = REFERENCE_ATOL + REFERENCE_RTOL*np.abs(b)
    ratio = delta/tolerance
    worst = np.argsort(ratio.ravel())[-min(8, a.size):][::-1]
    report.update(
        strict_reference_matched=bool(close.all()), atol=REFERENCE_ATOL, rtol=REFERENCE_RTOL,
        compared_elements=int(a.size), failing_elements=int((~close).sum()),
        compared_rows=len(a), failing_rows=int((~close).any(axis=1).sum()),
        reference_range=[float(b.min()), float(b.max())],
        reference_max_absolute_value=float(np.abs(b).max()),
        actual_range=[float(a.min()), float(a.max())],
        worst_coordinates=[], per_row=[],
    )
    for flat in worst:
        i, j = np.unravel_index(flat, a.shape)
        report['worst_coordinates'].append(dict(
            reference_row=int(rows[i]), component=int(j), actual=float(a[i, j]),
            reference=float(b[i, j]), absolute_difference=float(delta[i, j]),
            allowed_difference=float(tolerance[i, j]), within_tolerance=bool(close[i, j]),
        ))
    for i, row in enumerate(rows):
        na, nb = float(np.linalg.norm(a[i])), float(np.linalg.norm(b[i]))
        error = float(np.linalg.norm(a[i]-b[i]))
        report['per_row'].append(dict(
            reference_row=int(row), actual_l2=na, reference_l2=nb,
            relative_l2_error=error/nb if nb else None,
            cosine_similarity=float(np.clip(np.dot(a[i], b[i])/(na*nb), -1, 1)) if na and nb else None,
            failing_elements=int((~close[i]).sum()),
        ))
    return report


def pair_score_report(actual, reference, predict):
    """Same frozen head/runtime on both embeddings; no labels or pilot scores."""
    if len(reference) < 2:
        return dict(pairs=0, note='at least two reference rows required')
    ids = next(pair_batches(len(reference), len(reference)**2))
    def logits(h):
        z = np.asarray(predict(pair_features(h[ids[:, 0]], h[ids[:, 1]])))
        if z.shape != (len(ids), 1):
            raise ValueError('expected scalar logit per reference pair')
        sigmoid(z)  # Validate finiteness even before computing a difference.
        return z[:, 0]
    old, new = logits(reference), logits(actual)
    old_score, new_score = sigmoid(old), sigmoid(new)
    return dict(pairs=len(ids), logits=difference_summary(new, old),
                scores=difference_summary(new_score, old_score),
                diagnostic_crossings_at_score_0_5=int(np.count_nonzero((old_score >= .5) != (new_score >= .5))),
                note='Both use the same current MLP runtime. 0.5 is a diagnostic boundary, not a grouping threshold. These unlabelled reference pairs do not measure accuracy or calibrate an acceptance tolerance.')


def diagnose_samples(batch, reference, rows, encode, predict_pairs):
    actual = extract_embeddings(batch, encode, len(batch))
    repeat = extract_embeddings(batch, encode, len(batch))
    one_at_a_time = extract_embeddings(batch, encode, 1)
    report = dict(
        reference_comparison=embedding_report(actual, reference, rows),
        repeat_inference=difference_summary(repeat, actual),
        batch_one_vs_sample_batch=difference_summary(one_at_a_time, actual),
        batch_one_reference_comparison=embedding_report(one_at_a_time, reference, rows),
        pair_score_effect=pair_score_report(actual, reference, predict_pairs),
    )
    return report, actual, one_at_a_time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('encoder-model', 'pair-model', 'reference-blocks', 'reference-embeddings', 'output'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--reference-manifest')
    p.add_argument('--threads', type=int, default=2)
    a = p.parse_args()
    output = Path(a.output)
    if output.exists():
        raise FileExistsError('output exists; choose a new diagnostic directory')
    batch, reference, metadata = reference_sample(a.reference_blocks, a.reference_embeddings)
    original_manifest = None
    if a.reference_manifest:
        path = Path(a.reference_manifest)
        original_manifest = dict(path=str(path), exists=path.is_file())
        if path.is_file():
            if path.stat().st_size > 1024*1024:
                raise ValueError('reference manifest unexpectedly exceeds 1 MiB')
            original_manifest.update(sha256=file_hash(path), content=json.loads(path.read_text()))
    encoder, head, provenance = load_models_cpu(a)
    print(f'[기존 임베딩 {len(batch)}행 진단 — 전체 점수 생성 아님]', flush=True)
    report, actual, singles = diagnose_samples(
        batch, reference, metadata['rows'], encoder.predict_on_batch, head.predict_on_batch)
    report.update(provenance=provenance, reference_sample=metadata,
                  reference_manifest=original_manifest, hidden_layer=HIDDEN_LAYER,
                  tolerance_changed=False, pilot_scoring_started=False,
                  note='Diagnostic completion does not approve inference. Inspect preprocessing, model provenance, runtime and downstream score differences before deciding how to proceed.')
    output.mkdir(parents=True, exist_ok=False)
    np.savez(output/'embedding_samples.npz', reference_rows=np.asarray(metadata['rows']),
             stored=reference, actual=actual, batch_one=singles)
    (output/'diagnosis.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    for key in ('provenance', 'reference_comparison', 'repeat_inference',
                'batch_one_vs_sample_batch', 'pair_score_effect'):
        content = dict(report[key])
        content.pop('per_row', None)
        print(f'\n[{key}]')
        print(json.dumps(content, ensure_ascii=False, indent=2))
    print('\n진단 저장:', output/'diagnosis.json')
    print('진단만 완료. 허용오차 변경·파일럿 점수 생성은 수행하지 않음.')


if __name__ == '__main__':
    main()
