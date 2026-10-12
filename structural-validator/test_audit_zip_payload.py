import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import numpy as np

from audit_zip_payload import audit
from test_zip_pair_payload import archive_blocks
from zip_pair_candidates import build
from zip_pair_payload import check, file_hash


def prepare(root, x, identities=None):
    np.save(root/'blocks.npy', x)
    with contextlib.redirect_stdout(io.StringIO()):
        build(root/'blocks.npy', root/'candidates')
        check(root/'blocks.npy', root/'candidates', root/'payload')
    if identities is None:
        identities = [('a' if i < 2 else 'b', i % 2 * 512) for i in range(len(x))]
    with (root/'truth.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['sample_id', 'source_file_id', 'source_byte_offset', 'format'])
        w.writeheader()
        for i, (file_id, offset) in enumerate(identities):
            w.writerow(dict(sample_id=i, source_file_id=file_id, source_byte_offset=offset, format='jar'))


def run_audit(root):
    with contextlib.redirect_stdout(io.StringIO()):
        return audit(root/'blocks.npy', root/'truth.csv', root/'payload', root/'audit')


def read_cases(root, category):
    return [json.loads(line) for line in (root/'audit'/f'{category}.jsonl').read_text().splitlines()]


class AuditZipPayloadTests(unittest.TestCase):
    def test_identical_content_cross_file_pass_and_coverage(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                a = archive_blocks(method)
                self.assertEqual(len(a), 2)
                prepare(root, np.concatenate([a, a]))
                before = {p: file_hash(p) for p in (root/'payload').rglob('*') if p.is_file()}
                r = run_audit(root)
                self.assertEqual(r['case_counts']['cross_file_pass'], 2)
                self.assertEqual(r['cross_file_pass_comparisons'], {'IDENTICAL_TARGET_BLOCK': 2})
                passed = r['by_status']['PASS']
                self.assertEqual(passed['candidate_relations'], 4)
                self.assertEqual(passed['exact_target_relations'], 2)
                self.assertEqual(passed['unique_unordered_pairs'], 4)
                self.assertEqual(passed['source_files'], 2)
                self.assertEqual(passed['touched_blocks'], 4)
                self.assertTrue(all(c['decoded_payload_equal'] for c in read_cases(root, 'cross_file_pass')))
                self.assertEqual(before, {p: file_hash(p) for p in before})
                with self.assertRaises(FileExistsError):
                    run_audit(root)

    def test_same_payload_does_not_require_identical_whole_target_block(self):
        a = archive_blocks()
        b = a.copy()
        b[1, -1] ^= 1  # padding after the ZIP, outside the checked payload
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root, np.concatenate([a, b]))
            r = run_audit(root)
            self.assertEqual(r['cross_file_pass_comparisons'],
                             {'IDENTICAL_RECONSTRUCTED_COMPRESSED_PAYLOAD': 2})
            for c in read_cases(root, 'cross_file_pass'):
                self.assertFalse(c['whole_target_block_equal'])
                self.assertTrue(c['used_target_payload_equal'])
                self.assertTrue(c['decoded_payload_equal'])

    def test_missing_reference_not_inferred_from_candidate_pass(self):
        a = archive_blocks()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root, a, [('a', 0), ('b', 512)])
            r = run_audit(root)
            self.assertEqual(r['cross_file_pass_comparisons'], {'REFERENCE_TARGET_ABSENT': 1})
            self.assertIsNone(read_cases(root, 'cross_file_pass')[0]['reference'])

    def test_same_file_wrong_position_failure_separate_from_exact_target_failure(self):
        x = np.concatenate([archive_blocks(seed=1), archive_blocks(seed=2)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root, x, [('a', i*512) for i in range(len(x))])
            r = run_audit(root)
            self.assertEqual(r['case_counts']['same_file_wrong_target_fail'], 2)
            self.assertEqual(r['case_counts']['exact_target_fail'], 0)
            self.assertEqual(r['by_status']['FAIL']['wrong_file_relations'], 0)
            cases = read_cases(root, 'same_file_wrong_target_fail')
            self.assertEqual({c['candidate_minus_expected_blocks'] for c in cases}, {-2, 2})
            self.assertTrue(all(c['comparison'] == 'REFERENCE_PAIR_PASSES' for c in cases))

    def test_true_position_failure_is_reported(self):
        a = archive_blocks()
        a[1, 10] ^= 1
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root, a)
            r = run_audit(root)
            self.assertEqual(r['case_counts']['exact_target_fail'], 1)
            self.assertEqual(r['case_counts']['same_file_wrong_target_fail'], 0)
            self.assertEqual(read_cases(root, 'exact_target_fail')[0]['candidate_minus_expected_blocks'], 0)

    def test_tampered_evidence_rejected_by_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root, archive_blocks())
            path = root/'payload/payload_evidence.jsonl'
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[0]['check']['status'] = 'FAIL'
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with self.assertRaisesRegex(ValueError, 'differs from replay'):
                run_audit(root)
            self.assertFalse((root/'audit').exists())

    def test_block_and_baseline_tampering_rejected(self):
        for tamper in ('blocks', 'baseline'):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                a = archive_blocks()
                prepare(root, a)
                if tamper == 'blocks':
                    a[0, -1] ^= 1
                    np.save(root/'blocks.npy', a)
                else:
                    with (root/'payload/baseline/queries.jsonl').open('a') as f:
                        f.write('\n')
                with self.assertRaises(ValueError):
                    run_audit(root)

    def test_duplicate_truth_positions_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root, archive_blocks(), [('a', 0), ('a', 0)])
            with self.assertRaisesRegex(ValueError, 'duplicate truth position'):
                run_audit(root)


if __name__ == '__main__':
    unittest.main()
