import argparse
import contextlib
import csv
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from compare_samefile_grouping import (build_graphs, cluster, environment, evaluate_memberships,
                                      load_scores, load_zip_pairs, metrics, pair_counts, prepare_routing, run)
from score_samefile_pilot import write_scores
from test_score_samefile_pilot import dummy_encoder
from test_zip_pair_payload import archive_blocks
from zip_pair_candidates import build, fingerprint
from zip_pair_payload import check, file_hash

READY = environment()['ready']


def fixture(root):
    # Duplicate archive bytes represent different originals; preserve cross-file PASS.
    a = archive_blocks(seed=42)
    x = np.concatenate([a, a])
    np.save(root/'blocks.npy', x)
    with (root/'truth.csv').open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['sample_id', 'source_file_id', 'source_byte_offset', 'format'])
        for i in range(len(x)):
            w.writerow([i, 'file-a' if i < len(a) else 'file-b', (i % len(a))*512, 'jar'])
    build(root/'blocks.npy', root/'candidates')
    check(root/'blocks.npy', root/'candidates', root/'payload')
    write_scores(x, dummy_encoder(x), lambda f: np.full((len(f),1), -2., dtype=np.float32),
                 root/'scores', 3, {'fixture': True})
    overlap = root/'overlap'
    overlap.mkdir()
    (overlap/'overlap_summary.json').write_text(json.dumps(dict(
        pilot_blocks_sha256=fingerprint(x), truth_sha256=file_hash(root/'truth.csv'),
        identity_diagnostics={}, overlap_files=dict(train=1, val=0, test=0),
        not_observed_in_model_metadata=1)))
    (overlap/'pilot_source_overlap.csv').write_text(
        'source_file_id,format,train_model_sources,val_model_sources,test_model_sources\n'
        'file-a,jar,model-a,,\nfile-b,jar,,,\n')
    return x


def arguments(root, name='comparison', overlap=True):
    return argparse.Namespace(blocks=root/'blocks.npy', scores=root/'scores',
                              payload=root/'payload', truth=root/'truth.csv',
                              overlap=root/'overlap' if overlap else None, output=root/name,
                              k=1, threshold=.5, zip_alpha=.5, resolution=1., seeds=[0,1])


class GraphConstructionTests(unittest.TestCase):
    def test_format_restriction_precedes_topk_and_applies_to_zip_too(self):
        s = np.array([[0,.7,.99,.98], [.7,0,.97,.96],
                      [.99,.97,0,.8], [.98,.96,.8,0]])
        pairs = {(0,1), (0,2)}
        labels, passed, info = prepare_routing('oracle-format', ['jar','jar','apk','apk'], pairs, 4)
        r = build_graphs(s, passed, 1, .5, .5, labels)
        self.assertEqual(r['samefile'], {(0,1):.7, (2,3):.8})
        # Dropping edges AFTER unrestricted kNN would not recover (0,1).
        self.assertNotIn((0,1), build_graphs(s, pairs, 1, .5, .5)['samefile'])
        self.assertNotIn((0,2), r['samefile_zip'])
        self.assertAlmostEqual(r['samefile_zip'][0,1], .85)
        self.assertEqual(info['eligible_unordered_pairs'], 2)
        self.assertEqual(info['pass_pairs_excluded_cross_format'], 1)
        zero = build_graphs(s, passed, 1, .5, 0, labels)
        self.assertEqual(zero['samefile'], zero['samefile_zip'])
        # A format with only one block stays isolated even with a high ZIP score.
        labels, passed, _ = prepare_routing('oracle-format', ['jar','jar','apk','docx'], pairs, 4)
        isolated = build_graphs(s, passed, 1, .5, .5, labels)
        self.assertEqual(set(isolated['samefile_zip']), {(0,1)})

    def test_routing_requires_explicit_valid_format_labels(self):
        for mode, formats in [('unsupported',None), ('all-pairs',['jar','jar']),
                              ('oracle-format',None), ('oracle-format',['jar']),
                              ('oracle-format',['jar',' '])]:
            with self.assertRaises(ValueError):
                prepare_routing(mode, formats, set(), 2)
        labels, passed, info = prepare_routing('all-pairs', None, {(0,1)}, 2)
        self.assertIsNone(labels)
        self.assertFalse(info['ground_truth_format_used'])
        self.assertEqual(passed, {(0,1)})

    def test_knn_union_threshold_and_soft_weight_without_truth(self):
        s = np.array([[0,.8,.1,.0], [.8,0,.9,.0], [.1,.9,0,.0], [.0,.0,.0,0]])
        base = build_graphs(s, {(0,2)}, 1, .5, 0)
        self.assertEqual(base['samefile'], {(0,1):.8, (1,2):.9})
        self.assertEqual(base['samefile'], base['samefile_zip'])
        augmented = build_graphs(s, {(0,2)}, 1, .5, .5)
        self.assertAlmostEqual(augmented['samefile_zip'][0,2], .55)
        self.assertEqual(augmented['samefile'], base['samefile'])
        self.assertEqual(len(augmented['samefile_zip']), 3)

    def test_pair_count_definitions_match_enumerated_pairs(self):
        truth = np.array(['a','a','a','b','b','c'])
        pred = np.array([0,0,1,0,2,2])
        expected = dict(tp=0, fp=0, fn=0, tn=0)
        for i in range(len(truth)):
            for j in range(i+1, len(truth)):
                same, joined = truth[i] == truth[j], pred[i] == pred[j]
                expected['tp' if same and joined else 'fp' if joined else 'fn' if same else 'tn'] += 1
        self.assertEqual(pair_counts(truth, pred), expected)

    def test_score_integrity_rejects_swapped_blocks_duplicate_and_missing_pairs(self):
        x = np.array([[0]*512, [1]*512, [2]*512], dtype=np.uint8)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            write_scores(x, dummy_encoder(x), lambda f: f[:,:1], root/'scores', 4, {})
            matrix, _ = load_scores(x, root/'scores')
            self.assertEqual(matrix.shape, (3,3))
            with self.assertRaisesRegex(ValueError, 'blocks/order'):
                load_scores(x[::-1], root/'scores')
            path = root/'scores/samefile_scores.csv.gz'
            with gzip.open(path, 'rt') as f:
                original = f.readlines()
            manifest_path = root/'scores/score_manifest.json'
            for lines, message in [(original[:-1], 'missing'),
                                   (original + [original[1]], 'duplicate')]:
                with gzip.open(path, 'wt') as f:
                    f.writelines(lines)
                manifest = json.loads(manifest_path.read_text())
                manifest['scores_sha256'] = file_hash(path)
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, message):
                    load_scores(x, root/'scores')
            state = root/'scores/run_state.json'
            state.write_text('{"status":"running"}')
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                load_scores(x, root/'scores')

    def test_payload_replay_keeps_ambiguous_pass_and_catches_tampering(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            x = fixture(root)
            pairs, info = load_zip_pairs(x, root/'payload')
            self.assertTrue(any(i < len(x)//2 <= j for i,j in pairs))
            self.assertGreater(info['replay_counts']['PASS'], 0)
            p = root/'payload/payload_evidence.jsonl'
            rows = [json.loads(s) for s in p.read_text().splitlines()]
            rows[0]['check']['status'] = 'FAIL'
            p.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with self.assertRaisesRegex(ValueError, 'does not reproduce'):
                load_zip_pairs(x, root/'payload')


@unittest.skipUnless(READY, 'requires separate requirements-grouping.txt environment')
class LeidenComparisonTests(unittest.TestCase):
    def test_oracle_metadata_labels_scope_without_using_file_ids_or_offsets(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            x = fixture(root)
            path = root/'truth.csv'
            rows = list(csv.DictReader(io.StringIO(path.read_text())))
            for row in rows[len(x)//2:]:
                row['format'] = 'apk'
            def save_truth():
                with path.open('w', newline='') as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0]))
                    w.writeheader()
                    w.writerows(rows)
            save_truth()
            args = arguments(root, 'oracle', overlap=False)
            args.routing = 'oracle-format'
            result = run(args)
            self.assertTrue(result['graph_truth_used'])
            self.assertEqual(result['experiment'], 'samefile-zip-leiden-oracle-format-pilot-v1')
            self.assertGreater(result['routing']['pass_pairs_excluded_cross_format'], 0)
            self.assertIn('실제 FFC', (root/'oracle/report.md').read_text())
            with (root/'oracle/graph_edges.csv').open() as f:
                edges = list(csv.DictReader(f))
            self.assertTrue(edges)
            for e in edges:
                self.assertEqual(rows[int(e['sample_id_1'])]['format'], rows[int(e['sample_id_2'])]['format'])
            from audit_grouping import audit
            checked = audit(root/'blocks.npy', path, root/'oracle', root/'oracle-audit')
            self.assertEqual(checked['routing'], result['routing'])
            for i, row in enumerate(rows):
                row['source_file_id'] = f'arbitrary-source-{i}'
                row['source_byte_offset'] = '0'
            save_truth()
            args.output = root/'changed-ids'
            run(args)
            self.assertEqual((root/'oracle/graph_edges.csv').read_bytes(),
                             (root/'changed-ids/graph_edges.csv').read_bytes())
            self.assertEqual((root/'oracle/memberships.csv').read_bytes(),
                             (root/'changed-ids/memberships.csv').read_bytes())

    def test_known_partition_isolates_and_induced_reclustering(self):
        ids = np.arange(7)
        edges = {(0,1):1., (0,2):1., (1,2):1., (3,4):1., (3,5):1., (4,5):1.}
        a, _ = cluster(ids, edges, 42, 1.)
        b, _ = cluster(ids, edges, 42, 1.)
        np.testing.assert_array_equal(a, b)
        self.assertEqual(metrics(np.array(['a']*3+['b']*3+['c']), a)['ari'], 1.)
        # Path through a removed node must not survive a fresh induced partition.
        sub, graph = cluster(np.array([0,2]), {(0,1):1., (1,2):1.}, 42, 1.)
        self.assertNotEqual(sub[0], sub[1])
        self.assertEqual(graph['isolated_nodes'], 2)

    def test_metrics_handle_all_merged_all_split_and_undefined_rates(self):
        truth = np.array(['a','a','b','b'])
        merged = metrics(truth, np.zeros(4, dtype=int))
        self.assertEqual(merged['over_merge_pairs'], 4)
        self.assertAlmostEqual(merged['over_merge_rate'], 4/6)
        self.assertEqual(merged['over_split_rate'], 0)
        self.assertEqual(merged['mixed_clusters'], 1)
        split = metrics(truth, np.arange(4))
        self.assertIsNone(split['over_merge_rate'])
        self.assertEqual(split['over_split_rate'], 1)
        self.assertEqual(split['split_files'], 2)
        self.assertEqual(metrics(truth, np.array([3,3,7,7]))['nmi'], 1.)

    def test_supported_subset_also_reports_merges_with_unsupported_blocks(self):
        truth = np.array(['a','b','b'])
        r = evaluate_memberships(truth, np.array([0,0,1]), np.array([True,False,False]))
        self.assertEqual(r['zip_supported_blocks']['over_merge_pairs'], 0)
        self.assertEqual(r['pairs_touching_zip_blocks']['over_merge_pairs'], 1)

    def test_end_to_end_every_seed_no_truth_routing_and_zero_alpha_control(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            x = fixture(root)
            r = run(arguments(root))
            self.assertEqual(len(r['records']), 8)
            self.assertEqual(r['cohorts']['not_observed_in_model_metadata']['blocks'], len(x)//2)
            state = json.loads((root/'comparison/run_state.json').read_text())
            self.assertEqual(state['status'], 'complete')
            self.assertGreater(r['graph_changes']['added_edges'], 0)
            # Alpha=0 must yield identical partitions and zero paired metric deltas.
            args = arguments(root, 'nozip')
            args.zip_alpha = 0.
            control = run(args)
            for c in control['aggregate'].values():
                for key in ('ari', 'nmi', 'over_split_rate'):
                    self.assertEqual(c['all_blocks'][key]['paired_delta_zip_minus_samefile']['mean'], 0.)
            # Truth permutation changes metrics, never full graph memberships.
            truth = root/'truth.csv'
            lines = list(csv.DictReader(io.StringIO(truth.read_text())))
            for i,line in enumerate(lines):
                line['source_file_id'] = 'one-file'
                line['source_byte_offset'] = str(i*512)
            with truth.open('w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=list(lines[0]))
                w.writeheader()
                w.writerows(lines)
            run(arguments(root, 'permuted', overlap=False))
            def memberships(path):
                with path.open() as f:
                    return [row for row in csv.DictReader(f) if row['cohort'] == 'all_pilot']
            self.assertEqual(memberships(root/'comparison/memberships.csv'),
                             memberships(root/'permuted/memberships.csv'))
            self.assertEqual((root/'comparison/graph_edges.csv').read_bytes(),
                             (root/'permuted/graph_edges.csv').read_bytes())
            with self.assertRaises(FileExistsError):
                run(arguments(root))

    def test_failure_preserves_failed_state_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            fixture(root)
            with patch('compare_samefile_grouping.cluster', side_effect=RuntimeError('test error')):
                with self.assertRaises(RuntimeError):
                    run(arguments(root))
            state = json.loads((root/'comparison/run_state.json').read_text())
            self.assertEqual(state['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
