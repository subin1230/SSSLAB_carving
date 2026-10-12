"""Post-hoc score, graph and partition diagnostics. Never changes a prediction.

Truth diagnoses saved results. For an explicitly marked oracle-format run its
routing is replayed solely for integrity checking; no new configuration is chosen.
No model inference or Leiden optimization is performed.
"""
import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path

import numpy as np

from audit_zip_payload import read_truth
from compare_samefile_grouping import (VARIANTS, aggregate, build_graphs, dump,
                                      load_scores, load_zip_pairs, pair_counts, prepare_routing, unseen_ids)
from zip_pair_candidates import fingerprint, load_blocks
from zip_pair_payload import file_hash

TRANSITIONS = ('recovered_same_file_pairs', 'new_wrong_file_merges',
               'resolved_wrong_file_merges', 'new_same_file_splits')


def describe(values):
    values = np.asarray(values, dtype=np.float64)
    if not len(values):
        return dict(n=0, mean=None, min=None, p50=None, p95=None, max=None)
    return dict(n=len(values), mean=float(values.mean()), min=float(values.min()),
                p50=float(np.quantile(values, .5)), p95=float(np.quantile(values, .95)),
                max=float(values.max()))


def classification(scores, same_file, threshold):
    """Ranking and the EXISTING threshold, not a search for a better threshold."""
    from sklearn.metrics import average_precision_score, roc_auc_score
    predicted = scores >= threshold
    tp = int(np.count_nonzero(predicted & same_file))
    fp = int(np.count_nonzero(predicted & ~same_file))
    positives = int(same_file.sum())
    both = 0 < positives < len(scores)
    return dict(pairs=len(scores), same_file_pairs=positives,
                positive_prevalence=positives/len(scores) if len(scores) else None,
                roc_auc=float(roc_auc_score(same_file, scores)) if both else None,
                average_precision=float(average_precision_score(same_file, scores)) if both else None,
                threshold=threshold, selected_pairs=tp+fp, tp=tp, fp=fp,
                fn=positives-tp, tn=len(scores)-positives-fp,
                precision=tp/(tp+fp) if tp+fp else None,
                recall=tp/positives if positives else None)


def score_audit(scores, file_codes, format_codes, threshold):
    u, v = np.triu_indices(len(file_codes), 1)
    same = file_codes[u] == file_codes[v]
    same_format = format_codes[u] == format_codes[v]
    values = scores[u, v]
    buckets = dict(same_file=same, different_file_same_format=~same & same_format,
                   different_format=~same_format)
    report = dict(by_relation={k: dict(describe(values[m]),
                                      above_threshold=int(np.count_nonzero(values[m] >= threshold)))
                               for k,m in buckets.items()},
                  all_pairs=classification(values, same, threshold),
                  same_format_pairs=classification(values[same_format], same[same_format], threshold))
    return report, u, v, same


def edge_audit(ids, edges, truth):
    """Selected edge purity is distinct from final co-cluster pair purity."""
    allowed = set(map(int, ids))
    selected = [(i,j,w) for (i,j),w in sorted(edges.items()) if i in allowed and j in allowed]
    counts, weights = Counter(), Counter()
    format_pairs = defaultdict(Counter)
    same_degree = Counter()
    for i,j,w in selected:
        a, b = truth[i], truth[j]
        same = a['source_file_id'] == b['source_file_id']
        category = ('same_file' if same else 'different_file_same_format'
                    if a['format'] == b['format'] else 'different_format')
        counts[category] += 1
        weights[category] += w
        key = ' / '.join(sorted((a['format'], b['format'])))
        format_pairs[key]['edges'] += 1
        format_pairs[key]['same_file_edges' if same else 'wrong_file_edges'] += 1
        if same:
            same_degree[i] += 1
            same_degree[j] += 1
    total_weight = sum(weights.values())
    return dict(edges=len(selected), by_relation=dict(counts), weight_by_relation=dict(weights),
                same_file_precision=counts['same_file']/len(selected) if selected else None,
                same_file_weight_fraction=weights['same_file']/total_weight if total_weight else None,
                wrong_same_format_edges=counts['different_file_same_format'],
                wrong_cross_format_edges=counts['different_format'],
                nodes_without_direct_same_file_edge=sum(same_degree[int(i)] == 0 for i in ids),
                by_format_pair=dict(sorted(format_pairs.items())))


def partition_details(ids, labels, truth):
    groups, sources = defaultdict(list), defaultdict(list)
    for sid, label in zip(ids, labels):
        groups[int(label)].append(int(sid))
        sources[truth[int(sid)]['source_file_id']].append(int(label))
    cluster_rows = []
    for label, members in sorted(groups.items()):
        files = Counter(truth[i]['source_file_id'] for i in members)
        formats = Counter(truth[i]['format'] for i in members)
        majority = max(files.values())
        cluster_rows.append(dict(cluster_id=label, blocks=len(members), source_files=len(files),
                                 formats=len(formats), majority_file_blocks=majority,
                                 majority_file_fraction=majority/len(members),
                                 source_file_counts=json.dumps(dict(sorted(files.items()))),
                                 format_counts=json.dumps(dict(sorted(formats.items())))))
    file_rows = [dict(source_file_id=f, blocks=len(values), clusters=len(set(values)),
                      largest_piece_blocks=max(Counter(values).values()))
                 for f,values in sorted(sources.items())]
    summary = dict(clusters=len(groups), true_files=len(sources),
                   cluster_sizes=describe([len(g) for g in groups.values()]),
                   source_files_per_cluster=describe([r['source_files'] for r in cluster_rows]),
                   mixed_file_clusters=sum(r['source_files'] > 1 for r in cluster_rows),
                   mixed_format_clusters=sum(r['formats'] > 1 for r in cluster_rows),
                   split_files=sum(r['clusters'] > 1 for r in file_rows),
                   majority_file_purity=sum(r['majority_file_blocks'] for r in cluster_rows)/len(ids))
    return summary, cluster_rows, file_rows


def transitions(same_file, before, after, mask):
    """Compare pair equivalence, not arbitrary numeric cluster IDs."""
    joined = ~before & after & mask
    separated = before & ~after & mask
    return dict(
        evaluated_pairs=int(np.count_nonzero(mask)),
        recovered_same_file_pairs=int(np.count_nonzero(joined & same_file)),
        new_wrong_file_merges=int(np.count_nonzero(joined & ~same_file)),
        resolved_wrong_file_merges=int(np.count_nonzero(separated & ~same_file)),
        new_same_file_splits=int(np.count_nonzero(separated & same_file)))


def read_partitions(run, comparison, count):
    expected = {(r['cohort'], r['seed'], r['variant']): r for r in comparison['records']}
    if len(expected) != len(comparison['records']):
        raise ValueError('duplicate comparison record')
    cohorts = {}
    for name, c in comparison['cohorts'].items():
        ids = c['sample_ids']
        if (len(ids) != c['blocks'] or len(set(ids)) != len(ids)
                or any(type(i) is not int or not 0 <= i < count for i in ids)):
            raise ValueError('invalid cohort IDs')
        if c.get('status', 'run') == 'run':
            cohorts[name] = np.asarray(ids, dtype=np.int64)
    if set(comparison['cohorts']['all_pilot']['sample_ids']) != set(range(count)):
        raise ValueError('full cohort must include all blocks')
    required = {(c,s,v) for c in cohorts for s in comparison['parameters']['seeds'] for v in VARIANTS}
    if set(expected) != required:
        raise ValueError('missing/extra comparison partitions')
    saved = {key: {} for key in required}
    allowed = {c: set(ids) for c,ids in cohorts.items()}
    with (run/'memberships.csv').open(newline='') as f:
        for row in csv.DictReader(f):
            key = row['cohort'], int(row['seed']), row['variant']
            sid, label = int(row['sample_id']), int(row['cluster_id'])
            if key not in saved or sid not in allowed[key[0]] or sid in saved[key] or label < 0:
                raise ValueError('duplicate or invalid membership')
            saved[key][sid] = label
    for key in saved:
        ids = cohorts[key[0]]
        if set(saved[key]) != allowed[key[0]]:
            raise ValueError('missing membership rows')
        saved[key] = np.asarray([saved[key][int(i)] for i in ids], dtype=np.int64)
    return cohorts, expected, saved


def check_saved_graph(run, scores, passed, graphs):
    wanted = set(graphs[VARIANTS[0]]) | set(graphs[VARIANTS[1]])
    seen = set()
    with (run/'graph_edges.csv').open(newline='') as f:
        for row in csv.DictReader(f):
            i,j = int(row['sample_id_1']), int(row['sample_id_2'])
            if (i,j) not in wanted or (i,j) in seen:
                raise ValueError('unexpected or duplicate saved graph edge')
            seen.add((i,j))
            if int(row['zip_pass']) != int((i,j) in passed):
                raise ValueError('saved ZIP flag differs from replay')
            for value, target in [(row['same_file_score'], scores[i,j]),
                                  (row['samefile_weight'], graphs[VARIANTS[0]].get((i,j),0)),
                                  (row['samefile_zip_weight'], graphs[VARIANTS[1]].get((i,j),0))]:
                if not math.isclose(float(value), target, rel_tol=1e-12, abs_tol=1e-12):
                    raise ValueError('saved graph weight does not reproduce')
    if seen != wanted:
        raise ValueError('missing saved graph edges')


def write_csv(path, rows):
    if not rows:
        Path(path).write_text('')
        return
    with Path(path).open('x', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def render(report):
    def pct(x):
        return '—' if x is None else f'{x:.2%}'
    def num(x):
        return '—' if x is None else f'{x:.4f}'
    lines = ['# Grouping 원인 진단', '',
             '저장된 점수·그래프·그룹의 사후 정답 분석임. 추론·clustering·설정 변경 없음.', '']
    oracle = report.get('routing', {}).get('ground_truth_format_used', False)
    if oracle:
        lines += ['**원 실행은 정답 포맷으로 연결 후보를 제한한 oracle 진단임. 실제 FFC 성능이 아님.**',
                  '원래 점수 평가는 라우팅 전 점수임. 아래 그래프·그룹 평가는 라우팅 후 저장된 결과임.', '']
    for name,c in report['cohorts'].items():
        lines += [f'## {name} — {c["blocks"]}블록 / {c["files"]}파일', '',
                  '| 원래 점수 평가 | 쌍 수 | 같은 파일 비율 | AP | ROC-AUC | 기존 임계값 정밀도 |',
                  '|---|---:|---:|---:|---:|---:|']
        for scope in ('all_pairs','same_format_pairs'):
            s = c['scores'][scope]
            lines.append(f'| {scope} | {s["pairs"]} | {pct(s["positive_prevalence"])} | '
                         f'{num(s["average_precision"])} | {num(s["roc_auc"])} | {pct(s["precision"])} |')
        lines += ['', '| 선택된 그래프 | 연결 수 | 같은 파일 연결 비율 | 같은 포맷·다른 파일 | 다른 포맷 |',
                  '|---|---:|---:|---:|---:|']
        for v in VARIANTS:
            g = c['graphs'][v]
            lines.append(f'| {v} | {g["edges"]} | {pct(g["same_file_precision"])} | '
                         f'{g["wrong_same_format_edges"]} | {g["wrong_cross_format_edges"]} |')
        lines += ['', '| 최종 그룹 — seed 평균 | 그룹 수 | 최대 그룹 블록 수 | 혼합 파일 그룹 수 | 분리된 파일 수 |',
                  '|---|---:|---:|---:|---:|']
        for v in VARIANTS:
            p = [r['partitions'][v] for r in c['seeds']]
            means = [np.mean([r['clusters'] for r in p]), np.mean([r['cluster_sizes']['max'] for r in p]),
                     np.mean([r['mixed_file_clusters'] for r in p]), np.mean([r['split_files'] for r in p])]
            lines.append('| '+v+' | '+' | '.join(f'{m:.2f}' for m in means)+' |')
        lines += ['', '| ZIP 보완 전후 바뀐 쌍 — seed 평균 | 같은 파일 연결 회복 | 새로운 오병합 | 기존 오병합 해소 | 새로운 과분리 |',
                  '|---|---:|---:|---:|---:|']
        for scope, changes in c['transition_aggregate'].items():
            lines.append('| '+scope+' | '+' | '.join(f'{changes[k]["mean"]:.2f}' for k in TRANSITIONS)+' |')
        lines.append('')
    lines += ['- AP는 Average Precision이며, 고정 임계값의 정밀도와 다른 순위 지표임. 같은 파일 비율과 함께 해석함.',
              '- 그래프 연결 정밀도는 직접 선택된 엣지 기준임. 최종 over-merge는 같은 그룹의 모든 쌍 기준이므로 서로 다름.',
              ('- same_format_pairs는 원래 점수 중 라우팅 허용 범위의 평가임. 그래프는 동일 포맷 내부에서 이웃을 다시 선택한 저장 결과임.'
               if oracle else '- same_format_pairs는 정답 포맷을 이용한 사후 진단 범위임. 포맷으로 쌍을 걸러낸 새 grouping 결과가 아님.'),
              '- pairs_touching_zip_blocks는 ZIP 적용 블록과 비적용 블록 사이의 관계도 포함함.',
              '- 직접 ZIP 쌍 / ZIP에 닿는 쌍 / 전체 쌍은 중첩되므로 더하지 않음.',
              '- 숫자 그룹 ID의 변경은 세지 않음. 두 블록이 함께 묶였는지가 바뀐 경우만 셈.',
              '- 변화는 두 조건의 결과 차이임. 특정 ZIP 엣지 하나가 변화를 일으켰다는 인과 분석은 아님.',
              '- 현재 그래프 설정·모델·편의 파일럿의 진단임. 좋은 결과를 고르기 위한 임계값 탐색을 수행하지 않음.', '']
    return '\n'.join(lines)


def audit(blocks, truth_path, run, output):
    run, output = Path(run), Path(output)
    if output.exists():
        raise FileExistsError('output exists; choose a new audit directory')
    comparison = json.loads((run/'comparison.json').read_text())
    state = json.loads((run/'run_state.json').read_text())
    if (state.get('status') != 'complete' or state.get('partitions') != len(comparison['records'])
            or comparison.get('experiment') not in ('samefile-zip-leiden-pilot-v1',
                                                    'samefile-zip-leiden-oracle-format-pilot-v1')):
        raise ValueError('requires completed supported grouping run')
    x = load_blocks(blocks)
    if fingerprint(x) != comparison['blocks_sha256'] or file_hash(truth_path) != comparison['truth_sha256']:
        raise ValueError('blocks/truth differ from grouping input')
    for name,key in [('graph_edges.csv','graph_edges_sha256'), ('memberships.csv','memberships_sha256')]:
        if file_hash(run/name) != comparison[key]:
            raise ValueError('saved '+name+' hash mismatch')
    print('저장된 점수·ZIP 근거·그래프 대응 검사', flush=True)
    scores, score_info = load_scores(x, comparison['scores']['run'])
    if any(score_info[k] != comparison['scores'][k] for k in ('manifest_sha256','scores_sha256')):
        raise ValueError('score input changed since grouping')
    passed, zip_info = load_zip_pairs(x, comparison['zip']['run'])
    if any(zip_info[k] != comparison['zip'][k] for k in
           ('payload_summary_sha256','baseline_queries_sha256','evidence_sha256')):
        raise ValueError('ZIP input changed since grouping')
    p = comparison['parameters']
    truth, _ = read_truth(truth_path, len(x))
    mode = p.get('routing', 'all-pairs')
    oracle = mode == 'oracle-format'
    if (comparison.get('graph_truth_used') is not oracle or
            (comparison['experiment'] == 'samefile-zip-leiden-oracle-format-pilot-v1') != oracle):
        raise ValueError('routing/truth provenance mismatch')
    route_labels, passed, routing_info = prepare_routing(
        mode, [r['format'] for r in truth] if oracle else None, passed, len(x))
    if 'routing' in comparison and comparison['routing'] != routing_info:
        raise ValueError('routing metadata does not reproduce')
    if oracle and 'routing' not in comparison:
        raise ValueError('oracle run requires routing metadata')
    graphs = build_graphs(scores, passed, p['k'], p['threshold'], p['zip_alpha'], route_labels)
    check_saved_graph(run, scores, passed, graphs)
    # File-format disagreement would make relation buckets ambiguous.
    file_formats = defaultdict(set)
    for r in truth:
        file_formats[r['source_file_id']].add(r['format'])
    if any(len(v) != 1 for v in file_formats.values()):
        raise ValueError('one source file has multiple format labels')
    cohorts, records, memberships = read_partitions(run, comparison, len(x))
    if 'not_observed_in_model_metadata' in cohorts:
        overlap = comparison['overlap']
        for name,key in [('overlap_summary.json','summary_sha256'),('pilot_source_overlap.csv','sources_sha256')]:
            if file_hash(Path(overlap['path'])/name) != overlap[key]:
                raise ValueError('overlap audit changed since grouping')
        expected = unseen_ids(truth, truth_path, x, overlap['path'])
        if not np.array_equal(expected, cohorts['not_observed_in_model_metadata']):
            raise ValueError('overlap cohort does not reproduce')
    report = dict(audit='grouping-diagnostics-v1', comparison_path=str(run/'comparison.json'),
                  comparison_sha256=file_hash(run/'comparison.json'), blocks_sha256=fingerprint(x),
                  truth_sha256=file_hash(truth_path), truth_used=True, inference_performed=False,
                  input_results_modified=False, routing=routing_info, cohorts={})
    clusters, files, zip_rows = [], [], []
    for name, ids in cohorts.items():
        print(f'점수·연결·그룹 진단: {name}', flush=True)
        local_truth = np.asarray([truth[int(i)]['source_file_id'] for i in ids])
        _, fcode = np.unique(local_truth, return_inverse=True)
        _, tcode = np.unique([truth[int(i)]['format'] for i in ids], return_inverse=True)
        score_stats, u, v, same = score_audit(scores[np.ix_(ids,ids)], fcode, tcode, p['threshold'])
        position = {int(sid): i for i,sid in enumerate(ids)}
        zip_local = [(i,j) for i,j in sorted(passed) if i in position and j in position]
        zip_matrix = np.zeros((len(ids),len(ids)), dtype=bool)
        touched = np.zeros(len(ids), dtype=bool)
        for i,j in zip_local:
            a,b = position[i],position[j]
            zip_matrix[a,b] = zip_matrix[b,a] = True
            touched[a] = touched[b] = True
        masks = dict(all_pairs=np.ones(len(u),dtype=bool),
                     pairs_touching_zip_blocks=touched[u] | touched[v],
                     direct_zip_pairs=zip_matrix[u,v])
        result = dict(blocks=len(ids), files=len(set(local_truth)), scores=score_stats,
                      graphs={v: edge_audit(ids,graphs[v],truth) for v in VARIANTS},
                      zip_pass_pairs=len(zip_local), zip_touched_blocks=int(touched.sum()), seeds=[])
        for seed in p['seeds']:
            part, together = {}, {}
            for variant in VARIANTS:
                labels = memberships[name,seed,variant]
                existing = records[name,seed,variant]['evaluation']['all_blocks']
                if pair_counts(local_truth, labels) != existing['pair_counts']:
                    raise ValueError('membership pair counts do not match comparison')
                detail, cr, fr = partition_details(ids,labels,truth)
                if detail['clusters'] != existing['clusters']:
                    raise ValueError('cluster count does not match comparison')
                part[variant] = detail
                prefix = dict(cohort=name, seed=seed, variant=variant)
                clusters.extend(dict(prefix, **r) for r in cr)
                files.extend(dict(prefix, **r) for r in fr)
                together[variant] = labels[u] == labels[v]
            before, after = (together[v] for v in VARIANTS)
            changes = {scope: transitions(same,before,after,m) for scope,m in masks.items()}
            base = records[name,seed,VARIANTS[0]]['evaluation']['all_blocks']['pair_counts']
            aug = records[name,seed,VARIANTS[1]]['evaluation']['all_blocks']['pair_counts']
            c = changes['all_pairs']
            if (aug['fp']-base['fp'] != c['new_wrong_file_merges']-c['resolved_wrong_file_merges']
                    or aug['fn']-base['fn'] != c['new_same_file_splits']-c['recovered_same_file_pairs']):
                raise ValueError('pair transition accounting does not balance')
            result['seeds'].append(dict(seed=seed, partitions=part, transitions=changes))
            for i,j in zip_local:
                a,b = position[i],position[j]
                zip_rows.append(dict(cohort=name, seed=seed, sample_id_1=i, sample_id_2=j,
                                     same_file=int(truth[i]['source_file_id']==truth[j]['source_file_id']),
                                     format_1=truth[i]['format'], format_2=truth[j]['format'],
                                     score=float(scores[i,j]), baseline_edge=int((i,j) in graphs[VARIANTS[0]]),
                                     baseline_together=int(memberships[name,seed,VARIANTS[0]][a] == memberships[name,seed,VARIANTS[0]][b]),
                                     zip_together=int(memberships[name,seed,VARIANTS[1]][a] == memberships[name,seed,VARIANTS[1]][b])))
        result['transition_aggregate'] = {
            scope: {k: aggregate([r['transitions'][scope][k] for r in result['seeds']]) for k in TRANSITIONS}
            for scope in masks}
        report['cohorts'][name] = result
    output.mkdir(parents=True, exist_ok=False)
    dump(output/'run_state.json', dict(status='running'))
    try:
        dump(output/'diagnosis.json', report)
        write_csv(output/'cluster_details.csv', clusters)
        write_csv(output/'file_spread.csv', files)
        write_csv(output/'zip_pair_outcomes.csv', zip_rows)
        (output/'report.md').write_text(render(report))
        dump(output/'run_state.json', dict(status='complete'))
    except Exception as e:
        dump(output/'run_state.json', dict(status='failed',error=str(e)))
        raise
    print(render(report))
    print('진단 저장:', output)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ('blocks','truth','run','output'):
        p.add_argument('--'+key, required=True)
    a = p.parse_args()
    audit(a.blocks,a.truth,a.run,a.output)


if __name__ == '__main__':
    main()
