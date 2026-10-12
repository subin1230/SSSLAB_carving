import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from diagnose_samefile_reference import (
    diagnose_samples, difference_summary, embedding_report, main, pair_score_report,
)
from score_samefile_pilot import verify_reference


class ReferenceDiagnosisTests(unittest.TestCase):
    def test_large_magnitudes_can_pass_while_small_coordinates_fail(self):
        reference = np.full((2, 256), 100, dtype=np.float32)
        reference[0, 0] = 0
        actual = reference.copy()
        actual[1, 0] += .0045  # Within relative tolerance at 100.
        actual[0, 0] = .0002  # Outside absolute tolerance at zero.
        r = embedding_report(actual, reference, [10, 20])
        self.assertFalse(r['strict_reference_matched'])
        self.assertEqual(r['failing_elements'], 1)
        self.assertEqual(r['failing_rows'], 1)
        self.assertEqual(r['worst_coordinates'][0]['reference_row'], 10)
        self.assertEqual(r['worst_coordinates'][0]['component'], 0)
        self.assertGreater(r['max_absolute_difference'], .004)

    def test_zero_norms_are_explicitly_undefined_not_nan(self):
        r = embedding_report(np.zeros((2, 256)), np.zeros((2, 256)), [0, 1])
        self.assertTrue(r['strict_reference_matched'])
        self.assertIsNone(r['per_row'][0]['relative_l2_error'])
        self.assertIsNone(r['per_row'][0]['cosine_similarity'])
        with self.assertRaises(ValueError):
            difference_summary(np.array([np.nan]), np.array([0]))

    def test_reports_score_effect_without_declaring_model_accuracy(self):
        stored = np.zeros((3, 256), dtype=np.float32)
        actual = stored.copy()
        actual[1, 0] = 1
        r = pair_score_report(actual, stored, lambda features: features[:, :1]-.5)
        self.assertEqual(r['pairs'], 3)
        self.assertEqual(r['diagnostic_crossings_at_score_0_5'], 2)
        self.assertAlmostEqual(r['scores']['max_absolute_difference'], .2449186624)
        self.assertAlmostEqual(r['logits']['max_absolute_difference'], 1)
        self.assertNotIn('accuracy', r)
        self.assertEqual(pair_score_report(stored[:1], stored[:1], None)['pairs'], 0)
        with self.assertRaises(ValueError):
            pair_score_report(actual, stored, lambda features: np.zeros((len(features), 2)))

    def test_separates_batch_effect_and_repeated_call_effect(self):
        batch = np.ones((3, 512), dtype=np.uint8)
        stored = np.ones((3, 256), dtype=np.float32)
        def encode(x):
            self.assertEqual(x.dtype, np.uint8)
            return np.ones((len(x), 256), dtype=np.float32) * (1 if len(x) == 1 else 2)
        r, actual, singles = diagnose_samples(batch, stored, [0, 1, 2], encode, lambda f: f[:, :1])
        self.assertFalse(r['reference_comparison']['strict_reference_matched'])
        self.assertTrue(r['batch_one_reference_comparison']['strict_reference_matched'])
        self.assertEqual(r['repeat_inference']['max_absolute_difference'], 0)
        self.assertEqual(r['batch_one_vs_sample_batch']['max_absolute_difference'], 1)
        np.testing.assert_array_equal(actual, 2*stored)
        np.testing.assert_array_equal(singles, stored)

    def test_diagnostic_saves_failure_without_bypassing_scoring_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, emb, output = root/'raw.npy', root/'emb.npy', root/'diagnosis'
            np.save(raw, np.ones((20, 512), dtype=np.uint8))
            np.save(emb, np.zeros((20, 256), dtype=np.float32))
            original = emb.read_bytes()
            def encode(x):
                return np.ones((len(x), 256), dtype=np.float32)
            models = (SimpleNamespace(predict_on_batch=encode),
                      SimpleNamespace(predict_on_batch=lambda x: x[:, :1]), {'test_double': True})
            argv = ['diagnose_samefile_reference.py', '--encoder-model', 'unused',
                    '--pair-model', 'unused', '--reference-blocks', str(raw),
                    '--reference-embeddings', str(emb), '--output', str(output)]
            with patch('sys.argv', argv), patch('diagnose_samefile_reference.load_models', return_value=models), contextlib.redirect_stdout(io.StringIO()):
                main()
                with self.assertRaises(FileExistsError):
                    main()
            report = json.loads((output/'diagnosis.json').read_text())
            self.assertFalse(report['reference_comparison']['strict_reference_matched'])
            self.assertEqual(report['pair_score_effect']['pairs'], 120)
            self.assertFalse(report['tolerance_changed'])
            self.assertFalse(report['pilot_scoring_started'])
            self.assertFalse((output/'samefile_scores.csv.gz').exists())
            self.assertEqual(emb.read_bytes(), original)
            with self.assertRaisesRegex(ValueError, 'do not reproduce'):
                verify_reference(raw, emb, encode)

    def test_original_batch_report_and_resource_failure_remain_distinct(self):
        for fail_original in (False, True):
            with self.subTest(fail_original=fail_original), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                raw, emb, output = root/'raw.npy', root/'emb.npy', root/'diagnosis'
                np.save(raw, np.ones((20, 512), dtype=np.uint8))
                saved = np.full((20, 256), 8, dtype=np.float32)
                saved[16:] = 4
                np.save(emb, saved)
                def encode(x):
                    if fail_original and len(x) == 8:
                        raise MemoryError('simulated GPU cap reached')
                    return np.full((len(x), 256), len(x), dtype=np.float32)
                models = (SimpleNamespace(predict_on_batch=encode),
                          SimpleNamespace(predict_on_batch=lambda x: x[:, :1]), {'test_double': True})
                argv = ['diagnose_samefile_reference.py', '--encoder-model', 'unused',
                        '--pair-model', 'unused', '--reference-blocks', str(raw),
                        '--reference-embeddings', str(emb), '--output', str(output),
                        '--reference-batch-size', '8']
                with patch('sys.argv', argv), patch('diagnose_samefile_reference.load_models', return_value=models), contextlib.redirect_stdout(io.StringIO()):
                    if fail_original:
                        with self.assertRaisesRegex(RuntimeError, 'partial diagnosis was saved'):
                            main()
                    else:
                        main()
                r = json.loads((output/'diagnosis.json').read_text())
                self.assertFalse(r['reference_comparison']['strict_reference_matched'])
                self.assertFalse(r['pilot_scoring_started'])
                if fail_original:
                    self.assertEqual(r['diagnostic_status'], 'partial_failure')
                    self.assertEqual(r['original_batch_context']['status'], 'failed')
                    self.assertNotIn('reference_comparison', r['original_batch_context'])
                else:
                    self.assertEqual(r['diagnostic_status'], 'complete')
                    self.assertTrue(r['original_batch_context']['reference_comparison']['strict_reference_matched'])
                    self.assertEqual(r['original_batch_context']['batch_context']['processed_rows'], 20)


if __name__ == '__main__':
    unittest.main()
