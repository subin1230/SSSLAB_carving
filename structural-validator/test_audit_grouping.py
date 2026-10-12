import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from audit_grouping import (audit, classification, edge_audit, partition_details,
                            read_partitions, score_audit, transitions)
from compare_samefile_grouping import environment, run
from test_compare_samefile_grouping import arguments, fixture
from zip_pair_payload import file_hash

READY = environment()['ready']


class GroupingAuditTests(unittest.TestCase):
    def test_edge_purity_separates_wrong_same_format_and_cross_format(self):
        truth = [dict(source_file_id=f, format=t) for f,t in
                 [('a','jar'),('a','jar'),('b','jar'),('c','apk')]]
        edges = {(0,1):.9,(0,2):.95,(0,3):.99}
        r = edge_audit(np.arange(4),edges,truth)
        self.assertEqual(r['by_relation'], dict(same_file=1,different_file_same_format=1,different_format=1))
        self.assertAlmostEqual(r['same_file_precision'],1/3)
        self.assertEqual(r['nodes_without_direct_same_file_edge'],2)
        # An induced cohort must exclude edges to removed nodes, not just score rows.
        sub = edge_audit(np.array([0,1,2]),edges,truth)
        self.assertEqual(sub['edges'],2)
        self.assertEqual(sub['wrong_cross_format_edges'],0)
        self.assertEqual(sub['same_file_precision'],.5)

    def test_cluster_profiles_count_mixed_clusters_and_file_fragments(self):
        truth = [dict(source_file_id=f, format=t) for f,t in
                 [('a','jar'),('a','jar'),('b','apk'),('b','apk'),('c','docx')]]
        summary, clusters, files = partition_details(np.arange(5),np.array([7,7,7,9,10]),truth)
        self.assertEqual(summary['clusters'],3)
        self.assertEqual(summary['mixed_file_clusters'],1)
        self.assertEqual(summary['mixed_format_clusters'],1)
        self.assertEqual(summary['split_files'],1)
        self.assertEqual(summary['majority_file_purity'],.8)
        self.assertEqual(next(f['clusters'] for f in files if f['source_file_id']=='b'),2)
        self.assertEqual(json.loads(clusters[0]['source_file_counts']),dict(a=2,b=1))

    def test_pair_transitions_ignore_group_number_permutations(self):
        u,v = np.triu_indices(4,1)
        truth = np.array([0,0,1,1])
        base = np.array([10,10,20,20])
        renamed = np.array([3,3,9,9])
        r = transitions(truth[u]==truth[v],base[u]==base[v],renamed[u]==renamed[v],np.ones(len(u),bool))
        self.assertEqual(r['evaluated_pairs'],6)
        self.assertTrue(all(v == 0 for k,v in r.items() if k!='evaluated_pairs'))

    def test_pair_transitions_match_brute_force_with_endpoint_scope(self):
        rng = np.random.default_rng(2026)
        for _ in range(20):
            n = 8
            truth, before, after = (rng.integers(0,3,n) for _ in range(3))
            touched = rng.integers(0,2,n).astype(bool)
            u,v = np.triu_indices(n,1)
            r = transitions(truth[u]==truth[v],before[u]==before[v],after[u]==after[v],touched[u]|touched[v])
            expected = dict.fromkeys(r,0)
            for i in range(n):
                for j in range(i+1,n):
                    if not (touched[i] or touched[j]):
                        continue
                    expected['evaluated_pairs'] += 1
                    a,b = before[i]==before[j], after[i]==after[j]
                    if a == b:
                        continue
                    same = truth[i]==truth[j]
                    key = ('recovered_same_file_pairs' if same else 'new_wrong_file_merges') if b else (
                        'new_same_file_splits' if same else 'resolved_wrong_file_merges')
                    expected[key] += 1
            self.assertEqual(r,expected)


@unittest.skipUnless(READY,'requires requirements-grouping.txt environment')
class GroupingAuditIntegrationTests(unittest.TestCase):
    def test_original_run_without_routing_fields_still_audits(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            fixture(root)
            run(arguments(root))
            path = root/'comparison/comparison.json'
            saved = json.loads(path.read_text())
            del saved['routing']
            del saved['parameters']['routing']
            path.write_text(json.dumps(saved))
            result = audit(root/'blocks.npy',root/'truth.csv',root/'comparison',root/'audit')
            self.assertFalse(result['routing']['ground_truth_format_used'])
            self.assertEqual(result['routing']['pass_pairs_excluded_cross_format'], 0)

    def test_oracle_replay_keeps_within_format_wrong_pass_and_checks_provenance(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            fixture(root)
            args = arguments(root)
            args.routing = 'oracle-format'
            run(args)
            originals = {p: file_hash(p) for p in root.rglob('*') if p.is_file()}
            with patch('compare_samefile_grouping.cluster', side_effect=AssertionError('no reclustering')):
                result = audit(root/'blocks.npy',root/'truth.csv',root/'comparison',root/'audit')
            self.assertTrue(result['routing']['ground_truth_format_used'])
            self.assertEqual(result['routing']['pass_pairs_excluded_cross_format'], 0)
            with (root/'audit/zip_pair_outcomes.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertTrue(any(r['same_file']=='0' for r in rows))
            for p,digest in originals.items():
                self.assertEqual(file_hash(p),digest)
            comparison = root/'comparison/comparison.json'
            metadata = json.loads(comparison.read_text())
            metadata['graph_truth_used'] = False
            comparison.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, 'routing/truth provenance'):
                audit(root/'blocks.npy',root/'truth.csv',root/'comparison',root/'invalid-audit')

    def test_score_ranking_and_threshold_precision_have_different_denominators(self):
        s = np.zeros((4,4))
        s[0,1],s[0,2],s[0,3],s[1,2],s[1,3],s[2,3] = .9,.95,.99,.8,.1,.2
        s += s.T
        r,_,_,_ = score_audit(s,np.array([0,0,1,2]),np.array([0,0,0,1]),.5)
        self.assertEqual(r['all_pairs']['pairs'],6)
        self.assertEqual(r['all_pairs']['precision'],.25)
        self.assertAlmostEqual(r['all_pairs']['average_precision'],1/3)
        self.assertAlmostEqual(r['same_format_pairs']['precision'],1/3)
        self.assertEqual(r['same_format_pairs']['average_precision'],.5)
        self.assertEqual(r['by_relation']['different_format']['n'],3)
        zero = classification(np.array([.1,.2]),np.array([False,False]),.5)
        self.assertIsNone(zero['precision'])
        self.assertIsNone(zero['recall'])
        self.assertIsNone(zero['roc_auc'])
        self.assertIsNone(zero['average_precision'])

    def test_end_to_end_reads_only_preserves_inputs_and_accounts_all_seeds(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            fixture(root)
            run(arguments(root))
            originals = {p: file_hash(p) for p in root.rglob('*') if p.is_file()}
            with patch('compare_samefile_grouping.cluster',side_effect=AssertionError('no reclustering')):
                result = audit(root/'blocks.npy',root/'truth.csv',root/'comparison',root/'audit')
            self.assertEqual(result['input_results_modified'],False)
            self.assertEqual(len(result['cohorts']['all_pilot']['seeds']),2)
            for path,digest in originals.items():
                self.assertEqual(file_hash(path),digest)
            self.assertEqual(json.loads((root/'audit/run_state.json').read_text())['status'],'complete')
            with (root/'audit/zip_pair_outcomes.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertTrue(any(r['same_file']=='0' for r in rows))
            self.assertTrue(any(r['cohort']=='not_observed_in_model_metadata' for r in rows))
            with self.assertRaises(FileExistsError):
                audit(root/'blocks.npy',root/'truth.csv',root/'comparison',root/'audit')

    def test_tampered_membership_hash_and_missing_rows_fail(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            x = fixture(root)
            run(arguments(root))
            path = root/'comparison/memberships.csv'
            path.write_text('\n'.join(path.read_text().splitlines()[:-1])+'\n')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                audit(root/'blocks.npy',root/'truth.csv',root/'comparison',root/'audit')
            saved = json.loads((root/'comparison/comparison.json').read_text())
            with self.assertRaisesRegex(ValueError,'missing membership'):
                read_partitions(root/'comparison',saved,len(x))
            self.assertFalse((root/'audit').exists())

    def test_incomplete_run_fails_and_score_change_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            fixture(root)
            run(arguments(root))
            state = root/'comparison/run_state.json'
            complete = state.read_text()
            state.write_text('{"status":"failed"}')
            with self.assertRaisesRegex(ValueError,'completed'):
                audit(root/'blocks.npy',root/'truth.csv',root/'comparison',root/'audit')
            state.write_text(complete)
            scores = root/'scores/samefile_scores.csv.gz'
            with scores.open('ab') as f:
                f.write(b'corrupt')
            with self.assertRaisesRegex(ValueError,'score hash'):
                audit(root/'blocks.npy',root/'truth.csv',root/'comparison',root/'audit')


if __name__ == '__main__':
    unittest.main()
