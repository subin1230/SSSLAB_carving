import contextlib
import csv
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from score_samefile_pilot import (extract_embeddings, pair_batches, pair_features,
                                 sigmoid, verify_reference, write_scores)


def dummy_encoder(batch):
    return np.repeat(np.mean(batch, axis=1, dtype=np.float32)[:, None], 256, axis=1)


class SameFileScoringTests(unittest.TestCase):
    def test_confirmed_feature_order_and_swap_symmetry(self):
        a = np.full((2, 256), 2, dtype=np.float32)
        b = np.full((2, 256), 5, dtype=np.float32)
        b[1] = -3
        f = pair_features(a, b)
        np.testing.assert_array_equal(f[0, :256], np.full(256, 3))
        np.testing.assert_array_equal(f[0, 256:], np.full(256, 10))
        np.testing.assert_array_equal(f[1, :256], np.full(256, 5))
        np.testing.assert_array_equal(f[1, 256:], np.full(256, -6))
        np.testing.assert_array_equal(f, pair_features(b, a))
        self.assertEqual(f.dtype, np.float32)

    def test_logits_are_converted_once_without_overflow(self):
        z = np.array([-1000., -1., 0., 1., 1000.])
        np.testing.assert_allclose(sigmoid(z), [0, 0.26894142137, .5, .73105857863, 1])
        with self.assertRaises(ValueError):
            sigmoid(np.array([np.nan]))

    def test_pair_batches_cover_all_pairs_independent_of_batch_size(self):
        expected = {(i, j) for i in range(7) for j in range(i+1, 7)}
        for size in (1, 4, 100):
            batches = list(pair_batches(7, size))
            pairs = [tuple(row) for a in batches for row in a]
            self.assertEqual(len(pairs), 21)
            self.assertEqual(set(pairs), expected)
            self.assertTrue(all(len(a) <= size for a in batches))

    def test_raw_input_keeps_byte_values_and_zero_blocks(self):
        x = np.array([[0]*512, [255]*512, [100]*512], dtype=np.uint8)
        h = extract_embeddings(x, dummy_encoder, 2)
        np.testing.assert_array_equal(h[:, 0], [0, 255, 100])

    def test_reference_reproduction_catches_wrong_preprocessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            x = np.full((20, 512), 200, dtype=np.uint8)
            np.save(root/'raw.npy', x)
            np.save(root/'emb.npy', dummy_encoder(x))
            r = verify_reference(root/'raw.npy', root/'emb.npy', dummy_encoder)
            self.assertEqual(len(r['rows']), 16)
            self.assertTrue(r['matched'])
            with self.assertRaisesRegex(ValueError, 'do not reproduce'):
                verify_reference(root/'raw.npy', root/'emb.npy', lambda b: dummy_encoder(b/255))

    def test_scores_written_without_truth_and_ids_preserved(self):
        x = np.array([[0]*512, [1]*512, [3]*512, [7]*512], dtype=np.uint8)
        h = dummy_encoder(x)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'scores'
            with contextlib.redirect_stdout(io.StringIO()):
                m = write_scores(x, h, lambda f: f[:, :1], out, 2, {'test_double': True})
            self.assertFalse(m['truth_used'])
            self.assertFalse(m['zip_evidence_used'])
            self.assertEqual(m['pair_count'], 6)
            self.assertEqual(m['zero_blocks_retained'], 1)
            with gzip.open(out/'samefile_scores.csv.gz', 'rt') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 6)
            for row in rows:
                i, j = int(row['sample_id_1']), int(row['sample_id_2'])
                z = abs(float(x[i, 0])-float(x[j, 0]))
                self.assertEqual(float(row['same_file_logit']), z)
                self.assertAlmostEqual(float(row['same_file_score']), 1/(1+np.exp(-z)), places=8)
            self.assertEqual(json.loads((out/'run_state.json').read_text())['status'], 'complete')
            with self.assertRaises(FileExistsError):
                write_scores(x, h, lambda f: f[:, :1], out, 2, {})

    def test_partial_run_marked_failed_on_bad_model_output(self):
        x = np.ones((3, 512), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'scores'
            with self.assertRaisesRegex(ValueError, 'scalar logit'):
                write_scores(x, dummy_encoder(x), lambda f: np.zeros((len(f), 2)), out, 4, {})
            self.assertEqual(json.loads((out/'run_state.json').read_text())['status'], 'failed')
            self.assertFalse((out/'score_manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
