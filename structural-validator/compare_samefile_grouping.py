"""Paired, exploratory Leiden experiment on frozen Same-File and ZIP evidence.

Graph construction and clustering take no truth labels. Truth is used for
evaluation and, optionally, a separately reclustered model-overlap cohort.
"""
import argparse
from collections import Counter
import csv
import gzip
import importlib.metadata
import json
import math
from pathlib import Path
import sys

import numpy as np

from audit_zip_payload import read_truth
from zip_pair_candidates import fingerprint, load_blocks
from zip_pair_payload import file_hash, payload_check

VARIANTS = ('samefile', 'samefile_zip')
METRICS = ('ari', 'nmi', 'over_merge_rate', 'over_split_rate',
           'over_merge_pairs', 'over_split_pairs', 'mixed_clusters', 'split_files')


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + '\n')


def environment():
    versions = {}
    for name in ('numpy', 'scipy', 'scikit-learn', 'igraph', 'leidenalg'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return dict(python=sys.executable, python_version=sys.version.split()[0],
                packages=versions, ready=all(versions.values()), gpu_required=False)


def load_scores(x, run):
    """Require a complete, hashed, all-pairs run for these exact block rows."""
    run = Path(run)
    manifest = json.loads((run / 'score_manifest.json').read_text())
    state = json.loads((run / 'run_state.json').read_text())
    n = len(x)
    if not 2 <= n <= 10000:
        raise ValueError('dense pilot comparison supports 2..10000 blocks')
    total = n * (n - 1) // 2
    if state.get('status') != 'complete' or state.get('pairs') != total:
        raise ValueError('Same-File run is incomplete')
    if manifest['blocks'] != n or manifest['blocks_sha256'] != fingerprint(x):
        raise ValueError('Same-File blocks/order mismatch')
    if manifest['pair_count'] != total or manifest.get('truth_used') is not False:
        raise ValueError('requires complete truth-free scores')
    if manifest.get('zip_evidence_used') is not False:
        raise ValueError('baseline scores must not already include ZIP evidence')
    path = run / 'samefile_scores.csv.gz'
    if file_hash(path) != manifest['scores_sha256']:
        raise ValueError('Same-File score hash mismatch')
    scores = np.full((n, n), np.nan, dtype=np.float64)
    np.fill_diagonal(scores, 0.)
    count = 0
    with gzip.open(path, 'rt', newline='') as f:
        for row in csv.DictReader(f):
            i, j = int(row['sample_id_1']), int(row['sample_id_2'])
            s, z = float(row['same_file_score']), float(row['same_file_logit'])
            if not 0 <= i < j < n or not np.isnan(scores[i, j]):
                raise ValueError('duplicate or invalid score pair')
            if not math.isfinite(z) or not math.isfinite(s) or not 0 <= s <= 1:
                raise ValueError('invalid score/logit')
            expected = 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))
            if not math.isclose(s, expected, abs_tol=2e-7, rel_tol=2e-7):
                raise ValueError('score is not sigmoid(logit)')
            scores[i, j] = scores[j, i] = s
            count += 1
    if count != total or not np.isfinite(scores).all():
        raise ValueError('missing Same-File pairs')
    return scores, dict(run=str(run), manifest_sha256=file_hash(run / 'score_manifest.json'),
                       scores_sha256=manifest['scores_sha256'], pairs=count,
                       score_provenance=manifest['provenance'])


def load_zip_pairs(x, run):
    """Replay bytes-only checks; never read truth-based audit/evaluation files."""
    run = Path(run)
    summary = json.loads((run / 'payload_summary.json').read_text())
    if summary['blocks_sha256'] != fingerprint(x) or summary.get('truth_used') is not False:
        raise ValueError('ZIP blocks/order or truth-free provenance mismatch')
    if summary['checker_version'] != 'zip-pair-payload-v1':
        raise ValueError('unsupported payload checker')
    queries = run / 'baseline/queries.jsonl'
    if file_hash(queries) != summary['variant_queries_sha256']['baseline']:
        raise ValueError('ZIP baseline queries hash mismatch')
    evidence_path = run / 'payload_evidence.jsonl'
    evidence = {}
    with evidence_path.open() as f:
        for line in f:
            r = json.loads(line)
            key = (r['query_id'], r['target_sample_id'], r['target_offset'])
            if key in evidence:
                raise ValueError('duplicate ZIP evidence')
            evidence[key] = r
    passed, counts = set(), Counter()
    with queries.open() as f:
        for qid, line in enumerate(f):
            q = json.loads(line)
            sid = q['source_sample_id']
            if type(sid) is not int or not 0 <= sid < len(x):
                raise ValueError('invalid ZIP source ID')
            for t in q['candidates']:
                tid = t['target_sample_id']
                if type(tid) is not int or not 0 <= tid < len(x) or tid == sid:
                    raise ValueError('invalid ZIP target ID')
                key = (qid, tid, t['target_offset'])
                r = payload_check(x[sid].tobytes(), x[tid].tobytes(), q, t,
                                  summary['max_output_bytes'])
                expected = dict(query_id=qid, source_sample_id=sid,
                                source_offset=q['source_offset'], target_sample_id=tid,
                                target_offset=t['target_offset'], rule=q['rule'], check=r)
                if evidence.pop(key, None) != expected:
                    raise ValueError('ZIP evidence does not reproduce from block bytes')
                counts[r['status']] += 1
                if r['status'] == 'PASS':
                    passed.add(tuple(sorted((sid, tid))))
    if evidence:
        raise ValueError('extra ZIP evidence without a candidate')
    return passed, dict(run=str(run), payload_summary_sha256=file_hash(run / 'payload_summary.json'),
                        baseline_queries_sha256=file_hash(queries),
                        evidence_sha256=file_hash(evidence_path), replay_counts=dict(counts),
                        unique_pass_pairs=len(passed), duplicate_pass_relations=counts['PASS']-len(passed),
                        negative_edges_used=False, truth_used=False)


def build_graphs(scores, passed, k, threshold, alpha):
    """Symmetric union kNN baseline; add/boost each unique PASS pair once."""
    n = len(scores)
    if k < 1 or not 0 <= threshold <= 1 or not 0 <= alpha <= 1:
        raise ValueError('invalid graph parameters')
    baseline = {}
    for i in range(n):
        js = np.flatnonzero((scores[i] >= threshold) & (scores[i] > 0))
        js = js[js != i]
        # Stable secondary key for exact ties, independent of true source order.
        js = js[np.lexsort((js, -scores[i, js]))[:k]]
        for j in js:
            baseline[tuple(sorted((i, int(j))))] = float(scores[i, j])
    augmented = dict(baseline)
    if alpha:  # alpha=0 is an exact no-op, including edge selection.
        for i, j in sorted(passed):
            s = float(scores[i, j])
            augmented[i, j] = s + alpha * (1 - s)
    return dict(samefile=baseline, samefile_zip=augmented)


def cluster(ids, edges, seed, resolution):
    """Labels/offsets are deliberately absent from this interface."""
    import igraph as ig
    import leidenalg as la
    index = {int(sid): i for i, sid in enumerate(ids)}
    retained = [(index[i], index[j], w) for (i, j), w in sorted(edges.items())
                if i in index and j in index]
    g = ig.Graph(n=len(ids), edges=[(i, j) for i, j, _ in retained], directed=False)
    weights = [w for _, _, w in retained]
    if not retained:
        labels = np.arange(len(ids), dtype=np.int64)
    else:
        p = la.find_partition(g, la.RBConfigurationVertexPartition, weights=weights,
                              resolution_parameter=resolution, seed=seed, n_iterations=-1)
        labels = np.asarray(p.membership, dtype=np.int64)
        # Keep every isolate as its own cluster regardless of library tie handling.
        next_label = int(labels.max()) + 1
        for v, degree in enumerate(g.degree()):
            if degree == 0:
                labels[v] = next_label
                next_label += 1
    return labels, dict(nodes=len(ids), edges=len(retained),
                        isolated_nodes=sum(d == 0 for d in g.degree()),
                        components=len(g.connected_components()), total_weight=sum(weights))


def pair_counts(truth, predicted):
    def pairs(values):
        return sum(c * (c - 1) // 2 for c in Counter(values).values())
    n = len(truth)
    tp = pairs(zip(truth, predicted))
    actual = pairs(truth)
    inferred = pairs(predicted)
    return dict(tp=tp, fp=inferred-tp, fn=actual-tp,
                tn=n*(n-1)//2-actual-inferred+tp)


def pair_metrics(counts):
    tp, fp, fn = (counts[k] for k in ('tp', 'fp', 'fn'))
    return dict(pair_counts=counts, over_merge_pairs=fp, over_split_pairs=fn,
                # Pairwise impurity among merged pairs, and missed true pairs.
                over_merge_rate=fp/(tp+fp) if tp+fp else None,
                over_split_rate=fn/(tp+fn) if tp+fn else None)


def metrics(truth, predicted):
    from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score
    if not len(truth):
        return dict(blocks=0, files=0, clusters=0, ari=None, nmi=None,
                    mixed_clusters=0, split_files=0, **pair_metrics(dict(tp=0, fp=0, fn=0, tn=0)))
    return dict(blocks=len(truth), files=len(set(truth)), clusters=len(set(predicted)),
                ari=float(adjusted_rand_score(truth, predicted)),
                nmi=float(normalized_mutual_info_score(truth, predicted, average_method='arithmetic')),
                mixed_clusters=sum(len(set(truth[predicted == c])) > 1 for c in set(predicted)),
                split_files=sum(len(set(predicted[truth == f])) > 1 for f in set(truth)),
                **pair_metrics(pair_counts(truth, predicted)))


def evaluate_memberships(truth, labels, touched):
    all_counts = pair_counts(truth, labels)
    untouched_counts = pair_counts(truth[~touched], labels[~touched])
    return dict(all_blocks=metrics(truth, labels),
                zip_supported_blocks=metrics(truth[touched], labels[touched]),
                # Includes pairs from a supported block to an unsupported one;
                # a subset-only metric would miss such wrong merges.
                pairs_touching_zip_blocks=pair_metrics({k: all_counts[k]-untouched_counts[k]
                                                       for k in all_counts}))


def unseen_ids(truth, truth_path, x, overlap):
    """Predetermined evaluation-cohort selection, never graph edge generation."""
    overlap = Path(overlap)
    s = json.loads((overlap / 'overlap_summary.json').read_text())
    if s['pilot_blocks_sha256'] != fingerprint(x) or s['truth_sha256'] != file_hash(truth_path):
        raise ValueError('overlap audit does not match pilot/truth')
    if any(s['identity_diagnostics'].values()):
        raise ValueError('overlap audit has unresolved identity anomalies')
    with (overlap / 'pilot_source_overlap.csv').open(newline='') as f:
        rows = list(csv.DictReader(f))
    observed = {r['source_file_id']: r for r in rows}
    if len(observed) != len(rows) or set(observed) != {r['source_file_id'] for r in truth}:
        raise ValueError('overlap file coverage mismatch')
    fields = ('train_model_sources', 'val_model_sources', 'test_model_sources')
    for split, field in zip(('train', 'val', 'test'), fields):
        if sum(bool(r[field]) for r in rows) != s['overlap_files'][split]:
            raise ValueError('overlap split counts mismatch')
    absent = {u for u, r in observed.items() if not any(r[k] for k in fields)}
    if len(absent) != s['not_observed_in_model_metadata']:
        raise ValueError('overlap not-observed count mismatch')
    for r in truth:
        if observed[r['source_file_id']]['format'] != r['format']:
            raise ValueError('overlap format mismatch')
    return np.asarray([i for i, r in enumerate(truth) if r['source_file_id'] in absent], dtype=np.int64)


def summarize(records):
    """Report every seed, paired deltas and their spread; never select a winner."""
    summary = {}
    for cohort in sorted({r['cohort'] for r in records}):
        rows = [r for r in records if r['cohort'] == cohort]
        out = {}
        for scope in ('all_blocks', 'zip_supported_blocks', 'pairs_touching_zip_blocks'):
            section = {}
            for key in METRICS:
                if key not in rows[0]['evaluation'][scope]:
                    continue
                stats = {}
                for variant in VARIANTS:
                    vals = [r['evaluation'][scope][key] for r in rows if r['variant'] == variant]
                    stats[variant] = aggregate(vals)
                before = {r['seed']: r['evaluation'][scope][key] for r in rows if r['variant'] == VARIANTS[0]}
                after = {r['seed']: r['evaluation'][scope][key] for r in rows if r['variant'] == VARIANTS[1]}
                stats['paired_delta_zip_minus_samefile'] = aggregate([
                    after[s]-before[s] if after[s] is not None and before[s] is not None else None
                    for s in before])
                section[key] = stats
            out[scope] = section
        summary[cohort] = out
    return summary


def aggregate(values):
    finite = [v for v in values if v is not None]
    return dict(n_defined=len(finite), n_total=len(values),
                mean=float(np.mean(finite)) if finite else None,
                std=float(np.std(finite, ddof=1)) if len(finite) > 1 else None,
                min=min(finite) if finite else None, max=max(finite) if finite else None)


def write_report(path, result):
    lines = ['# Same-File + ZIP Leiden 파일럿 비교', '',
             '같은 점수·노드·seed·Leiden 설정을 사용함. ZIP PASS 쌍만 추가/보강함.',
             '아래 값은 seed별 결과의 평균임. 차이는 ZIP 보완 − Same-File임.', '',
             '| 대상 | 지표 | Same-File | + ZIP | 차이 |',
             '|---|---|---:|---:|---:|']
    def number(v):
        return '—' if v is None else f'{v:.6f}'
    for cohort, s in result['aggregate'].items():
        for key in ('ari', 'nmi', 'over_merge_rate', 'over_split_rate'):
            r = s['all_blocks'][key]
            vals = [r[v]['mean'] for v in (*VARIANTS, 'paired_delta_zip_minus_samefile')]
            lines.append(f'| {cohort} | {key} | ' + ' | '.join(map(number, vals)) + ' |')
    lines += ['', '- ARI·NMI는 높을수록 좋음. over-merge·over-split 비율은 낮을수록 좋음.',
              '- over-merge = 서로 다른 파일인데 함께 묶인 쌍 / 함께 묶인 모든 쌍.',
              '- over-split = 같은 파일인데 나뉜 쌍 / 실제 같은 파일의 모든 쌍.',
              '- 분모가 0이면 비율은 null/—이며 0% 성공으로 계산하지 않음.',
              '- comparison.json에 모든 seed, 혼합 그룹/분리 파일 수, ZIP 적용 블록 및 그 블록과 연결된 쌍의 평가를 저장함.',
              '- ZIP 적용 블록 부분집합의 ARI/NMI는 부분집합 밖과의 오병합을 놓칠 수 있으므로 pairs_touching_zip_blocks도 함께 확인함.',
              '- 모델 메타에서 미관측인 파일은 별도 유도 그래프에서 새로 clustering함. 전체 결과에서 점수만 잘라낸 것이 아님.',
              '- 편의 표본의 탐색적 비교임. seed 편차는 반복 실행 변동이며 독립 파일 표본의 신뢰구간이 아님.',
              '- k/임계값/보강량/resolution은 이번 파일럿용 고정 설정임. 나린님 원래 grouping을 재현한 결과가 아니며 이 결과로 최적값을 고르지 않음.',
              '- 전체 혼합 포맷 그래프를 사용함. 정답 포맷으로 쌍을 걸러내지 않으며 V7의 동일 포맷 음성 학습 조건과 다름.',
              '- PASS도 다른 파일의 동일 콘텐츠와 연결될 수 있음. 강제 병합·음성 제약·ZIP FAIL에 의한 점수 차감은 사용하지 않음.', '']
    Path(path).write_text('\n'.join(lines))


def run(args):
    output = Path(args.output)
    if output.exists():
        raise FileExistsError('output exists; choose a new comparison directory')
    if not environment()['ready']:
        raise RuntimeError('install requirements-grouping.txt in a separate CPU venv first')
    if (args.k < 1 or not 0 <= args.threshold <= 1 or not 0 <= args.zip_alpha <= 1
            or not math.isfinite(args.resolution) or args.resolution <= 0
            or not args.seeds or len(set(args.seeds)) != len(args.seeds)
            or any(s < 0 or s >= 2**31 for s in args.seeds)):
        raise ValueError('invalid graph parameters or seeds')
    x = load_blocks(args.blocks)
    print('완료된 Same-File 점수와 블록 대응 확인', flush=True)
    scores, score_info = load_scores(x, args.scores)
    print('ZIP 후보·payload 판정을 바이트로 재검사', flush=True)
    passed, zip_info = load_zip_pairs(x, args.payload)
    graphs = build_graphs(scores, passed, args.k, args.threshold, args.zip_alpha)
    parameters = dict(k=args.k, threshold=args.threshold, zip_alpha=args.zip_alpha,
                      resolution=args.resolution, seeds=args.seeds,
                      graph='undirected union kNN after score threshold; ties by sample_id',
                      zip_weight='s + alpha * (1-s); add PASS pairs outside kNN; unique pair once',
                      alpha_zero='exact baseline including topology',
                      leiden_partition='RBConfigurationVertexPartition', n_iterations=-1,
                      no_true_group_count_used=True, negative_constraints=False)
    output.mkdir(parents=True, exist_ok=False)
    dump(output / 'run_state.json', dict(status='running'))
    records, partitions = [], []
    try:
        dump(output / 'parameters.json', parameters)
        with (output / 'graph_edges.csv').open('x', newline='') as f:
            w = csv.writer(f)
            w.writerow(['sample_id_1', 'sample_id_2', 'same_file_score',
                        'samefile_weight', 'samefile_zip_weight', 'zip_pass'])
            for i, j in sorted(set(graphs[VARIANTS[0]]) | set(graphs[VARIANTS[1]])):
                w.writerow([i, j, scores[i, j], graphs[VARIANTS[0]].get((i,j), 0),
                            graphs[VARIANTS[1]].get((i,j), 0), int((i,j) in passed)])

        def infer(cohort, ids):
            if len(ids) < 2:
                return []
            id_set = set(ids)
            local_pass = {(i,j) for i,j in passed if i in id_set and j in id_set}
            touched_ids = {i for pair in local_pass for i in pair}
            touched = np.asarray([i in touched_ids for i in ids])
            inferred = []
            for seed in args.seeds:
                for variant in VARIANTS:
                    print(f'Leiden: {cohort} | {variant} | seed={seed}', flush=True)
                    labels, graph_info = cluster(ids, graphs[variant], seed, args.resolution)
                    record = dict(cohort=cohort, variant=variant, seed=seed, graph=graph_info,
                                  zip_pass_pairs=len(local_pass), zip_touched_blocks=int(touched.sum()))
                    partitions.extend(dict(cohort=cohort, variant=variant, seed=seed,
                                           sample_id=int(i), cluster_id=int(c)) for i,c in zip(ids, labels))
                    inferred.append((record, labels, touched))
            return inferred

        all_ids = np.arange(len(x))
        full = infer('all_pilot', all_ids)  # Full inference BEFORE opening truth.
        truth, _ = read_truth(args.truth, len(x))
        true_labels = np.asarray([r['source_file_id'] for r in truth])
        cohorts = dict(all_pilot=dict(blocks=len(x), files=len(set(true_labels)),
                                     sample_ids=all_ids.tolist()))

        def evaluate(inferred, ids):
            for record, labels, touched in inferred:
                record['evaluation'] = evaluate_memberships(true_labels[ids], labels, touched)
                records.append(record)

        evaluate(full, all_ids)
        overlap_info = None
        if args.overlap:
            ids = unseen_ids(truth, args.truth, x, args.overlap)
            cohorts['not_observed_in_model_metadata'] = dict(
                blocks=len(ids), files=len(set(true_labels[ids])), sample_ids=ids.tolist(),
                status='run' if len(ids) >= 2 else 'skipped_fewer_than_two_blocks',
                selection='exclude files observed in any model train/val/test metadata',
                graph='induced subgraph of each full graph; no kNN refill; Leiden rerun')
            evaluate(infer('not_observed_in_model_metadata', ids), ids)
            overlap_info = dict(path=str(args.overlap),
                                summary_sha256=file_hash(Path(args.overlap) / 'overlap_summary.json'),
                                sources_sha256=file_hash(Path(args.overlap) / 'pilot_source_overlap.csv'))
        with (output / 'memberships.csv').open('x', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['cohort', 'variant', 'seed', 'sample_id', 'cluster_id'])
            w.writeheader()
            w.writerows(partitions)
        result = dict(experiment='samefile-zip-leiden-pilot-v1', parameters=parameters,
                      environment=environment(), blocks_sha256=fingerprint(x),
                      truth_path=str(args.truth), truth_sha256=file_hash(args.truth),
                      scores=score_info, zip=zip_info, overlap=overlap_info, cohorts=cohorts,
                      graph_changes=dict(
                          added_edges=len(set(graphs[VARIANTS[1]])-set(graphs[VARIANTS[0]])),
                          increased_existing_edges=sum(graphs[VARIANTS[1]][p] > w
                                                       for p,w in graphs[VARIANTS[0]].items())),
                      graph_truth_used=False, truth_use='evaluation and predefined overlap cohort only',
                      records=records, aggregate=summarize(records),
                      graph_edges_sha256=file_hash(output / 'graph_edges.csv'),
                      memberships_sha256=file_hash(output / 'memberships.csv'),
                      note='Exploratory convenience pilot, not held-out general performance. No parameter/seed selected by these labels; no automatic claims of improvement.')
        dump(output / 'comparison.json', result)
        write_report(output / 'report.md', result)
        dump(output / 'run_state.json', dict(status='complete', partitions=len(records)))
        print((output / 'report.md').read_text())
        print('비교 저장:', output)
    except Exception as e:
        dump(output / 'run_state.json', dict(status='failed', error=str(e)))
        raise
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('environment')
    r = sub.add_parser('run')
    for name in ('blocks', 'scores', 'payload', 'truth', 'output'):
        r.add_argument('--' + name, required=True)
    r.add_argument('--overlap', help='existing samefile-v7-overlap audit; optional separate cohort')
    r.add_argument('--k', type=int, default=15)
    r.add_argument('--threshold', type=float, default=0.5)
    r.add_argument('--zip-alpha', type=float, default=0.5)
    r.add_argument('--resolution', type=float, default=1.0)
    r.add_argument('--seeds', type=int, nargs='+', default=[0, 1, 2, 3, 4])
    a = p.parse_args()
    if a.command == 'environment':
        print(json.dumps(environment(), indent=2))
    else:
        run(a)


if __name__ == '__main__':
    main()
